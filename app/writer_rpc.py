"""Bounded local Codex WebSocket client; never resumes threads when reading."""
import base64
import hashlib
import json
import os
import socket
import struct
import time


class RPCError(RuntimeError):
    pass


class RPC:
    def __init__(self, path, timeout=2.5):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.timeout = timeout
        self.sequence = 0
        try:
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                              f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
            header = bytearray()
            deadline = time.monotonic() + timeout
            while not header.endswith(b"\r\n\r\n"):
                if len(header) > 8192 or time.monotonic() > deadline:
                    raise RPCError("控制服务握手超时")
                header.extend(self._exact(1))
            lines = bytes(header).decode("latin-1").split("\r\n")
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            headers = {line.split(':',1)[0].lower():line.split(':',1)[1].strip() for line in lines[1:] if ':' in line}
            if " 101 " not in lines[0] or headers.get("sec-websocket-accept", "") != expected:
                raise RPCError("控制服务握手失败")
            self.call("initialize", {"clientInfo": {"name": "session-writer-panel", "version": "3.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self._send(1, json.dumps({"method": "initialized"}).encode())
        except Exception:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _exact(self, length):
        chunks = bytearray()
        while len(chunks) < length:
            data = self.sock.recv(length - len(chunks))
            if not data:
                raise RPCError("控制服务连接已关闭")
            chunks.extend(data)
        return bytes(chunks)

    def _send(self, opcode, payload):
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def call(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        self._send(1, json.dumps({"id": request_id, "method": method, "params": params}, separators=(",", ":")).encode())
        deadline = time.monotonic() + self.timeout
        fragment = bytearray()
        while time.monotonic() < deadline:
            self.sock.settimeout(max(.01, deadline - time.monotonic()))
            first, second = self._exact(2)
            opcode = first & 15
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            if length > 8 * 1024 * 1024 or len(fragment) + length > 8 * 1024 * 1024:
                raise RPCError("控制服务响应超过上限")
            mask = self._exact(4) if second & 128 else None
            payload = self._exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise RPCError("控制服务连接已关闭")
            if opcode == 9:
                self._send(10, payload)
                continue
            if opcode not in (0, 1):
                continue
            fragment.extend(payload)
            if not first & 128:
                continue
            message = json.loads(fragment)
            fragment.clear()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                # Never expose an authentication response or upstream data in diagnostics.
                error = message["error"]
                raise RPCError(f"{method}: {error.get('code', 'error')}")
            return message.get("result", {})
        raise RPCError(f"{method}: 请求超时")
