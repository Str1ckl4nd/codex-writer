"""Synthetic log checks only: temporary files, no app or network operations."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import writer_log


class WriterLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-writer-log-test-")
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(writer_log, "LOG_DIR", Path(self.temp.name) / "logs")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        writer_log._last_error = None
        writer_log._warned = False

    def test_event_metadata_counts_and_diagnostic_codes(self):
        self.assertTrue(writer_log.event(
            "bridge.prepare", "request-123", status="ready", durationMs=12.34,
            appCount=2, controllerCount=1, proxyCount=1, ownerPid=123,
            canApply=True, sessionId="00000000-0000-0000-0000-000000000001",
            diagnostics=[{"code": "remote_ready", "message": "private text"}, "ready", "ready"],
        ))
        record = writer_log.recent_events()[0]
        self.assertEqual(record["requestId"], "request-123")
        self.assertEqual(record["event"], "bridge.prepare")
        self.assertTrue(record["timestamp"].endswith("Z"))
        self.assertGreater(record["pid"], 0)
        self.assertEqual(record["appCount"], 2)
        self.assertEqual(record["controllerCount"], 1)
        self.assertEqual(record["proxyCount"], 1)
        self.assertEqual(record["diagnostics"], ["remote_ready", "ready"])
        self.assertTrue(record["canApply"])
        self.assertNotIn("private text", json.dumps(record))

    def test_unknown_payloads_and_embedded_credentials_are_not_written(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertTrue(writer_log.event(
                "probe.fail", "request-123", reason="Authorization: Bearer top-secret-test",
                token="never-write-test", commandLine="ssh secret-user@host", body={"text": "conversation-test"},
                errorCode="MASTER_CONNECT_TIMEOUT", errorType="TimeoutExpired",
            ))
        contents = (writer_log.LOG_DIR / writer_log.LOG_FILENAME).read_text()
        self.assertNotIn("top-secret-test", contents)
        self.assertNotIn("never-write-test", contents)
        self.assertNotIn("ssh secret-user", contents)
        self.assertNotIn("conversation-test", contents)
        self.assertIn("redacted sensitive detail", contents)
        self.assertEqual(stdout.getvalue(), "")

    def test_malformed_values_do_not_raise_or_produce_invalid_json(self):
        class Opaque:
            def __str__(self):
                raise RuntimeError("must not stringify arbitrary objects")
        self.assertTrue(writer_log.event(
            Opaque(), Opaque(), operation=Opaque(), reason=Opaque(),
            durationMs=float("nan"), appCount=-1, proxyCount=True, ownerPid="123",
            controllerCount=2**50, canApply="yes", diagnostics=[{}, [], Opaque(), "valid"],
        ))
        record = writer_log.recent_events()[0]
        self.assertEqual(record["event"], "invalid_event")
        self.assertEqual(record["diagnostics"], ["valid"])
        for key in ("operation", "reason", "durationMs", "appCount", "proxyCount", "ownerPid", "controllerCount", "canApply"):
            self.assertNotIn(key, record)

    def test_rotation_is_bounded_and_reader_keeps_recent_order(self):
        with patch.object(writer_log, "MAX_BYTES", 1024):
            for index in range(45):
                self.assertTrue(writer_log.event("rotate.check", "request-123", appCount=index, reason="x" * 300))
            files = sorted(writer_log.LOG_DIR.glob("writer.jsonl*"))
            self.assertEqual(len(files), 5)
            self.assertTrue(all(path.stat().st_size <= 1024 for path in files))
            recent = writer_log.recent_events(3)
            self.assertEqual([entry["appCount"] for entry in recent], [42, 43, 44])
            self.assertEqual(len(writer_log.recent_events(1)), 1)

    def test_readback_reapplies_whitelist_and_skips_corruption(self):
        writer_log.event("read.check", "request-123")
        path = writer_log.LOG_DIR / writer_log.LOG_FILENAME
        with path.open("a") as stream:
            stream.write("invalid JSON\n")
            stream.write(json.dumps({"event": "old.entry", "timestamp": "2026-09-10T00:00:00Z",
                                     "token": "never-return-this", "reason": "password=never-return-password"}) + "\n")
            stream.write("[]\n")
        records = writer_log.recent_events()
        self.assertEqual(len(records), 2)
        self.assertNotIn("never-return", json.dumps(records))

    def test_failure_is_best_effort_and_stderr_only(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(writer_log, "_open", side_effect=PermissionError("credential=never-echo")):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertFalse(writer_log.event("log.check"))
                self.assertFalse(writer_log.event("log.check"))
                self.assertEqual(writer_log.recent_events(), [])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue().count("log unavailable"), 1)
        self.assertNotIn("never-echo", stderr.getvalue())
        self.assertEqual(writer_log.log_info()["errorCode"], "PermissionError")
        self.assertTrue(writer_log.event("log.recovered"))
        self.assertIsNone(writer_log.log_info()["errorCode"])

    def test_log_info_does_not_create_directory_and_reason_is_bounded(self):
        info = writer_log.log_info()
        self.assertEqual(info["maxFileBytes"], 2 * 1024 * 1024)
        self.assertEqual(info["maxFiles"], 5)
        self.assertFalse(writer_log.LOG_DIR.exists())
        writer_log.event("bounds.check", reason="line\n\t" + "a" * 600)
        reason = writer_log.recent_events()[0]["reason"]
        self.assertLessEqual(len(reason), 400)
        self.assertNotIn("\n", reason)
        self.assertNotIn("\t", reason)

    @unittest.skipIf(os.name == "nt", "POSIX lock failure simulation")
    def test_busy_log_lock_cannot_stall_primary_operation(self):
        with patch("fcntl.flock", side_effect=BlockingIOError()), \
                patch.object(writer_log.time, "monotonic", side_effect=[0.0, 1.0]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(writer_log.event("busy.check"))
        self.assertEqual(writer_log.log_info()["errorCode"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()
