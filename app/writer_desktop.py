"""Small, bounded client for the installed desktop's thread-follower IPC.

Only read runtime state, interrupt an exact turn, or submit normal user input.
No raw model items, permission overrides, UI automation, or child input bypass.
Protocol versions are checked by the desktop; a mismatch fails closed.
"""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import shutil
import subprocess
import time
import uuid

from writer_rpc import RPCError


PROJECTION = r'''
reduce inputs as $row ({};
  ($row[0]) as $p |
  if ($row|length)==2 and (
    $p==["type"] or $p==["method"] or $p==["sourceClientId"] or $p==["version"] or
    $p==["params","conversationId"] or $p==["params","hostId"] or
    $p==["params","change","type"] or
    $p==["params","change","conversationState","threadRuntimeStatus","type"] or
    $p==["params","change","conversationState","canAcceptDirectInput"] or
    (($p|length)==8 and $p[0:6]==["params","change","conversationState","turnHistory","history","entitiesByKey"] and
     ($p[7]=="turnId" or $p[7]=="id" or $p[7]=="status")) or
    (($p|length)==10 and $p[0:6]==["params","change","conversationState","turnHistory","history","islands"] and
     $p[7]=="entries" and $p[9]=="value") or
    (($p|length)==6 and $p[0:4]==["params","change","conversationState","turns"] and
     ($p[4]|type)=="number" and ($p[5]=="turnId" or $p[5]=="id" or $p[5]=="status")))
  then setpath($p;$row[1]) else . end)
'''


