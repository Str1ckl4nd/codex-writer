"""Dynamic observations and explicit session-writer handoff. No polling mutations."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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
from runtime_config import APP_DIR, STATE_DIR, CODEX_HOME, CODEX_APP, CODEX_BINARY, EXCLUDED_IDS, CONFIG, controller_specs, peer_controller_ids
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


def controller_bridge(operation, spec, **kwargs):
    started=time.monotonic()
    event('bridge.start',operation=operation,stage='ssh',profile=spec['alias'])
    try:
        value=ssh_request(operation,alias=spec['alias'],**kwargs)
    except BridgeError as error:
        event('bridge.failed',operation=operation,stage='ssh',errorCode=error.code,durationMs=(time.monotonic()-started)*1000)
        raise WriterError(spec['alias']+' 的 SSH 请求未确认：'+error.code,code=error.code,stage='ssh') from None
    if value.get('error'):
        raise WriterError(spec['alias']+' 的连接检查失败。')
    event('bridge.complete',operation=operation,stage='remote_query',durationMs=(time.monotonic()-started)*1000,
          appCount=len(value.get('apps',[])),controllerCount=len(value.get('controllers',[])),proxyCount=value.get('proxyCount',0))
    return value


def controller_identity(spec, ssh_hosts, force=False):
    binding=dict(spec=spec,hosts={a:ssh_hosts.get(a) for a in spec['peer_aliases']})
    key=hashlib.sha256(json.dumps(binding,sort_keys=True).encode()).hexdigest()
    path=ROOT/('controller-'+key[:24]+'-cache.json')
    previous=read_json(path)
    ttl=10 if previous.get('available') and previous.get('detection')=='confirmed' else 3
    if (not force and previous.get('binding')==key and previous.get('schemaVersion')==2 and
            0<=time.time()-previous.get('checkedAt',0)<ttl):
        return previous
    try:
        identity=controller_bridge('identity',spec)
        if (identity.get('schemaVersion')!=2 or not isinstance(identity.get('hostName'),str) or
                not identity.get('hostName') or not isinstance(identity.get('controllers'),list)):
            raise WriterError('SSH 身份响应格式不匹配。')
        identity.update(checkedAt=time.time(),available=True,binding=key)
    except Exception as error:
        identity=dict(schemaVersion=2,hostName=previous.get('hostName',''),checkedAt=time.time(),binding=key,
                      available=False,apps=[],controllers=[],proxyCount=0,
                      error=str(error) if isinstance(error,WriterError) else type(error).__name__,
                      errorCode=getattr(error,'code',type(error).__name__))
    atomic_json(path,identity)
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
        return '远端桌面客户端未运行。'
    return '远端桌面客户端正在运行，但尚未找到它通往此数据主机的 SSH 会话连接；请等待重连后刷新。'


def controller_sources(pid, context):
    specs=context.get('controllerSpecs',[])
    by_id={spec['id']:spec for spec in specs}
    ids=set()
    for peer in context.get('peers',[]):
        if peer['serverPid']!=pid:
            continue
        matches=peer_controller_ids(peer,specs)
        if len(matches)!=1:
            raise WriterError('原服务存在未唯一确认的 SSH 控制端，不能关闭。',code='CONTROLLER_UNKNOWN')
        ids.add(matches[0])
    result=[]
    for key in sorted(ids):
        identity=context.get('identities',{}).get(key,{})
        problem=controller_problem(identity)
        if problem or not identity.get('hostName'):
            raise WriterError(by_id[key]['alias']+'：'+(problem or '主机名称未确认'),code='CONTROLLER_UNKNOWN')
        result.append(dict(spec=by_id[key],identity=identity))
    return result


def controller_impacts(pid, machine, context):
    keep=machine['id'] if machine['id']!='local' and pid==context['serverPid'] else None
    return [dict(machineId=value['spec']['id'],name=value['identity']['hostName'],
                 pid=value['identity']['controllers'][0]['pid'],start=value['identity']['controllers'][0]['start'])
            for value in controller_sources(pid,context) if value['spec']['id']!=keep]


def checked_peers(pid, context):
    from discovery_adapter import peer_connections
    def stamp(peer):
        return json.dumps({k:v for k,v in peer.items() if k!='verifiedAt'},sort_keys=True)
    approved={stamp(peer) for peer in context['peers'] if peer['serverPid']==pid}
    current=[peer for peer in peer_connections() if peer['serverPid']==pid]
    if not {stamp(peer) for peer in current}<=approved:
        raise WriterError('SSH 控制端连接已经变化，请重新确认。',code='PEER_SCOPE_CHANGED')
    return current


def close_remote_controllers(pid, machine, context, on_closed=None):
    keep=machine['id'] if machine['id']!='local' and pid==context['serverPid'] else None
    closed=[]
    for value in controller_sources(pid,context):
        spec,identity=value['spec'],value['identity']
        if spec['id']==keep:
            continue
        writer_force.assert_scope(sys.modules[__name__],
            dict(sourcePids=[pid],affected=context['affected'].get(pid,[])),context,allow_shrink=True)
        current=checked_peers(pid,context)
        if not any(spec['id'] in peer_controller_ids(peer,context['controllerSpecs']) for peer in current):
            continue
        owner=identity['controllers'][0]
        event('claim.close_controller',operation='claim',profile=spec['alias'],ownerPid=owner['pid'])
        controller_bridge('close-desktop',spec,expectedPid=owner['pid'],expectedStart=owner['start'],
                          expectedHostName=identity['hostName'])
        deadline=time.monotonic()+12
        while True:
            current=checked_peers(pid,context)
            if not any(spec['id'] in peer_controller_ids(peer,context['controllerSpecs']) for peer in current):
                break
            if time.monotonic()>deadline:
                raise WriterError(spec['alias']+' 的 SSH 控制连接仍未释放。',code='CONTROLLER_STILL_CONNECTED')
            time.sleep(.2)
        closed.append(spec['id'])
        if on_closed:
            on_closed(spec['id'])
    return closed


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
    ssh_hosts=discovery.get('sshHosts',{})
    specs=[spec for spec in controller_specs() if spec['alias'] in ssh_hosts]
    machines=[machine for machine in discovery['machines'] if machine['id']=='local' or
              (machine.get('source')=='ssh-config' and machine.get('alias') in ssh_hosts and
               machine['id']=='ssh:'+machine['alias'])]
    storage_name=next(machine['name'] for machine in machines if machine['id']=='local')
    caller,local_identity=resolve_context(client_context,os.environ.get('SSH_CONNECTION',''),
                                         ssh_hosts,storage_name,specs=specs)
    peers=discovery.get('peerConnections',[])
    identities={}
    machine_by_id={machine['id']:machine for machine in machines}
    spec_by_id={spec['id']:spec for spec in specs}
    grouped_aliases={alias:spec['id'] for spec in specs for alias in spec['peer_aliases']}
    machines=[machine for machine in machines if machine.get('alias') not in grouped_aliases or
              machine['id']==grouped_aliases[machine['alias']]]
    linked_by_id={spec['id']:[peer for peer in peers if peer_controller_ids(peer,specs)==[spec['id']]] for spec in specs}
    pending=[spec for spec in specs if linked_by_id[spec['id']] and
             not (local_identity is not None and caller.get('machineId')==spec['id'])]
    if pending:
        with ThreadPoolExecutor(max_workers=min(8,len(pending))) as pool:
            queries={spec['id']:pool.submit(controller_identity,spec,ssh_hosts,force=force) for spec in pending}
            identities.update({key:future.result() for key,future in queries.items()})
    for spec in specs:
        machine=machine_by_id[spec['id']]
        linked=linked_by_id[spec['id']]
        if local_identity is not None and caller.get('machineId')==spec['id']:
            identity=local_identity
        elif linked:
            identity=identities[spec['id']]
        else:
            identity=dict(available=False,error='SSH 任务代理尚未连接；不能据此判定电脑离线。')
        identities[spec['id']]=identity
        if identity.get('hostName') and (identity.get('available') or caller.get('machineId')==spec['id']):
            machine['name']=identity['hostName']
        problem=controller_problem(identity)
        if not identity.get('hostName') and not problem:
            problem='SSH 返回的主机名称未确认。'
        proven=bool(linked) and not problem
        is_caller=caller.get('verified') and caller.get('machineId')==spec['id']
        label='Windows' if spec['platform']=='windows' else 'Mac'
        machine.update(selectable=bool(proven),reason='' if proven else problem,
                       clientKind=spec['platform'],role='writer-controller',sourceLabel='SSH · '+label+' 控制端检测',
                       status='online' if proven or is_caller else 'configured',
                       statusLabel=('当前操作端 · ' if is_caller else label+' · ')+('可接管' if proven else '接管条件未满足'),
                       transport='ssh',isCaller=bool(is_caller),sources=['ssh-config']+(['verified-ssh-peer'] if linked else []))
    for machine in machines:
        if machine['id']=='local':
            machine.update(selectable=True,clientKind='mac',role='storage-host',sourceLabel='任务数据主机',
                           statusLabel='本机 · 数据主机' if caller.get('machineId')=='local' else '在线 · 数据主机')
        if not machine.get('selectable'):
            diagnostics.append(diagnostic('machine_'+hashlib.sha256(machine['id'].encode()).hexdigest()[:8],
                                          machine['name']+' · '+machine['statusLabel'],machine.get('reason',''),'','info'))
    for peer in peers:
        if len(peer_controller_ids(peer,specs))!=1:
            diagnostics.append(diagnostic('peer_unconfirmed','存在未唯一对应的 SSH 控制端',
                                          '该连接不能唯一对应到已登记的控制端；不会自动关闭。','检查 ssh_controllers 和 SSH 别名/地址。'))
    owners={}
    affected={}
    for thread_id,entries in locks.items():
        for item in entries:
            affected.setdefault(item['pid'],[]).append(thread_id)
        if len(entries)!=1:
            owners[thread_id]=dict(pid=0,ownerState='ambiguous',writerHostName='多个进程占用',
                                  ownerMachineId=None,ownerMachineIds=[],processKind='unknown')
            continue
        pid=entries[0]['pid']
        kind=process_kind(processes.get(pid,{}),processes)
        links=[peer for peer in peers if peer['serverPid']==pid]
        mapping=[peer_controller_ids(peer,specs) for peer in links]
        ids=sorted({key for match in mapping for key in match}) if mapping and all(len(match)==1 for match in mapping) else []
        if kind=='mac-desktop':
            name,machine_id,state,ids=storage_name,'local','observed',['local']
        elif kind=='ssh-server' and ids:
            names=[machine_by_id[key]['name'] for key in ids]
            name=names[0] if len(ids)==1 else '共享 SSH：'+'、'.join(names)
            machine_id=ids[0] if len(ids)==1 else None
            state='observed' if len(ids)==1 else 'shared'
        elif kind=='ssh-server':
            name,machine_id,state='SSH 共享服务（控制端未确认）',None,'ambiguous'
        else:
            name,machine_id,state='未识别进程',None,'unknown'
        owners[thread_id]=dict(pid=pid,ownerState=state,writerHostName=name,ownerMachineId=machine_id,
                               ownerMachineIds=ids,processKind=kind)
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
            '多个已登记 SSH 控制端共用此服务；交接将列出需断开的控制端。' if owner['ownerState']=='shared' else
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
    diagnostics.append(diagnostic('scope','SSH 多机交接','显示数据主机保存的最近 60 条主任务及已占用主任务；操作端不等于当前占用者。控制端按用户 SSH 配置逐台核验，不要求远端运行本工具界面。',
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
    snapshot = dict(schemaVersion=5, backendVersion='4.0.0', generatedAt=dt.datetime.now(dt.timezone.utc).isoformat(), records=rows,
                    caller=caller,storageHost=dict(name=storage_name,platform='mac',machineId='local'),
                    machines=machines, diagnostics=diagnostics, stale=False, discoveryMode='ssh-only',
                    discoveryScope=discovery.get('discoveryScope',''), refreshSeconds=10,logInfo=log_info())
    event('snapshot.complete',operation='snapshot',durationMs=(time.monotonic()-started)*1000,
          diagnostics=[d['code'] for d in diagnostics],ownerPid=server_pid)
    return snapshot, dict(processes=processes, locks=locks, threads=threads, owners=owners, affected=affected,
                          record=record,serverPid=server_pid,identities=identities,controllerSpecs=specs,
                          identity=next(iter(identities.values()),{}),peers=peers,sshHosts=ssh_hosts)


def revision_for(thread_id, machine, context, force_scope=None):
    owner=context['owners'].get(thread_id,{})
    pid=owner.get('pid',0)
    impacted=sorted(set(context['affected'].get(pid,[])) | (
        set(context['affected'].get(context['serverPid'],[])) if machine['id']!='local' else set()))
    stamp=dict(threadId=thread_id,machineId=machine['id'],owner=owner,
               process=context['processes'].get(pid,{}),locks=context['locks'].get(thread_id,[]),
               impacted=impacted,states={key:context['record'](key)['status'] for key in impacted},
               destinationProcess=context['processes'].get(context['serverPid'],{}) if machine['id']!='local' else {},
               affectedLocks={key:context['locks'].get(key,[]) for key in impacted},
               peers=[{k:v for k,v in peer.items() if k!='verifiedAt'} for peer in context.get('peers',[])],
               specs=context.get('controllerSpecs',[]),
               identities={key:{k:v for k,v in value.items() if k!='checkedAt'} for key,value in context.get('identities',{}).items()},
               selectable=machine.get('selectable',False))
    if force_scope is not None:
        stamp['forceContinue']=writer_force.stamp(force_scope,context)
    return hashlib.sha256(json.dumps(stamp,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def make_plan(thread_id, machine_id, force=False, force_continue=False, client_context=None):
    snapshot, context = observe(force,client_context=client_context)
    machines = snapshot['machines']
    matches=[m for m in machines if m['id']==machine_id]
    if not matches:
        matches=[m for m in machines if m['name']==machine_id]
    machine=matches[0] if len(matches)==1 else None
    if not machine:
        raise WriterError('目标主机不在当前发现列表中，请重新发现。')
    row = next((r for r in snapshot['records'] if r['id']==thread_id), None)
    if not row:
        raise WriterError('会话不在当前列表中，请刷新后重新选择。')
    same = row.get('ownerMachineId')==machine['id']
    impacts = [context['record'](k) for k in context['affected'].get(row['pid'],[])]
    reason = ''
    force_scope = None
    controller_effects=[]
    try:
        remote_pids={row['pid']} if row['processKind']=='ssh-server' else set()
        if machine['id']!='local' and context['serverPid']:
            remote_pids.add(context['serverPid'])
        controller_effects=[effect for pid in sorted(remote_pids) for effect in controller_impacts(pid,machine,context)]
        if machine['id']!='local' and controller_effects:
            impact_ids={item['id'] for item in impacts} | set(context['affected'].get(context['serverPid'],[]))
            impacts=[context['record'](key) for key in sorted(impact_ids)]
    except WriterError as error:
        reason=str(error)
    if force_continue:
        try:
            force_scope = writer_force.prepare(sys.modules[__name__],thread_id,machine,context)
            controller_effects=force_scope['controllerImpacts']
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
        if controller_effects:
            message+='\n\n将关闭以下 SSH 控制端的 Codex 窗口：\n'+'\n'.join('• '+item['name']+'（'+item['machineId']+'）' for item in controller_effects)
        if machine['id']!='local' and row['pid']==context['serverPid']:
            message+='\n保留目标控制端和共享后台，仅断开其他已确认控制端。'
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
              forceContinue=force_continue,forceScope=force_scope,controllerImpacts=controller_effects)
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
    local=machine['id']=='local'
    holders=lock_inventory().get(thread_id,[])
    wanted='mac-desktop' if local else 'ssh-server'
    already=False
    if holders:
        processes=process_table()
        already=(len(holders)==1 and process_kind(processes.get(holders[0]['pid'],{}),processes)==wanted
                 and (local or holders[0]['pid']==context['serverPid']))
        if not already:
            raise WriterError('目标任务仍由其他写入者占用，未启动继续。',code='DESTINATION_NOT_OWNED')
    if not already:
        if not local:
            if socket_server(process_table())!=context['serverPid']:
                raise WriterError('目标会话服务已变化，请刷新。')
            rpc=RPC(CONTROL,timeout=15)
            try:
                rpc.call('thread/resume',{'threadId':thread_id,'excludeTurns':True})
            finally:
                rpc.close()
        else:
            result=command(['/usr/bin/open','-a',str(CODEX_APP),'codex://threads/'+thread_id])
            if result.returncode:
                raise WriterError('写锁已释放，但本机未打开该任务。')
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        holders=lock_inventory().get(thread_id,[])
        if len(holders)==1:
            processes=process_table()
            if process_kind(processes.get(holders[0]['pid'],{}),processes)==wanted:
                if not local:
                    if holders[0]['pid']!=context['serverPid']:
                        raise WriterError('写锁被另一个后台服务获取，请刷新确认。')
                    from discovery_adapter import peer_connections
                    peers=[peer for peer in peer_connections() if peer['serverPid']==holders[0]['pid']]
                    if not peers or any(peer_controller_ids(peer,context['controllerSpecs'])!=[machine['id']] for peer in peers):
                        raise WriterError('后台已持锁，但尚未确认目标为唯一 SSH 控制端。',code='DESTINATION_SHARED')
                    spec=next(spec for spec in context['controllerSpecs'] if spec['id']==machine['id'])
                    fresh=controller_identity(spec,context['sshHosts'],force=True)
                    approved=context['identities'][machine['id']]
                    if (controller_problem(fresh) or fresh.get('hostName')!=approved.get('hostName') or
                            fresh.get('controllers')!=approved.get('controllers')):
                        raise WriterError('目标控制端进程身份变化，请刷新。',code='DESTINATION_CHANGED')
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
        plan,snapshot,context,machine=make_plan(thread_id,machine_id,force=True,force_continue=force_continue,client_context=client_context)
        if not plan['canApply']:
            raise WriterError(plan['reason'])
        if not expected_revision or plan['revision']!=expected_revision:
            raise WriterError('占用进程、控制端、影响范围或目标连接已变化，请刷新并重新应用。')
        if not plan['changed']:
            return dict(ok=True,changed=False,message=plan['message'])
        if force_continue:
            return writer_force.apply(sys.modules[__name__],plan,context,machine)
        owner=context['owners'].get(thread_id,{})
        pid=owner.get('pid',0)
        process=context['processes'].get(pid,{})
        if pid:
            current=process_table().get(pid,{})
            new_locks=lock_inventory()
            affected=sorted(key for key,entries in new_locks.items() if any(item['pid']==pid for item in entries))
            if current!=process or new_locks.get(thread_id)!=context['locks'].get(thread_id) or affected!=sorted(context['affected'].get(pid,[])):
                raise WriterError('确认后占用者已变化，请刷新。')
            if owner['processKind']=='mac-desktop':
                app=context['processes'].get(process['ppid'],{})
                if process_table().get(app.get('pid'))!=app:
                    raise WriterError('原桌面进程已变化。')
                os.kill(app['pid'],signal.SIGTERM)
                wait_released(pid,process['start'],writer_force.family_of(thread_id,context['threads']))
            elif owner['processKind']=='ssh-server':
                close_remote_controllers(pid,machine,context)
                preserve=machine['id']!='local' and pid==context['serverPid']
                if not preserve:
                    if checked_peers(pid,context):
                        raise WriterError('仍有 SSH 控制端连接，未停止共享后台。',code='CONTROLLER_STILL_CONNECTED')
                    current=process_table().get(pid)
                    if current:
                        if current!=process:
                            raise WriterError('原服务身份已变化，请刷新。')
                        os.kill(pid,signal.SIGTERM)
                        wait_released(pid,process['start'],writer_force.family_of(thread_id,context['threads']))
            else:
                raise WriterError('占用进程类型不支持交接。')
        if machine['id']!='local' and context['serverPid']!=pid:
            close_remote_controllers(context['serverPid'],machine,context)
        result=acquire_writer(thread_id,machine,context)
        result['changed']=True
        result['controllerImpacts']=plan['controllerImpacts']
        atomic_json(ROOT/'last-handoff.json',dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),**result))
        event('claim.verified',operation='claim',status='success',sessionId=thread_id,ownerPid=result['pid'],machineId=machine['id'])
        return result


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
