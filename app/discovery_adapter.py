"""SSH-only inventory from user configuration and observed local connections.

No account lookup, credential access, HTTP requests, network scanning, or cloud
device cache. Configured hosts are candidates, never evidence of reachability.
"""
from __future__ import annotations

import ipaddress
import platform
import shlex
import socket
import subprocess
import time
from pathlib import Path
from runtime_config import CODEX_HOME, WINDOWS_ALIAS, ALIAS_PATTERN

SSH_CONFIG = Path.home() / '.ssh/config'
CONTROL_SOCKET = str(CODEX_HOME / 'app-server-control/app-server-control.sock')
REFRESH_SECONDS = 10


def _run(args: list[str], timeout: float = 4) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode not in (0, 1):
        raise OSError(f"{Path(args[0]).name} unavailable")
    return result.stdout


def _diag(code: str, title: str, detail: str, action: str = "", severity: str = "warning") -> dict:
    return dict(code=code, severity=severity, title=title, detail=detail, action=action)


def local_hostname() -> str:
    try:
        local = _run(["/usr/sbin/scutil", "--get", "LocalHostName"], 2).strip()
        if local:
            return local + ".local"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return socket.gethostname()


def configured_ssh_hosts() -> dict[str, dict]:
    """Read concrete Host blocks; never execute or resolve SSH configuration.

    Include, wildcard defaults and conditional Match blocks are not evaluated.
    The first literal value wins. Ownership matching still needs an established
    inbound SSH connection, not just a configured endpoint.
    """
    try:
        lines = SSH_CONFIG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    hosts: dict[str, dict] = {}
    aliases: list[str] = []
    for line in lines:
        try:
            parts = shlex.split(line, comments=True)
        except ValueError:
            continue
        if len(parts) < 2:
            continue
        key = parts[0].lower()
        if key == "host":
            aliases = [x for x in parts[1:] if ALIAS_PATTERN.fullmatch(x)]
            for alias in aliases:
                hosts.setdefault(alias, {"alias": alias, "endpoints": []})
        elif key == "match":
            aliases = []
        else:
            for alias in aliases:
                item = hosts[alias]
                if key == "hostname" and len(parts) == 2 and "%" not in parts[1]:
                    if not item["endpoints"]:
                        item["endpoints"].append(parts[1])
                elif key == "user":
                    item.setdefault("user", parts[1])
                elif key == "port" and parts[1].isdigit():
                    item.setdefault("port", int(parts[1]))
    return hosts


def _lsof_fields(output: str) -> list[dict]:
    result: list[dict] = []
    process = 0
    current: dict = {}
    for line in output.splitlines():
        if not line:
            continue
        key, value = line[0], line[1:]
        if key in ("p", "f") and current:
            result.append(current)
            current = {}
        if key == "p":
            process = int(value)
        elif key == "f":
            current = {"pid": process, "fd": value}
        elif current:
            current[key] = value
    if current:
        result.append(current)
    return result


def control_socket_names(path=None) -> set[str]:
    """Match both the published socket alias and the daemon's real pathname.

    Recent Codex releases expose app-server-control.sock as a symlink into a
    private temporary directory. lsof reports the destination, not that alias.
    Resolving a name is not evidence of connectivity; the FD/peer proof remains.
    """
    endpoint=Path(CONTROL_SOCKET if path is None else path)
    names={str(endpoint)}
    try:
        names.add(str(endpoint.resolve(strict=False)))
    except (OSError,RuntimeError):
        pass
    return names


