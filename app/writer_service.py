"""Dynamic observations and explicit session-writer handoff. No polling mutations."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

from discovery_adapter import discover, local_hostname
from caller_context import decode_context, resolve_context
from writer_rpc import RPC, RPCError
from writer_log import event, log_info, recent_events
import writer_force
from runtime_config import APP_DIR, STATE_DIR, CODEX_HOME, CODEX_APP, CODEX_BINARY, EXCLUDED_IDS, WINDOWS_ALIAS, PEER_ALIASES, CONFIG, is_windows_peer
from ssh_bridge import request as ssh_request, BridgeError

ROOT = STATE_DIR
CODEX_ROOT = CODEX_HOME
LOCKS = CODEX_ROOT / 'thread-writer-locks'
CONTROL = CODEX_ROOT / 'app-server-control/app-server-control.sock'
EXCLUDED = set(EXCLUDED_IDS)
UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
STATUS = {'active': '运行中', 'idle': '空闲', 'systemError': '任务出错', 'notLoaded': '未加载',
          'unknown': '运行状态未确认', 'unowned': '未占用'}


class WriterError(RuntimeError):
    def __init__(self, message, code='WRITER_ERROR', stage='operation'):
        super().__init__(message)
        self.code=code
        self.stage=stage


def command(argv, timeout=4, input=None):
    return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)


def diagnostic(code, title, detail, action='', severity='warning'):
    return dict(code=code, title=title, detail=detail, action=action, severity=severity)


def atomic_json(path, value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, prefix='.writer-', delete=False, encoding='utf-8') as handle:
        temporary = handle.name
        json.dump(value, handle, ensure_ascii=False)
    os.replace(temporary, path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return {}


def process_table():
    result = command(['/bin/ps', '-axo', 'pid=,ppid=,uid=,lstart=,command='])
    if result.returncode:
        raise WriterError('无法读取进程表，请检查系统访问权限。')
    output = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 8)
        if len(fields) == 9 and fields[0].isdigit():
            output[int(fields[0])] = {'pid': int(fields[0]), 'ppid': int(fields[1]),
                                     'uid': int(fields[2]), 'start': ' '.join(fields[3:8]), 'command': fields[8]}
    return output


def process_kind(process, processes):
    cmd = process.get('command', '')
    candidates=(str(CODEX_BINARY),'/Applications/ChatGPT.app/Contents/Resources/codex','/Applications/Codex.app/Contents/Resources/codex')
    trusted=any(cmd==candidate or cmd.startswith(candidate+' ') for candidate in candidates)
    standalone=str(CODEX_ROOT/'packages/standalone')+'/'
    if cmd.startswith(standalone):
        end=cmd.find('/bin/codex',len(standalone))
        trusted=trusted or (end>=0 and (len(cmd)==end+10 or cmd[end+10:end+11]==' '))
    if not trusted or process.get('uid') != os.getuid() or ' app-server' not in cmd or ' app-server proxy' in cmd:
        return 'unknown'
    if '--listen unix://' in cmd:
        return 'ssh-server'
    parent = processes.get(process.get('ppid'), {}).get('command', '')
    if parent in (str(CODEX_APP/'Contents/MacOS'/CODEX_APP.stem),'/Applications/ChatGPT.app/Contents/MacOS/ChatGPT', '/Applications/Codex.app/Contents/MacOS/Codex'):
        return 'mac-desktop'
    return 'unknown'


def lock_inventory():
    if not LOCKS.is_dir():
        return {}
    result = command(['/usr/sbin/lsof', '-nP', '-Fpn', '+D', str(LOCKS)], timeout=6)
    if result.returncode not in (0, 1) or (result.stderr.strip() and not result.stdout):
        raise WriterError('无法读取会话锁；请检查锁目录访问权限。')
    inventory = {}
    pid = 0
    for line in result.stdout.splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith('n'):
            path = Path(line[1:])
            if path.parent != LOCKS or not UUID.fullmatch(path.stem):
                continue
            try:
                inode = path.stat().st_ino
            except FileNotFoundError:
                continue
            item = {'pid': pid, 'inode': inode}
            if item not in inventory.setdefault(path.stem, []):
                inventory[path.stem].append(item)
    return inventory


def metadata():
    candidates = sorted(CODEX_ROOT.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]), reverse=True)
    if not candidates:
        raise WriterError('找不到会话索引数据库。')
    with sqlite3.connect(candidates[0].as_uri()+'?mode=ro', uri=True, timeout=2) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
        names = ['id','name','title','preview','created_at','updated_at','thread_source','agent_path','source','archived','is_pinned']
        select = ','.join(name if name in columns else 'NULL AS '+name for name in names)
        rows = connection.execute('SELECT '+select+' FROM threads').fetchall()
    return {r['id']: dict(r) for r in rows}


def windows_bridge(operation, **kwargs):
    started=time.monotonic()
    event('bridge.start',operation=operation,stage='ssh',profile=WINDOWS_ALIAS)
    try:
        value=ssh_request(operation,**kwargs)
    except BridgeError as error:
        event('bridge.failed',operation=operation,stage='ssh',errorCode=error.code,durationMs=(time.monotonic()-started)*1000)
        raise WriterError('Windows 连接请求未确认：'+error.code,code=error.code,stage='ssh') from None
    if value.get('error'):
        raise WriterError('Windows 连接检查失败。')
    event('bridge.complete',operation=operation,stage='remote_query',durationMs=(time.monotonic()-started)*1000,
          appCount=len(value.get('apps',[])),controllerCount=len(value.get('controllers',[])),proxyCount=value.get('proxyCount',0))
    return value


def windows_identity(force=False):
    path = ROOT/'windows-identity-cache.json'
    previous = read_json(path)
    ttl=45 if previous.get('available') and previous.get('detection')=='confirmed' else 3
    if not force and previous.get('schemaVersion')==2 and 0 <= time.time()-previous.get('checkedAt', 0) < ttl:
        event('identity.cache',cacheState='hit',status='ok' if previous.get('available') else 'failed')
        return previous
    event('identity.cache',cacheState='refresh')
    try:
        identity = windows_bridge('identity')
        if identity.get('schemaVersion')!=2 or not isinstance(identity.get('hostName'), str) or not isinstance(identity.get('controllers'), list):
            raise WriterError('Windows 身份响应格式不匹配。')
        identity.update(checkedAt=time.time(), available=True)
        event('identity.complete',status=identity.get('detection','unknown'),appCount=len(identity.get('apps',[])),
              controllerCount=len(identity['controllers']),proxyCount=identity.get('proxyCount',0))
    except Exception as error:
        identity = {'schemaVersion':2,'hostName': previous.get('hostName', ''), 'checkedAt': time.time(),
                    'available': False, 'apps': [], 'controllers':[], 'proxyCount': 0,
                    'error':str(error) if isinstance(error,WriterError) else type(error).__name__,
                    'errorCode':getattr(error,'code',type(error).__name__), 'errorStage':getattr(error,'stage','identity')}
        event('identity.failed',errorCode=identity['errorCode'],stage=identity['errorStage'],errorType=type(error).__name__)
    atomic_json(path, identity)
    return identity


def socket_server(processes):
    if not CONTROL.exists():
        return 0
    # lsof's Unix-socket name matching does not follow the published alias.
    result = command(['/usr/sbin/lsof', '-t', str(CONTROL.resolve())])
    candidates={int(line) for line in result.stdout.splitlines()
                if line.isdigit() and process_kind(processes.get(int(line),{}),processes)=='ssh-server'}
    if len(candidates)>1:
        raise WriterError('控制通道对应多个会话服务，尚不能确认唯一写入服务。',code='CONTROL_SERVER_AMBIGUOUS')
    return next(iter(candidates),0)


def controller_problem(identity):
    if not identity.get('available'):
        return identity.get('error') or '远端进程检测未完成，请刷新 SSH 主机。'
    owners=identity.get('controllers',[])
    if len(owners)==1:
        if identity.get('matchedProxyCount',identity.get('proxyCount'))!=identity.get('proxyCount'):
            return '检测到无法关联到桌面客户端的额外连接，请检查 Windows 的会话连接后刷新。'
        return ''
    if len(owners)>1:
        return f'检测到 {len(owners)} 个桌面客户端连接此 Mac，无法确定应关闭哪一个。'
    if not identity.get('apps'):
        return 'Windows 桌面客户端未运行。'
    return 'Windows 桌面客户端正在运行，但尚未找到它通往此 Mac 的会话连接；请等待重连后刷新。'


def observe(force=False, client_context=None):
    started=time.monotonic()
    event('snapshot.start',operation='snapshot',cacheState='force' if force else 'normal')
    processes = process_table()
    locks = lock_inventory()
    threads = metadata()
    server_pid = socket_server(processes)
    rpc = None
    live = {}
    diagnostics = []
    try:
        if server_pid:
            rpc = RPC(CONTROL)
            ids = rpc.call('thread/loaded/list', {}).get('data', [])
            status_deadline=time.monotonic()+5
            for thread_id in ids[:150]:
                if time.monotonic()>status_deadline:
                    diagnostics.append(diagnostic('status_partial','部分任务状态待刷新','读取已达到本次时限；未读取的任务保留未知状态。','稍后刷新。'))
                    break
                try:
                    data = rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False})
                    live[thread_id] = data.get('thread', {}).get('status', {}).get('type', 'unknown')
                except RPCError:
                    live[thread_id]='unknown'
                    diagnostics.append(diagnostic('thread_read_'+thread_id,'一条任务状态暂不可读',thread_id,'任务可能已退出，稍后刷新。','info'))
        else:
            diagnostics.append(diagnostic('control_offline', '会话服务尚未识别', '当前未识别到独立的 Codex 会话服务；这不代表 Windows 的 SSH 命令通道不可达。', '在目标电脑打开 Mac 会话连接后刷新。'))
    except Exception:
        diagnostics.append(diagnostic('control_unreadable', '会话状态读取失败', '进程占用仍可查看，运行状态暂不能确认。', '检查桌面连接状态并重新发现。'))
        if rpc:
            rpc.close()
            rpc = None
    try:
        discovery = discover(force=force)
    finally:
        if rpc:
            rpc.close()
    diagnostics += discovery.get('diagnostics', [])
    ssh_hosts = discovery.get('sshHosts', {})
    machines = [machine for machine in discovery['machines'] if machine['id']=='local' or
                (machine.get('source')=='ssh-config' and machine.get('alias') in ssh_hosts
                 and machine['id']=='ssh:'+machine['alias'])]
    storage_name=next(machine['name'] for machine in machines if machine['id']=='local')
    caller,local_identity=resolve_context(client_context,os.environ.get('SSH_CONNECTION',''),
                                          ssh_hosts,storage_name)
    peer_connections = discovery.get('peerConnections', [])
    windows_peers = [p for p in peer_connections if is_windows_peer(p) and
                     set(p.get('aliases',[])).intersection(ssh_hosts)]
    windows = next((machine for machine in machines if machine['id']=='ssh:'+WINDOWS_ALIAS), None)
    # Only the registered SSH pair is eligible. A cache or cloud device record
    # must never grant authority to query or stop another machine.
    if local_identity is not None:
        identity=local_identity
        event('identity.local',source='windows-local',status=identity.get('detection','unavailable'))
        if identity.get('available'):
            atomic_json(ROOT/'windows-identity-cache.json',identity)
    else:
        identity = windows_identity(force=force) if windows and windows_peers else dict(
            available=False, error='Windows 的 SSH 会话代理尚未连接；不能据此判定电脑离线。')
    if windows:
        if identity.get('hostName') and (identity.get('available') or local_identity is not None):
            windows['name']=identity['hostName']
        if caller['platform']=='windows' and caller['verified']:
            caller['machineId']=windows['id']
        linked_aliases = {a for p in windows_peers for a in p.get('aliases', []) if a in PEER_ALIASES}
        machines = [m for m in machines if m is windows or not (m.get('source')=='ssh-config' and m.get('alias') in linked_aliases)]
        windows['sources'] = ['ssh-config'] + (['verified-ssh-peer'] if windows_peers else [])
    for machine in machines:
        if machine['id'] == 'local':
            machine.update(selectable=True, clientKind='mac',role='storage-host',sourceLabel='Mac 会话数据主机',
                           statusLabel='本机 Mac · 会话数据主机' if caller['platform']=='mac' else '在线 · Mac 会话数据主机')
        elif machine is windows:
            problem=controller_problem(identity)
            if not identity.get('hostName') and not problem:
                problem='SSH 返回的主机名称未确认。'
            proven = bool(windows_peers) and not problem
            is_caller=caller['platform']=='windows' and caller['verified']
            machine.update(selectable=bool(proven), reason='' if proven else (problem or 'Windows 未连接 Mac 会话服务；不是 Windows 电脑离线。'),
                           clientKind='windows',role='writer-controller',sourceLabel='SSH · Windows 本机检测' if local_identity is not None else 'SSH · Windows 连接检测',
                           status='online' if proven or is_caller else 'configured',
                           statusLabel=('本机 Windows · 可接管' if is_caller else 'Windows · 可接管') if proven else
                                       ('本机 Windows 在线 · 接管条件未满足' if is_caller else 'SSH 已配置 · Windows 接管状态待确认'),
                           transport='ssh',isCaller=is_caller)
        if not machine.get('selectable'):
            diagnostics.append(diagnostic('machine_'+hashlib.sha256(machine['id'].encode()).hexdigest()[:8],
                                          machine['name']+' · '+machine['statusLabel'], machine.get('reason',''), '', 'info'))
    if windows_peers and not windows:
        diagnostics.append(diagnostic('peer_ssh_unregistered', 'Windows SSH 配置尚未完整',
                                      '检测到 SSH 连接，但指定的 Windows 主机别名不在用户 SSH 主机列表中。', '在 SSH 配置中登记 windows_ssh_alias 对应的具名 Host 块。'))
    if windows_peers and not identity.get('available'):
        diagnostics.append(diagnostic('windows_probe_failed','Windows 状态检查失败',identity.get('error','连接检查未成功'),
                                      '刷新 SSH 主机；检查密钥、主机指纹与 VPN/局域网连接。'))
    owners = {}
    affected = {}
    for thread_id, entries in locks.items():
        for item in entries:
            affected.setdefault(item['pid'], []).append(thread_id)
        if len(entries) != 1:
            owners[thread_id] = dict(pid=0, ownerState='ambiguous', writerHostName='多个进程占用', ownerMachineId=None, processKind='unknown')
            continue
        pid = entries[0]['pid']
        kind = process_kind(processes.get(pid, {}), processes)
        peers = [p for p in peer_connections if p['serverPid'] == pid]
        endpoints = {p['peerIp'] for p in peers}
        if kind == 'mac-desktop':
            name, machine_id, state = machines[0]['name'], 'local', 'observed'
        elif kind == 'ssh-server' and len(endpoints) == 1 and windows and any(is_windows_peer(p) for p in peers):
            name, machine_id, state = windows['name'], windows['id'], 'observed'
        elif kind == 'ssh-server':
            name, machine_id, state = 'SSH 共享服务（客户端未确认）', None, 'ambiguous'
        else:
            name, machine_id, state = '未识别进程', None, 'unknown'
        owners[thread_id] = dict(pid=pid, ownerState=state, writerHostName=name, ownerMachineId=machine_id, processKind=kind)
    def record(thread_id):
        meta = threads.get(thread_id, {})
        name = meta.get('name') or meta.get('title') or thread_id
        created = meta.get('created_at') or 0
        try:
            date = dt.datetime.fromtimestamp(created).astimezone().strftime('%Y-%m-%d %H:%M:%S') if created else '未知'
        except (ValueError, OverflowError):
            date = '未知'
        owner = owners.get(thread_id, dict(pid=0, ownerState='unowned', writerHostName='未占用', ownerMachineId=None, processKind='none'))
        state = live.get(thread_id, 'unknown' if owner['pid'] else 'notLoaded')
        summary = '已根据实际文件句柄确认占用进程。' if owner['ownerState']=='observed' else (
            '没有进程打开该会话写锁。' if not owner['pid'] and owner['ownerState']=='unowned' else '占用来源不明确，请先排错。')
        if owner['pid'] and owner['pid'] != server_pid:
            summary += ' 桌面服务的运行状态需在对应客户端查看。'
        return dict(id=thread_id, name=str(name).replace('\n',' ')[:200], createdAt=created, createdTime=date,
                    updatedAt=meta.get('updated_at') or 0, **owner, status=state, statusLabel=STATUS.get(state,state),
                    affectedCount=len(affected.get(owner['pid'], [])), diagnosticSummary=summary)
    def visible(meta):
        return not meta.get('archived') and meta.get('thread_source') in (None,'user') and not meta.get('agent_path')
    recent = sorted((k for k,v in threads.items() if visible(v)), key=lambda k: threads[k].get('updated_at') or 0, reverse=True)[:60]
    included = set(recent) | {k for k in owners if k in threads and visible(threads[k])}
    rows = [record(k) for k in included if k not in EXCLUDED]
    rows.sort(key=lambda r: (r['pid']==0, -r['updatedAt'], r['id']))
    for pid, ids in affected.items():
        active = [i for i in ids if live.get(i)=='active']
        if len(ids)>1:
            diagnostics.append(diagnostic('shared_'+str(pid), '共享服务持有 '+str(len(ids))+' 条会话',
                                          '其中 '+str(len(active))+' 条已确认运行中；交接前会列出完整影响范围。',
                                          '应用前检查影响列表。', 'info'))
        if process_kind(processes.get(pid,{}),processes)=='unknown':
            diagnostics.append(diagnostic('owner_unknown_'+str(pid),'发现未识别持锁进程',f'进程 {pid} 的身份不满足自动交接条件。','检查该进程后再应用。'))
    diagnostics.append(diagnostic('scope','仅支持 SSH 双机交接','显示 Mac 保存的最近 60 条主任务及已占用主任务；操作端不等于当前占用者。机器发现与跨机控制只使用用户配置的 SSH。',
                                  '跨网请自行配置 Tailscale 或 VPN；不支持 OpenAI 同账号设备中转模式。','info'))
    last = read_json(ROOT/'last-handoff.json')
    if last.get('ok') is False:
        diagnostics.append(diagnostic('last_handoff_failed','上次应用未完成', str(last.get('message',''))[:500],
                                      '先刷新确认当前占用者，再根据具体原因处理。'))
    recovery = read_json(ROOT/'logs/force-handoff.json')
    if recovery.get('continuationComplete') is False or recovery.get('stage') in ('failed','partial'):
        diagnostics.append(diagnostic('continue_pending','强制接管恢复记录待核对',
                                      '写入权与代理继续分别记录；恢复编号：'+str(recovery.get('operationId','')),
                                      '在日志中的 force-handoff.json 查看名单和发送状态；不要盲目重复继续。'))
    snapshot = dict(schemaVersion=4, backendVersion='3.4.0', generatedAt=dt.datetime.now(dt.timezone.utc).isoformat(), records=rows,
                    caller=caller,storageHost=dict(name=storage_name,platform='mac',machineId='local'),
                    machines=machines, diagnostics=diagnostics, stale=False, discoveryMode='ssh-only',
                    discoveryScope=discovery.get('discoveryScope',''), refreshSeconds=10,logInfo=log_info())
    event('snapshot.complete',operation='snapshot',durationMs=(time.monotonic()-started)*1000,
          diagnostics=[d['code'] for d in diagnostics],ownerPid=server_pid)
    return snapshot, dict(processes=processes, locks=locks, threads=threads, owners=owners, affected=affected,
                          record=record, serverPid=server_pid, identity=identity, peers=peer_connections)


def revision_for(thread_id, machine, context, force_scope=None):
    owner = context['owners'].get(thread_id, {})
    pid = owner.get('pid',0)
    p = context['processes'].get(pid,{})
    impacted = sorted(context['affected'].get(pid, []))
    identity = context.get('identity',{}) if machine.get('clientKind')=='mac' and owner.get('processKind')=='ssh-server' else {}
    stamp = dict(threadId=thread_id, machineId=machine['id'], owner=owner, process=p,
                 locks=context['locks'].get(thread_id,[]), impacted=impacted,
                 states={i:context['record'](i)['status'] for i in impacted},
                 destinationProcess=context['processes'].get(context['serverPid'],{}) if machine.get('clientKind')=='windows' else {},
                 peers=[{k:v for k,v in peer.items() if k!='verifiedAt'} for peer in context.get('peers',[])],
                 windowsControllers=identity.get('controllers',[]), selectable=machine.get('selectable',False))
    if force_scope is not None:
        stamp['forceContinue'] = writer_force.stamp(force_scope,context)
    return hashlib.sha256(json.dumps(stamp,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def make_plan(thread_id, machine_id, force=False, force_continue=False, client_context=None):
    snapshot, context = observe(force,client_context=client_context)
    machines = snapshot['machines']
    machine = next((m for m in machines if m['id']==machine_id or m['name']==machine_id), None)
    if not machine:
        raise WriterError('目标主机不在当前发现列表中，请重新发现。')
    row = next((r for r in snapshot['records'] if r['id']==thread_id), None)
    if not row:
        raise WriterError('会话不在当前列表中，请刷新后重新选择。')
    same = row.get('ownerMachineId')==machine['id']
    impacts = [context['record'](k) for k in context['affected'].get(row['pid'],[])]
    reason = ''
    force_scope = None
    if force_continue:
        try:
            force_scope = writer_force.prepare(sys.modules[__name__],thread_id,machine,context)
            impacts = [context['record'](k) for k in force_scope['affected']]
            runtime_by_id = {entry['id']:entry for entry in force_scope['runtime']}
            for impact in impacts:
                runtime = runtime_by_id.get(impact['id'],{})
                if runtime.get('status'):
                    impact['status'] = runtime['status']
                    impact['statusLabel'] = STATUS.get(runtime['status'],runtime['status'])
                if runtime.get('agentPath'):
                    impact['name'] = '子代理 '+runtime['agentPath']+' · '+impact['id']
            same = same and not force_scope['sourcePids']
        except (WriterError,RPCError,ValueError,OSError) as error:
            reason = str(error) if isinstance(error,(WriterError,RPCError)) else '强制接管的运行状态读取失败，请刷新。'
    if reason:
        pass
    elif row['ownerState'] in ('unknown','ambiguous') and not force_continue:
        reason='当前占用者无法确定，无法自动交接。'
    elif not machine.get('selectable'):
        reason=machine.get('reason') or '该主机当前不可用。'
    elif row['processKind']=='ssh-server' and machine['clientKind']=='mac' and not same and not force_continue:
        reason=controller_problem(context['identity'])
    if CONFIG.get('enable_handoff') is not True:
        reason='只读模式：请完成配置并显式开启 enable_handoff 后再应用。'
    if same:
        message=f"{row['name']}\n当前占用者：{machine['name']}\n已是目标主机，无需切换。"
    else:
        message=f"会话：{row['name']}\n当前占用者：{row['writerHostName']}\n目标主机：{machine['name']}"
        if impacts:
            # Keep excluded UI rows in the process impact set.
            message+='\n\n切换需要关闭原客户端写入服务，会影响以下会话：\n'+'\n'.join('• '+r['name']+'（'+r['statusLabel']+'）' for r in impacts)
            message+='\n\n原桌面客户端也会退出；它连接的其他远程主机将断开。上表仅枚举本机保存的会话。'
        message+='\n\n应用后将检查实际写锁归属。'
        if force_continue:
            active = [entry for entry in (force_scope or {}).get('runtime',[]) if entry['status']=='active']
            message+='\n\n已选择强制接管：记录 '+str(len(active))+' 个运行中代理。正常关闭后仍持锁时，才强制停止上述已核验服务。'
            message+='\n仅恢复本次被中断且没有新回合的代理；已完成或原本空闲的代理不会启动。'
            message+='\n子代理受主代理管理，继续请求将经主代理转交；界面会明确显示尚待回执的情况。'
    if reason:
        message+='\n\n'+reason
    plan=dict(threadId=thread_id,currentPid=row['pid'],machineId=machine['id'],message=message,
              canApply=not reason,reason=reason,revision=revision_for(thread_id,machine,context,force_scope),
              changed=not same,impacted=impacts,currentOwner=row['writerHostName'],desiredOwner=machine['name'],
              forceContinue=force_continue,forceScope=force_scope)
    event('plan.complete',operation='plan',sessionId=thread_id,machineId=machine['id'],canApply=not reason,
          ownerPid=row['pid'],reason=reason,controllerCount=len(context['identity'].get('controllers',[])),
          errorCode=context['identity'].get('errorCode') if reason else None)
    return plan, snapshot, context, machine


def wait_gone(pid, start, timeout=12):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        p=process_table().get(pid)
        if p is None or p['start']!=start:
            return
        time.sleep(.2)
    raise WriterError('原写入服务未退出，交接未完成。')


def wait_released(pid, start, thread_ids, timeout=12):
    """The old process can live on after releasing this task; do not misreport it."""
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        locks=lock_inventory()
        if not any(any(item['pid']==pid for item in locks.get(key,[])) for key in thread_ids):
            return
        process=process_table().get(pid)
        if process is not None and process.get('start')!=start:
            raise WriterError('持锁进程身份已变化，请刷新。',code='WRITER_IDENTITY_CHANGED')
        time.sleep(.2)
    raise WriterError('原服务仍持有主任务或子代理的写锁；可选择“强制接管并继续被中断代理”后重新确认。',code='WRITER_STILL_HELD')


def acquire_writer(thread_id, machine, context):
    # Reuse the existing acquisition and postflight rules for each affected root.
    holders=lock_inventory().get(thread_id,[])
    wanted='ssh-server' if machine['clientKind']=='windows' else 'mac-desktop'
    already=False
    if holders:
        processes=process_table()
        already=(len(holders)==1 and process_kind(processes.get(holders[0]['pid'],{}),processes)==wanted
                 and (wanted=='mac-desktop' or holders[0]['pid']==context['serverPid']))
        if not already:
            raise WriterError('目标任务仍由其他写入者占用，未启动继续。',code='DESTINATION_NOT_OWNED')
    if not already:
        if machine['clientKind']=='windows':
            if socket_server(process_table())!=context['serverPid']:
                raise WriterError('目标会话服务已变化，请刷新。')
            rpc=RPC(CONTROL,timeout=15)
            try:
                rpc.call('thread/resume',{'threadId':thread_id,'excludeTurns':True})
            finally:
                rpc.close()
        elif machine['clientKind']=='mac':
            result=command(['/usr/bin/open','-a',str(CODEX_APP),'codex://threads/'+thread_id])
            if result.returncode:
                raise WriterError('写锁已释放，但 Mac 未打开该任务。')
        else:
            raise WriterError('该主机没有支持的交接接口。')
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        holders=lock_inventory().get(thread_id,[])
        if len(holders)==1:
            processes=process_table()
            if process_kind(processes.get(holders[0]['pid'],{}),processes)==wanted:
                if wanted=='ssh-server':
                    if holders[0]['pid']!=context['serverPid']:
                        raise WriterError('写锁被另一个后台服务获取，请刷新确认。')
                    from discovery_adapter import peer_connections
                    peers=[p for p in peer_connections() if p['serverPid']==holders[0]['pid']]
                    if len({p['peerIp'] for p in peers})!=1 or not any(is_windows_peer(p) for p in peers):
                        raise WriterError('目标服务已取得锁，但控制端身份变化。')
                return dict(ok=True,changed=not already,threadId=thread_id,machineId=machine['id'],
                            pid=holders[0]['pid'],message='已交给 '+machine['name']+'：'+thread_id)
        time.sleep(.3)
    raise WriterError('原占用者已释放，但未确认目标取得写锁，请刷新查看当前占用者。')


def claim(thread_id, machine_id, expected_revision, force_continue=False, client_context=None):
    if CONFIG.get('enable_handoff') is not True:
        raise WriterError('交接默认关闭；请先在配置中显式开启 enable_handoff。',code='HANDOFF_DISABLED')
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with open(ROOT/'handoff-operation.lock','a') as guard:
        try:
            fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise WriterError('另一项交接正在进行，请等待它完成后刷新。')
        p,snapshot,context,machine=make_plan(thread_id,machine_id,force=True,force_continue=force_continue,client_context=client_context)
        if not p['canApply']:
            raise WriterError(p['reason'])
        if not expected_revision or p['revision']!=expected_revision:
            raise WriterError('占用进程、影响范围或目标连接已变化，请刷新并重新应用。')
        if not p['changed']:
            return dict(ok=True,changed=False,message=p['message'])
        if force_continue:
            return writer_force.apply(sys.modules[__name__],p,context,machine)
        owner=context['owners'].get(thread_id,{})
        pid=owner.get('pid',0)
        process=context['processes'].get(pid,{})
        # All reads/probes above completed before any write. No timer invokes this function.
        if pid:
            current=process_table().get(pid,{})
            new_locks=lock_inventory()
            new_affected=sorted(k for k,entries in new_locks.items() if any(e['pid']==pid for e in entries))
            if (current!=process or new_locks.get(thread_id)!=context['locks'].get(thread_id)
                    or new_affected!=sorted(context['affected'].get(pid,[]))):
                raise WriterError('确认后占用者已变化，请刷新。')
            if owner['processKind']=='mac-desktop':
                app=context['processes'].get(process['ppid'],{})
                if process_table().get(app.get('pid'))!=app:
                    raise WriterError('原桌面进程已变化。')
                event('claim.close_source',operation='claim',stage='mac_desktop',sessionId=thread_id,ownerPid=app['pid'])
                os.kill(app['pid'],signal.SIGTERM)
                wait_released(pid,process['start'],writer_force.family_of(thread_id,context['threads']))
            elif owner['processKind']=='ssh-server':
                controllers=context['identity']['controllers']
                event('claim.close_source',operation='claim',stage='windows_desktop',sessionId=thread_id,ownerPid=controllers[0]['pid'])
                windows_bridge('close-desktop',expectedPid=controllers[0]['pid'],expectedStart=controllers[0]['start'])
                current=process_table().get(pid)
                if current:
                    if current!=process:
                        raise WriterError('原服务身份已变化，停止交接。')
                    event('claim.release_writer',operation='claim',sessionId=thread_id,ownerPid=pid)
                    os.kill(pid,signal.SIGTERM)
                    wait_released(pid,process['start'],writer_force.family_of(thread_id,context['threads']))
            else:
                raise WriterError('占用进程类型不支持交接。')
        if machine['clientKind']=='windows':
            event('claim.acquire',operation='claim',stage='windows',sessionId=thread_id)
            if socket_server(process_table())!=context['serverPid']:
                raise WriterError('目标会话服务已变化，已停止交接；请刷新。')
            rpc=RPC(CONTROL,timeout=15)
            try:
                rpc.call('thread/resume',{'threadId':thread_id,'excludeTurns':True})
            finally:
                rpc.close()
        elif machine['clientKind']=='mac':
            event('claim.acquire',operation='claim',stage='mac',sessionId=thread_id)
            result=command(['/usr/bin/open','-a',str(CODEX_APP),'codex://threads/'+thread_id])
            if result.returncode:
                raise WriterError('写锁已释放，但目标客户端未打开；请在目标主机重新打开会话。')
        else:
            raise WriterError('该主机没有支持的交接接口。')
        wanted='ssh-server' if machine['clientKind']=='windows' else 'mac-desktop'
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            holders=lock_inventory().get(thread_id,[])
            if len(holders)==1:
                processes=process_table()
                if process_kind(processes.get(holders[0]['pid'],{}),processes)==wanted:
                    if wanted=='ssh-server':
                        if holders[0]['pid']!=context['serverPid']:
                            raise WriterError('写锁被另一个后台服务获取，请刷新确认。')
                        # Verify the controller as well as the host-side process.
                        from discovery_adapter import peer_connections
                        peers=[p for p in peer_connections() if p['serverPid']==holders[0]['pid']]
                        if len({p['peerIp'] for p in peers})!=1 or not any(is_windows_peer(p) for p in peers):
                            raise WriterError('目标服务已取得锁，但控制端身份变化；请刷新确认。')
                    result=dict(ok=True,changed=True,threadId=thread_id,machineId=machine['id'],
                                pid=holders[0]['pid'],message='已交给 '+machine['name']+'：'+p['threadId'])
                    atomic_json(ROOT/'last-handoff.json',dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),**result))
                    event('claim.verified',operation='claim',status='success',sessionId=thread_id,ownerPid=holders[0]['pid'],machineId=machine['id'])
                    return result
            time.sleep(.3)
        raise WriterError('原占用者已释放，但未确认目标取得写锁，请刷新查看当前占用者。')


def valid_uuid(value):
    if not UUID.fullmatch(value):
        raise argparse.ArgumentTypeError('invalid session ID')
    return value


def main():
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest='command',required=True)
    logs_parser=commands.add_parser('logs')
    logs_parser.add_argument('--format',choices=['json'],default='json')
    logs_parser.add_argument('--client-context',type=decode_context)
    for name in ['snapshot','list','diagnose']:
        p=commands.add_parser(name)
        p.add_argument('--format',choices=['json','message','lines','tsv'],default='json')
        p.add_argument('--rediscover',action='store_true')
        p.add_argument('--client-context',type=decode_context)
    for name in ['plan','claim']:
        p=commands.add_parser(name)
        p.add_argument('--thread-id',required=True,type=valid_uuid)
        p.add_argument('--machine-id','--host',dest='machine_id',required=True)
        p.add_argument('--format',choices=['json','message','ui'],default='json')
        p.add_argument('--force-continue',action='store_true',help='Explicitly force the confirmed writer and request continuation only for interrupted agents')
        p.add_argument('--client-context',type=decode_context)
        if name=='claim':
            p.add_argument('--expected-revision',required=True)
    args=parser.parse_args()
    started=time.monotonic()
    event('request.start',operation=args.command,sessionId=getattr(args,'thread_id',None),machineId=getattr(args,'machine_id',None))
    try:
        if args.command=='logs':
            value=dict(**log_info(),events=recent_events(100))
        elif args.command in ['snapshot','list','diagnose']:
            snapshot,_=observe(args.rediscover,client_context=args.client_context)
            value=snapshot if args.command=='snapshot' else snapshot['records'] if args.command=='list' else snapshot['diagnostics']
            if args.format=='lines' or args.format=='tsv':
                for r in snapshot['records']:
                    print('\t'.join([r['id'],r['name'].replace('\t',' '),r['createdTime']]))
                return 0
        elif args.command=='plan':
            # An application plan must retry a failed identity observation now,
            # rather than reusing the snapshot's failure for another 45 seconds.
            value=make_plan(args.thread_id,args.machine_id,force=True,force_continue=args.force_continue,client_context=args.client_context)[0]
        else:
            value=claim(args.thread_id,args.machine_id,args.expected_revision,force_continue=args.force_continue,client_context=args.client_context)
        if args.format=='message' and isinstance(value,dict):
            print(value.get('message',json.dumps(value,ensure_ascii=False)))
        else:
            print(json.dumps(value,ensure_ascii=False,separators=(',',':')))
        event('request.complete',operation=args.command,durationMs=(time.monotonic()-started)*1000,
              sessionId=getattr(args,'thread_id',None),machineId=getattr(args,'machine_id',None))
        return 0
    except Exception as error:
        message=str(error) if isinstance(error,(WriterError,RPCError)) else '读取或执行失败：'+type(error).__name__
        if args.command=='claim':
            atomic_json(ROOT/'last-handoff.json',dict(ok=False,at=dt.datetime.now(dt.timezone.utc).isoformat(),message=message))
        print(json.dumps(dict(ok=False,error=message,message=message,errorCode=getattr(error,'code',type(error).__name__),
                              stage=getattr(error,'stage','operation')),ensure_ascii=False))
        event('request.failed',operation=args.command,durationMs=(time.monotonic()-started)*1000,
              errorCode=getattr(error,'code',type(error).__name__),errorType=type(error).__name__,
              stage=getattr(error,'stage','operation'),sessionId=getattr(args,'thread_id',None))
        return 1


if __name__=='__main__':
    sys.exit(main())