def project_payload(chunks):
    """Project very large history broadcasts in a streaming JSON parser.

    No conversation text is retained or written to disk. jq emits only one
    metadata object at EOF, so the producer cannot deadlock on output backpressure.
    """
    parser = next((path for path in ('/opt/homebrew/bin/jq','/usr/local/bin/jq','/usr/bin/jq')
                   if Path(path).is_file()), None) or shutil.which('jq')
    if not parser:
        raise RPCError('缺少桌面大任务状态读取器 jq，未执行接管')
    process = subprocess.Popen([parser,'-cn','--stream',PROJECTION],stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        for chunk in chunks:
            process.stdin.write(chunk)
        process.stdin.close()
        process.stdin = None
        output,_ = process.communicate(timeout=15)
        if process.returncode or len(output)>4*1024*1024:
            raise RPCError('桌面任务状态投影失败，未执行接管')
        return json.loads(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)


class Desktop:
    def __init__(self, path, timeout=4):
        path = Path(path)
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RPCError('桌面控制通道身份不符')
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.client_id = None
        self.following = {}
        try:
            self.sock.connect(str(path))
            result = self.request('initialize', {'clientType': 'session-writer'}, version=0)
            self.client_id = result['result']['clientId']
        except Exception:
            self.close()
            raise

    def close(self):
        for thread_id, owner in self.following.items():
            try:
                self.follow(thread_id, owner, False)
            except Exception:
                pass
        self.sock.close()

    def _send(self, message):
        data = json.dumps(message, separators=(',', ':')).encode()
        self.sock.sendall(struct.pack('<I', len(data)) + data)

    def _exact(self, count):
        data = bytearray()
        while len(data) < count:
            chunk = self.sock.recv(count-len(data))
            if not chunk:
                raise RPCError('桌面控制通道已关闭')
            data.extend(chunk)
        return bytes(data)

    def _read(self, deadline):
        self.sock.settimeout(max(.01, deadline-time.monotonic()))
        size = struct.unpack('<I', self._exact(4))[0]
        if not 0 < size <= 256*1024*1024:
            raise RPCError('桌面控制响应超过上限')
        if size>1024*1024:
            def chunks():
                remaining=size
                while remaining:
                    self.sock.settimeout(max(.01,deadline-time.monotonic()))
                    count=min(65536,remaining)
                    yield self._exact(count)
                    remaining-=count
            message = project_payload(chunks())
        else:
            message = json.loads(self._exact(size))
        if message.get('type') == 'client-discovery-request':
            self._send({'type': 'client-discovery-response', 'requestId': message['requestId'],
                        'response': {'canHandle': False}})
        return message

    def request(self, method, params, version=1, owner=None):
        request_id = str(uuid.uuid4())
        packet = dict(type='request', requestId=request_id, method=method, params=params,
                      version=version, timeoutMs=int(self.timeout*1000))
        if self.client_id:
            packet['sourceClientId'] = self.client_id
        if owner:
            packet['targetClientId'] = owner
        self._send(packet)
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            message = self._read(deadline)
            if message.get('type') != 'response' or message.get('requestId') != request_id:
                continue
            if message.get('resultType') != 'success':
                # Do not persist exception bodies or conversation contents.
                raise RPCError('桌面接口未完成请求：'+method)
            return message
        raise RPCError('桌面接口请求超时：'+method)

    def owner(self, thread_id):
        return self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': thread_id})['handledByClientId']

    def follow(self, thread_id, owner, enabled):
        self._send(dict(type='broadcast', method='thread-stream-following-changed', version=1,
                        sourceClientId=self.client_id, targetClientIds=[owner],
                        params=dict(conversationId=thread_id, hostId='local', following=enabled)))

    def state(self, thread_id):
        owner = self.owner(thread_id)
        if thread_id in self.following:
            self.follow(thread_id, owner, False)
        self.following[thread_id] = owner
        self.follow(thread_id, owner, True)
        deadline = time.monotonic()+self.timeout
        while time.monotonic() < deadline:
            message = self._read(deadline)
            params = message.get('params', {})
            if (message.get('type') != 'broadcast' or message.get('method') != 'thread-stream-state-changed'
                    or message.get('sourceClientId') != owner or params.get('conversationId') != thread_id
                    or params.get('hostId') != 'local' or message.get('version') != 11):
                continue
            change = params.get('change', {})
            if change.get('type') == 'snapshot':
                return runtime_from_snapshot(change.get('conversationState', {}))
        raise RPCError('桌面任务运行状态未返回')

    def interrupt(self, thread_id, turn_id):
        owner = self.owner(thread_id)
        result = self.request('thread-follower-interrupt-turn', dict(conversationId=thread_id,
                              mode='descendant-cleanup', expectedTurnId=turn_id), version=4, owner=owner)['result']
        return result.get('interruptedTurnId') == turn_id

    def start(self, thread_id, prompt, message_id):
        owner = self.owner(thread_id)
        request = dict(threadId=thread_id, clientUserMessageId=message_id,
                       input=[dict(type='text', text=prompt, text_elements=[])])
        result = self.request('thread-follower-start-turn', dict(conversationId=thread_id,
                              turnStart=dict(request=request, context=dict(inheritThreadSettings=True))),
                              version=2, owner=owner)['result']
        # Acceptance is not proof that descendants have received their messages.
        if 'result' not in result or result['result'] is None:
            raise RPCError('桌面未确认接受继续请求')
        return result['result']


def runtime_from_snapshot(value):
    turns = [turn for turn in value.get('turns',[]) or [] if isinstance(turn,dict)]
    history = (value.get('turnHistory') or {}).get('history') or {}
    entities = history.get('entitiesByKey') or {}
    for island in history.get('islands') or []:
        for entry in island.get('entries') or []:
            turn = entities.get(entry.get('value'))
            if isinstance(turn,dict):
                turns.append(turn)
    if not turns:
        turns = [turn for turn in entities.values() if isinstance(turn,dict)]
    # Legacy and canonical representations may overlap during a migration.
    unique = {turn.get('turnId') or turn.get('id'):turn for turn in turns}
    turns = list(unique.values())
    active = [t for t in turns if t.get('status') == 'inProgress']
    latest = active[-1] if active else (turns[-1] if turns else {})
    runtime = (value.get('threadRuntimeStatus') or {}).get('type')
    if runtime is None:
        runtime = 'active' if active else ('idle' if latest.get('status') in ('completed','interrupted','failed') else 'unknown')
    if runtime not in ('active', 'idle', 'systemError'):
        runtime = 'unknown'
    if runtime=='active' and len(active)!=1:
        latest = {}  # Do not guess an active turn from ambiguous history.
    return dict(status=runtime, turnId=latest.get('turnId') or latest.get('id'),
                turnStatus=latest.get('status'), directInput=value.get('canAcceptDirectInput'))