def peer_connections() -> list[dict]:
    """Prove listener <- local proxy <- sshd TCP peer, without network probes."""
    processes: dict[int, tuple[int, str]] = {}
    for line in _run(["/bin/ps", "-axo", "pid=,ppid=,command="]).splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            processes[int(parts[0])] = (int(parts[1]), parts[2])
    proxies = {pid for pid, (_, cmd) in processes.items() if "codex app-server proxy" in cmd and not any(x in cmd for x in (" -c ", " - <<"))}
    servers = {pid for pid, (_, cmd) in processes.items() if " app-server" in cmd and "--listen unix://" in cmd and " proxy" not in cmd and cmd.startswith("/") and "/bin/sh " not in cmd and "/bin/zsh " not in cmd}
    candidates = sorted(proxies | servers)
    if not candidates:
        return []
    unix = _lsof_fields(_run(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(map(str, candidates)), "-U", "-Fpcdtfn"]))
    endpoint_names=control_socket_names()
    socket_devices = {item.get("d"): item["pid"] for item in unix if item["pid"] in servers and item.get("n") in endpoint_names and item.get("d")}
    hosts = configured_ssh_hosts()
    result: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    for item in unix:
        proxy = item["pid"]
        target = item.get("n", "")
        server = socket_devices.get(target[2:] if target.startswith("->") else "")
        if proxy not in proxies or not server:
            continue
        parent = processes.get(proxy, (0, ""))[0]
        for _ in range(4):
            command = processes.get(parent, (0, ""))[1]
            if command.startswith("sshd-session:") or command.startswith("sshd:"):
                break
            parent = processes.get(parent, (0, ""))[0]
        else:
            continue
        tcp = _lsof_fields(_run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(parent), "-iTCP", "-sTCP:ESTABLISHED", "-Fpn"]))
        for conn in tcp:
            address = conn.get("n", "")
            if "->" not in address:
                continue
            destination = address.split("->", 1)[1].split(" ", 1)[0]
            peer = destination.rsplit(":", 1)[0].strip("[]")
            try:
                ipaddress.ip_address(peer)
            except ValueError:
                continue
            key = (server, proxy, peer)
            if key in seen:
                continue
            seen.add(key)
            aliases = sorted(alias for alias, details in hosts.items() if peer in details.get("endpoints", []))
            preferred = WINDOWS_ALIAS if WINDOWS_ALIAS in aliases else next(iter(aliases), None)
            result.append(dict(serverPid=server, proxyPid=proxy, sshPid=parent, peerIp=peer, alias=preferred, aliases=aliases, verifiedAt=int(time.time()), evidence="unix-socket-to-sshd-peer"))
    return result


def discover(force: bool = False) -> dict:
    """Refresh SSH configuration; force never enables another transport."""
    local = local_hostname()
    diagnostics = []
    try:
        ssh_hosts = configured_ssh_hosts()
    except (OSError, UnicodeError):
        ssh_hosts = {}
        diagnostics.append(_diag("ssh_config_unreadable", "SSH 配置无法读取", "未载入任何远端机器。", "检查用户 SSH 配置的权限和编码。"))
    try:
        peers = peer_connections()
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        peers = []
        diagnostics.append(_diag("peer_discovery_failed", "SSH 写入者识别暂不可用", "未能读取代理到主机的完整连接链。", "刷新后再执行交接。"))
    machines = [dict(id="local", name=local, status="online", statusLabel="本机在线", source="local",
                     selectable=True, reason="", clientKind="mac", os=platform.system(), transport="local")]
    for alias, host in sorted(ssh_hosts.items()):
        linked = [peer for peer in peers if alias in peer.get("aliases", [])]
        machines.append(dict(id="ssh:" + alias, name=alias, status="connected" if linked else "configured",
                             statusLabel="SSH 代理已连接" if linked else "SSH 已配置，未核验",
                             source="ssh-config", selectable=False, clientKind="ssh", transport="ssh", alias=alias,
                             reason="已登记 SSH 主机；在 ssh_controllers 中声明控制端平台，经连接和进程核验后可接管。",
                             endpoints=host.get("endpoints", [])))
    return dict(machines=machines, diagnostics=diagnostics, peerConnections=peers, sshHosts=ssh_hosts,
                discoveryMode="ssh-only", discoveredAt=int(time.time()), refreshAfterSeconds=REFRESH_SECONDS,
                discoveryScope="仅本机及用户 SSH 配置中的具名主机；不扫描网络，不使用 OpenAI 同账号设备发现或中转。")
