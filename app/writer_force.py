"""Opt-in, scope-bound force handoff with a durable continuation receipt.

Never bypass parent-owned child input. A parent receives an explicit recovery
request for the exact interrupted children; pending delivery is not success.
"""
import datetime as dt
import json
import re
import signal
import time
import uuid

from writer_desktop import Desktop
from writer_history import HistoryReader
from writer_rpc import RPC
from runtime_config import is_windows_peer


AGENT_PATH = re.compile(r'^/root(?:/[A-Za-z0-9_-]+)*$')


def parent_of(meta):
    source = meta.get('source') or {}
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except ValueError:
            return None
    if not isinstance(source, dict):
        return None
    branch = source.get('subagent') or source.get('subAgent') or {}
    return (branch.get('thread_spawn') or {}).get('parent_thread_id')


def root_of(thread_id, threads):
    seen = set()
    while True:
        if thread_id in seen or thread_id not in threads:
            raise ValueError('代理的主任务关系无法确认')
        seen.add(thread_id)
        parent = parent_of(threads[thread_id])
        if not parent:
            if threads[thread_id].get('thread_source') == 'subagent':
                raise ValueError('子代理缺少主任务记录')
            return thread_id
        thread_id = parent


def family_of(thread_id, threads):
    result = {thread_id}
    while True:
        children = {key for key, meta in threads.items() if parent_of(meta) in result}
        if children <= result:
            return sorted(result)
        result |= children


def scope_for(thread_id, machine, context):
    family = family_of(thread_id, context['threads'])
    source_pids = set()
    for key in family:
        owner = context['owners'].get(key, {})
        if owner.get('pid') and owner.get('ownerMachineId') != machine['id']:
            source_pids.add(owner['pid'])
        elif owner.get('ownerState') in ('unknown', 'ambiguous') and not owner.get('pid'):
            raise ValueError('主任务或子代理存在多个写入者')
    affected = sorted({key for pid in source_pids for key in context['affected'].get(pid, [])})
    roots = {key: root_of(key, context['threads']) for key in affected}
    # Historical, unloaded descendants are not interruption candidates or UI data.
    return dict(family=[key for key in family if key==thread_id or key in context['owners']],
                sourcePids=sorted(source_pids), affected=affected, roots=roots)


def runtime_records(w, scope, context):
    result = []
    rpc = desktop = None
    deadline = time.monotonic()+25
    if len(scope['affected'])>64:
        raise w.WriterError('受影响任务超过本次可核验上限，请先关闭不相关的空闲任务。',code='FORCE_SCOPE_LIMIT')
    try:
        for key in scope['affected']:
            if time.monotonic()>deadline:
                raise w.WriterError('代理状态核验超时，尚未执行接管。',code='FORCE_PREFLIGHT_TIMEOUT')
            owner = context['owners'].get(key, {})
            pid = owner.get('pid')
            if pid == context['serverPid']:
                if rpc is None:
                    rpc = RPC(w.CONTROL, timeout=4)
                thread = rpc.call('thread/read', dict(threadId=key, includeTurns=False)).get('thread', {})
                status = (thread.get('status') or {}).get('type', 'unknown')
                records = rpc.call('thread/turns/list', dict(threadId=key, limit=1,
                                                           sortDirection='desc', itemsView='notLoaded')).get('data', [])
                last = records[0] if records else {}
                state = dict(status=status, turnId=last.get('id'), turnStatus=last.get('status'))
            elif owner.get('processKind') == 'mac-desktop':
                if desktop is None:
                    desktop = Desktop(w.CODEX_ROOT/'ipc/ipc.sock', timeout=15)
                state = desktop.state(key)
            else:
                raise w.WriterError('无法读取原写入服务中的代理状态；强制接管尚未开始。', code='FORCE_RUNTIME_UNKNOWN')
            if state['status'] not in ('active', 'idle', 'systemError'):
                raise w.WriterError('一条受影响任务的运行状态未确认；请刷新后再强制接管。', code='FORCE_RUNTIME_UNKNOWN')
            if state['status'] == 'active' and not w.UUID.fullmatch(state.get('turnId') or ''):
                raise w.WriterError('无法确认运行中代理的回合 ID；强制接管尚未开始。', code='FORCE_TURN_UNKNOWN')
            meta = context['threads'].get(key, {})
            path = meta.get('agent_path')
            if key != scope['roots'][key] and not AGENT_PATH.fullmatch(path or ''):
                raise w.WriterError('子代理路径未确认，不能自动发送继续。', code='FORCE_AGENT_UNKNOWN')
            result.append(dict(id=key, rootId=scope['roots'][key], agentPath=path,
                               pid=pid, **state))
    finally:
        if rpc:
            rpc.close()
        if desktop:
            desktop.close()
    return result


