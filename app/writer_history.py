"""Metadata-only reader, used when the old control daemon has shut down.

The helper app-server is private stdio, never resumes threads or starts turns,
and is always stopped by its owner. It cannot acquire a session writer lock.
"""
import json
import os
import selectors
import subprocess
import time

from writer_rpc import RPCError


class HistoryReader:
    ALLOWED = frozenset(('initialize', 'thread/read', 'thread/turns/list'))

    def __init__(self, binary, timeout=6):
        self.timeout = timeout
        self.sequence = 0
        self.buffer = bytearray()
        self.process = subprocess.Popen([str(binary), 'app-server', '--listen', 'stdio://'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        try:
            self.call('initialize', dict(clientInfo=dict(name='session-writer-history', version='3.2'),
                                         capabilities=dict(experimentalApi=True)))
            self.process.stdin.write(b'{"method":"initialized"}\n')
            self.process.stdin.flush()
        except Exception:
            self.close()
            raise

    def call(self, method, params):
        if method not in self.ALLOWED:
            raise RPCError('历史读取器拒绝写入操作')
        self.sequence += 1
        self.process.stdin.write((json.dumps(dict(id=self.sequence, method=method, params=params))+'\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, _, remainder = self.buffer.partition(b'\n')
                self.buffer = bytearray(remainder)
                value = json.loads(line)
                if value.get('id') != self.sequence:
                    continue
                if 'error' in value:
                    raise RPCError('任务元数据读取失败：'+method)
                return value.get('result', {})
            if not self.selector.select(max(.01, deadline-time.monotonic())):
                break
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise RPCError('任务元数据通道已关闭')
            self.buffer.extend(data)
            if len(self.buffer) > 8*1024*1024:
                raise RPCError('任务元数据超过上限')
        raise RPCError('任务元数据读取超时')

    def latest(self, thread_id):
        value = self.call('thread/turns/list', dict(threadId=thread_id, limit=1,
                                                   sortDirection='desc', itemsView='notLoaded'))
        rows = value.get('data', [])
        row = rows[0] if rows else {}
        return dict(turnId=row.get('id'), turnStatus=row.get('status'))

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        finally:
            self.selector.close()
            self.process.stdout.close()
