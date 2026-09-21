"""Best-effort, bounded operational logs for the session writer.

Only pass static summaries to ``reason``. Raw command lines, request bodies,
conversation text, credentials and exception messages do not belong here.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid


from runtime_config import STATE_DIR
LOG_DIR = STATE_DIR / "logs"
LOG_FILENAME = "writer.jsonl"
MAX_BYTES = 2 * 1024 * 1024
MAX_FILES = 5  # Includes the active file.
DEFAULT_REQUEST_ID = uuid.uuid4().hex
_last_error = None
_warned = False
_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,159}$")
_SENSITIVE = re.compile(
    r"(?:auth(?:orization)?|access[_ -]?token|refresh[_ -]?token|token|password|secret|api[_ -]?key|cookie|bearer)\s*[:= ]"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|https?://\S*[?@]\S+",
    re.IGNORECASE,
)
_CODE_FIELDS = frozenset({
    "operation", "stage", "status", "errorCode", "errorType", "profile",
    "cacheState", "sessionId", "machineId", "source", "operationId", "turnId",
})
_COUNT_FIELDS = frozenset({"appCount", "controllerCount", "proxyCount", "ownerPid", "agentCount"})


def _code(value):
    if isinstance(value, str) and _CODE.fullmatch(value) and not _SENSITIVE.search(value):
        return value
    return None


def _reason(value):
    if not isinstance(value, str):
        return None
    # Structured payloads and recognizable secrets are dropped in their entirety.
    if value.lstrip().startswith(("{", "[")) or _SENSITIVE.search(value):
        return "[redacted sensitive detail]"
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:400]


def _fields(values):
    result = {}
    for key in _CODE_FIELDS:
        value = _code(values.get(key))
        if value is not None:
            result[key] = value
    for key in _COUNT_FIELDS:
        value = values.get(key)
        if type(value) is int and 0 <= value <= 2**31 - 1:
            result[key] = value
    value = values.get("durationMs")
    if type(value) in (int, float) and 0 <= value <= 86400000 and math.isfinite(value):
        result["durationMs"] = round(value, 3)
    if type(values.get("canApply")) is bool:
        result["canApply"] = values["canApply"]
    value = _reason(values.get("reason"))
    if value is not None:
        result["reason"] = value
    diagnostics = values.get("diagnostics")
    if isinstance(diagnostics, (list, tuple)):
        codes = []
        for diagnostic in diagnostics[:64]:
            code = _code(diagnostic.get("code") if type(diagnostic) is dict else diagnostic)
            if code and code not in codes:
                codes.append(code)
        result["diagnostics"] = codes
    return result


def _failure(exc):
    global _last_error, _warned
    _last_error = _code(type(exc).__name__) or "LoggingError"
    if not _warned:
        _warned = True
        try:
            print("Session writer log unavailable (" + _last_error + ").", file=sys.stderr)
        except Exception:
            pass


def _open(path, flags):
    return os.open(str(path), flags | getattr(os, "O_NOFOLLOW", 0), 0o600)


@contextmanager
def _locked():
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = _open(LOG_DIR / ".writer-log.lock", os.O_CREAT | os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            acquire = lambda: msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + 0.25
        while True:
            try:
                acquire()
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("log lock unavailable") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(descriptor)


def _rotate(incoming_bytes):
    active = LOG_DIR / LOG_FILENAME
    if not active.exists() or active.stat().st_size + incoming_bytes <= MAX_BYTES:
        return
    for index in range(MAX_FILES - 1, 0, -1):
        source = active if index == 1 else LOG_DIR / (LOG_FILENAME + "." + str(index - 1))
        target = LOG_DIR / (LOG_FILENAME + "." + str(index))
        if source.exists():
            os.replace(source, target)


def event(name, request_id=None, **fields):
    """Append one safe event, returning success without ever raising to callers."""
    global _last_error
    try:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "pid": os.getpid(),
            "requestId": _code(request_id) or DEFAULT_REQUEST_ID,
            "event": _code(name) or "invalid_event",
        }
        record.update(_fields(fields))
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValueError("record exceeds log size limit")
        with _locked():
            _rotate(len(encoded))
            descriptor = _open(LOG_DIR / LOG_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
        _last_error = None
        return True
    except Exception as exc:
        _failure(exc)
        return False


def log_info():
    """Report log location and retention; this call does not create files."""
    return {
        "directory": str(LOG_DIR), "file": str(LOG_DIR / LOG_FILENAME),
        "maxFileBytes": MAX_BYTES, "maxFiles": MAX_FILES,
        "errorCode": _last_error,
    }


def recent_events(limit=100):
    """Read recent sanitized events in chronological order across rotations."""
    try:
        count = min(500, max(1, limit)) if type(limit) is int else 100
        from collections import deque
        records = deque(maxlen=count)
        if not LOG_DIR.exists():
            return []
        with _locked():
            for index in range(MAX_FILES - 1, -1, -1):
                path = LOG_DIR / (LOG_FILENAME + ("." + str(index) if index else ""))
                if not path.exists():
                    continue
                with os.fdopen(_open(path, os.O_RDONLY), "rb") as stream:
                    for line in stream:
                        if len(line) > MAX_BYTES:
                            continue
                        try:
                            item = json.loads(line)
                            if type(item) is not dict:
                                continue
                            clean = _fields(item)
                            for key in ("timestamp", "event", "requestId"):
                                value = _code(item.get(key))
                                if value:
                                    clean[key] = value
                            if type(item.get("pid")) is int and 0 < item["pid"] <= 2**31 - 1:
                                clean["pid"] = item["pid"]
                            if "event" in clean and "timestamp" in clean:
                                records.append(clean)
                        except (ValueError, TypeError, UnicodeDecodeError):
                            continue
        return list(records)
    except Exception as exc:
        _failure(exc)
        return []