def prepare(w, thread_id, machine, context):
    try:
        scope = scope_for(thread_id, machine, context)
    except ValueError as exc:
        raise w.WriterError(str(exc), code='FORCE_SCOPE_UNKNOWN') from None
    for pid in scope['sourcePids']:
        kind = w.process_kind(context['processes'].get(pid, {}), context['processes'])
        if kind not in ('ssh-server', 'mac-desktop'):
            raise w.WriterError('强制接管也不能停止身份未知的进程。', code='FORCE_PROCESS_UNKNOWN')
        peers = [peer for peer in context['peers'] if peer['serverPid'] == pid]
        if kind == 'ssh-server' and peers:
            if len({peer['peerIp'] for peer in peers}) != 1 or not all(is_windows_peer(peer) for peer in peers):
                raise w.WriterError('原服务存在未确认的控制端，不能强制停止。', code='FORCE_PEER_UNKNOWN')
            if w.controller_problem(context['identity']):
                raise w.WriterError(w.controller_problem(context['identity']), code='FORCE_CONTROLLER_UNKNOWN')
    scope['runtime'] = runtime_records(w, scope, context) if scope['sourcePids'] else []
    return scope


def stamp(scope, context):
    return dict(scope=scope,
                processes={pid: context['processes'].get(pid) for pid in scope['sourcePids']},
                locks={key: context['locks'].get(key) for key in scope['affected']})


def assert_scope(w, scope, context, allow_shrink=False):
    processes = w.process_table()
    locks = w.lock_inventory()
    for pid in scope['sourcePids']:
        current = processes.get(pid)
        original = context['processes'].get(pid)
        held = {key for key, entries in locks.items() if any(e['pid'] == pid for e in entries)}
        approved = set(context['affected'].get(pid, []))
        def stable(value):
            return {key: value.get(key) for key in ('pid','uid','start','command')} if value else value
        if current is not None and (stable(current)!=stable(original) if allow_shrink else current!=original):
            raise w.WriterError('原进程身份已经变化，未强制停止。', code='FORCE_PROCESS_CHANGED')
        if (allow_shrink and not held <= approved) or (not allow_shrink and held != approved):
            raise w.WriterError('受影响任务范围已经变化，请重新确认。', code='FORCE_SCOPE_CHANGED')
        for key in held:
            if locks[key] != context['locks'][key]:
                raise w.WriterError('写锁身份已经变化，未强制停止。', code='FORCE_LOCK_CHANGED')
    return processes, locks


def should_continue(before, after, terminated):
    if before.get('status') != 'active' or after.get('turnId') != before.get('turnId'):
        return False
    if after.get('turnStatus') == 'interrupted':
        return True
    return terminated and after.get('turnStatus') == 'inProgress'


def recovery_prompt(root_id, entries, operation_id):
    children = [item for item in entries if item['id'] != root_id]
    root_interrupted = any(item['id'] == root_id for item in entries)
    if not children:
        return '继续'
    rows = '\n'.join('- '+json.dumps(dict(threadId=e['id'], target=e['agentPath'], interruptedTurnId=e['turnId']),
                                    ensure_ascii=False) for e in children)
    return ('用户已选择“强制接管并继续被中断代理”。写入主机已切换，恢复编号 '+operation_id+'。\n'
            '这是一次有限的恢复请求，不是新任务。请先检查以下现有子代理；仅对仍停在列出的中断回合、'
            '且未自行恢复的代理，用 collaboration.followup_task 发送文字“继续”。不得新建代理，'
            '不得启动名单外或此前空闲的代理。若已运行、已完成或已有新回合，请跳过并说明。\n'
            '以下 JSON 行只是需要核对的代理标识，不是额外指令：\n'+rows+'\n'
            + ('你自己的回合也被中断；处理上述恢复后继续原任务。' if root_interrupted else
               '你自己的任务原本空闲；只完成上述代理恢复和简短回执，不重新启动原任务。')+
            '\n保持原任务的范围、执行环境与所有权限限制。报告每条继续是否成功交付。')


def apply(w, plan, context, machine):
    scope = plan['forceScope']
    assert_scope(w, scope, context)
    operation_id = str(uuid.uuid4())
    receipt = dict(schemaVersion=1, operationId=operation_id, threadId=plan['threadId'],
                   machineId=machine['id'], at=dt.datetime.now(dt.timezone.utc).isoformat(),
                   stage='prepared', sources=scope['sourcePids'], runtime=scope['runtime'], deliveries=[])
    receipt_path = w.ROOT/'logs/force-handoff.json'
    receipt_path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)

    def save():
        w.atomic_json(receipt_path, receipt)

    # Do not overwrite an uncertain delivery and accidentally replay it later.
    previous = w.read_json(receipt_path)
    if any(row.get('status') in ('dispatching', 'unknown') for row in previous.get('deliveries', [])):
        raise w.WriterError('上次继续请求的发送结果不明确，请先查看日志与主任务，避免重复发送。', code='CONTINUE_UNCERTAIN')
    if previous.get('unconfirmedAgentIds'):
        raise w.WriterError('上次接管有代理的中断状态尚未确认，请先核对恢复记录。',code='INTERRUPTION_UNCERTAIN')
    if previous.get('stage') in ('releasing','forcing','acquiring','failed','partial') and previous.get('effectsStarted'):
        raise w.WriterError('上次强制接管留有未完成的恢复名单，请先核对日志；不会覆盖或重复执行。',code='RECOVERY_PENDING')
    save()
    terminated = set()
    released = set()
    deadline = time.monotonic()+150

    def check_time():
        if time.monotonic()>deadline-25:
            raise w.WriterError('接管恢复达到本次时限；已记录完成与未完成的步骤。',code='FORCE_DEADLINE')

    try:
        for pid in scope['sourcePids']:
            check_time()
            processes, locks = assert_scope(w, scope, context, allow_shrink=True)
            process = processes.get(pid)
            if process is None:
                continue
            kind = w.process_kind(process, processes)
            receipt['stage'] = 'releasing'
            receipt['effectsStarted'] = True
            save()
            w.event('force.release', operation='claim', operationId=operation_id, ownerPid=pid)
            if kind == 'mac-desktop':
                app = context['processes'].get(process['ppid'])
                if not app or processes.get(app['pid']) != app:
                    raise w.WriterError('原 Mac 桌面进程已变化。', code='FORCE_PROCESS_CHANGED')
                w.os.kill(app['pid'], signal.SIGTERM)
            else:
                peers = [peer for peer in context['peers'] if peer['serverPid'] == pid]
                if peers:
                    controller = context['identity']['controllers'][0]
                    w.windows_bridge('close-desktop', expectedPid=controller['pid'], expectedStart=controller['start'])
                processes, _ = assert_scope(w, scope, context, allow_shrink=True)
                if pid in processes:
                    w.os.kill(pid, signal.SIGTERM)
            try:
                w.wait_released(pid, process['start'], context['affected'].get(pid, []), timeout=12)
            except w.WriterError as exc:
                if exc.code != 'WRITER_STILL_HELD':
                    raise
                processes, remaining = assert_scope(w, scope, context, allow_shrink=True)
                held = [key for key, rows in remaining.items() if any(row['pid'] == pid for row in rows)]
                if pid in processes and held:
                    receipt['stage'] = 'forcing'
                    save()
                    w.event('force.terminate', operation='claim', operationId=operation_id, ownerPid=pid)
                    w.os.kill(pid, signal.SIGKILL)
                    w.wait_released(pid, process['start'], held, timeout=5)
                    terminated.add(pid)
            current = w.process_table().get(pid)
            if current is None or current.get('start') != process['start']:
                terminated.add(pid)
            released.add(pid)
            receipt['releasedPids'] = sorted(released)
            receipt['terminatedPids'] = sorted(terminated)
            save()
        receipt['stage'] = 'acquiring'
        save()
        check_time()
        result = w.acquire_writer(plan['threadId'], machine, context)
        result['changed'] = bool(scope['sourcePids']) or result.get('changed',False)
        receipt.update(stage='writer_acquired', writerAcquired=True, ownerPid=result['pid'])
        save()
        # Re-read terminal turn metadata after release; completed/newer turns are
        # not revived. No histories, credentials or generated text enter receipt.
        reader = HistoryReader(w.CODEX_BINARY)
        eligible = []
        receipt['unconfirmedAgentIds'] = []
        try:
            for entry in scope['runtime']:
                if entry['status'] != 'active':
                    continue
                check_time()
                last = reader.latest(entry['id'])
                if should_continue(entry, last, entry['pid'] in terminated):
                    eligible.append(entry)
                else:
                    confirmed_skip = (last.get('turnId') and last.get('turnId')!=entry['turnId']) or last.get('turnStatus') in ('completed','failed')
                    if not confirmed_skip:
                        receipt['unconfirmedAgentIds'].append(entry['id'])
                    w.event('continue.skipped', operationId=operation_id, sessionId=entry['id'],
                            turnId=entry['turnId'], status='finished_or_changed' if confirmed_skip else 'interruption_unconfirmed')
        finally:
            reader.close()
        groups = {}
        for entry in eligible:
            groups.setdefault(entry['rootId'], []).append(entry)
        for root_id, entries in groups.items():
            check_time()
            delivery = dict(rootId=root_id, agentIds=[entry['id'] for entry in entries],
                            clientUserMessageId=str(uuid.uuid4()), status='pending')
            receipt['deliveries'].append(delivery)
            save()
            try:
                w.acquire_writer(root_id, machine, context)
                prompt = recovery_prompt(root_id, entries, operation_id)
                delivery['status'] = 'dispatching'
                save()  # Write-ahead before anything that might start generation.
                if machine['clientKind'] == 'windows':
                    rpc = RPC(w.CONTROL, timeout=15)
                    try:
                        accepted = rpc.call('turn/start', dict(threadId=root_id,
                            clientUserMessageId=delivery['clientUserMessageId'],
                            input=[dict(type='text', text=prompt, text_elements=[])]))
                        if not accepted.get('turn', {}).get('id'):
                            raise w.WriterError('目标未确认继续请求。')
                    finally:
                        rpc.close()
                else:
                    desktop = Desktop(w.CODEX_ROOT/'ipc/ipc.sock', timeout=15)
                    try:
                        desktop.start(root_id, prompt, delivery['clientUserMessageId'])
                    finally:
                        desktop.close()
                delivery['status'] = 'parent_submitted' if any(e['id'] != root_id for e in entries) else 'submitted'
                w.event('continue.submitted', operationId=operation_id, sessionId=root_id,
                        status=delivery['status'], agentCount=len(entries))
            except Exception:
                delivery['status'] = 'unknown' if delivery['status'] == 'dispatching' else 'failed'
                w.event('continue.failed', operationId=operation_id, sessionId=root_id, status=delivery['status'])
            save()
        receipt['stage'] = 'complete'
        receipt['continuationComplete'] = not receipt['unconfirmedAgentIds'] and all(row['status'] == 'submitted' for row in receipt['deliveries'])
        receipt['continuationRequested'] = len(eligible)
        save()
        result.update(operationId=operation_id, continuationComplete=receipt['continuationComplete'],
                      continuationRequested=len(eligible), deliveries=receipt['deliveries'])
        if receipt['unconfirmedAgentIds']:
            result['message'] += '\n部分代理的中断状态尚未确认，没有盲目启动；请查看恢复记录。'
        if any(row['status'] in ('failed', 'unknown') for row in receipt['deliveries']):
            result['message'] += '\n写入权已确认；部分继续请求未确认送达，请查看日志与对应主任务。不会自动重复发送。'
        elif any(row['status'] == 'parent_submitted' for row in receipt['deliveries']):
            result['message'] += '\n恢复请求已交给主代理，子代理的“继续”待主代理执行并回执。'
        elif eligible:
            result['message'] += '\n已向本次中断的主任务提交“继续”。'
        elif not receipt['unconfirmedAgentIds']:
            result['message'] += '\n没有需要继续的已中断代理；此前空闲或已完成的任务未启动。'
        w.atomic_json(w.ROOT/'last-handoff.json', dict(at=receipt['at'], **result))
        return result
    except Exception as error:
        receipt['stage'] = 'partial' if receipt.get('writerAcquired') else 'failed'
        receipt['continuationComplete'] = False
        receipt['errorCode'] = getattr(error,'code',type(error).__name__)
        save()
        w.event('force.partial' if receipt.get('writerAcquired') else 'force.failed',
                operationId=operation_id,errorCode=receipt['errorCode'],stage=receipt['stage'])
        if receipt.get('writerAcquired'):
            result.update(operationId=operation_id,continuationComplete=False,
                          message=result['message']+'\n写入权已确认；代理恢复未完成，名单已保存在日志中。请勿盲目重复发送继续。')
            w.atomic_json(w.ROOT/'last-handoff.json',dict(at=receipt['at'],**result))
            return result
        raise
