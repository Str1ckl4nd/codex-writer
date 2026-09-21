"""Pure fixtures. Run in the isolated VM; never load, stop or resume real tasks."""
import copy
import json
from pathlib import Path
import signal
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import writer_force as force
import writer_service as writer
from writer_desktop import runtime_from_snapshot, project_payload, Desktop
from writer_history import HistoryReader


ROOT = '00000000-0000-0000-0000-000000000001'
CHILD = '00000000-0000-0000-0000-000000000002'
IDLE = '00000000-0000-0000-0000-000000000003'
NEW = '00000000-0000-0000-0000-000000000004'
TURN = '10000000-0000-0000-0000-000000000001'
CHILD_TURN = '10000000-0000-0000-0000-000000000002'


def fixture():
    threads = {
        ROOT: dict(thread_source='user', name='Parent'),
        CHILD: dict(thread_source='subagent', agent_path='/root/worker',
                    source=json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': ROOT}}})),
        IDLE: dict(thread_source='user', name='Idle'),
    }
    processes = {100: dict(pid=100, ppid=10, uid=501, start='old', command='verified-server')}
    locks = {key: [dict(pid=100, inode=index+1)] for index, key in enumerate(threads)}
    owners = {key: dict(pid=100, ownerMachineId='windows', processKind='ssh-server', ownerState='observed') for key in threads}
    return dict(threads=threads, processes=processes, locks=locks, owners=owners,
                affected={100: list(threads)}, peers=[], serverPid=100, identity={})


class PureRules(unittest.TestCase):
    def test_parent_and_descendants(self):
        ctx = fixture()
        self.assertEqual(force.root_of(CHILD, ctx['threads']), ROOT)
        self.assertEqual(force.family_of(ROOT, ctx['threads']), [ROOT, CHILD])

    def test_missing_parent_fails_closed(self):
        ctx = fixture()
        del ctx['threads'][ROOT]
        with self.assertRaises(ValueError):
            force.root_of(CHILD, ctx['threads'])

    def test_cycle_fails_closed(self):
        ctx = fixture()
        ctx['threads'][ROOT]['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': CHILD}}}
        with self.assertRaises(ValueError):
            force.root_of(CHILD, ctx['threads'])

    def test_primary_already_local_still_finds_remote_child(self):
        ctx = fixture()
        ctx['owners'][ROOT].update(pid=200, ownerMachineId='local')
        ctx['affected'][100].remove(ROOT)
        scope = force.scope_for(ROOT, {'id': 'local'}, ctx)
        self.assertEqual(scope['sourcePids'], [100])
        self.assertEqual(set(scope['affected']), {CHILD, IDLE})

    def test_idle_and_completed_or_newer_turns_not_restarted(self):
        before = dict(status='active', turnId=TURN)
        self.assertTrue(force.should_continue(before, dict(turnId=TURN, turnStatus='interrupted'), False))
        self.assertTrue(force.should_continue(before, dict(turnId=TURN, turnStatus='inProgress'), True))
        self.assertFalse(force.should_continue(before, dict(turnId=TURN, turnStatus='inProgress'), False))
        for status in ('completed', 'failed'):
            self.assertFalse(force.should_continue(before, dict(turnId=TURN, turnStatus=status), True))
        self.assertFalse(force.should_continue(before, dict(turnId=CHILD_TURN, turnStatus='interrupted'), True))
        self.assertFalse(force.should_continue(dict(before, status='idle'), dict(turnId=TURN, turnStatus='interrupted'), True))

    def test_child_recovery_is_parent_controlled_and_scope_bound(self):
        child = dict(id=CHILD, rootId=ROOT, agentPath='/root/worker', turnId=CHILD_TURN)
        prompt = force.recovery_prompt(ROOT, [child], 'receipt-id')
        self.assertIn('collaboration.followup_task', prompt)
        self.assertIn('不得新建代理', prompt)
        self.assertIn('不重新启动原任务', prompt)
        self.assertIn(CHILD, prompt)
        self.assertNotIn(IDLE, prompt)
        self.assertEqual(force.recovery_prompt(ROOT, [dict(id=ROOT)], 'id'), '继续')

    def test_desktop_snapshot_reads_only_runtime(self):
        state = runtime_from_snapshot(dict(threadRuntimeStatus={'type': 'active'},
                     turns=[dict(turnId=TURN, status='inProgress', items=[{'secret': 'never returned'}])]))
        self.assertEqual(state['turnId'], TURN)
        self.assertEqual(state['status'], 'active')
        self.assertNotIn('secret', json.dumps(state))

    def test_stream_projection_drops_full_history_and_preserves_runtime(self):
        payload = dict(type='broadcast', method='thread-stream-state-changed',version=11,
                       sourceClientId='owner',params=dict(conversationId=ROOT,hostId='local',
                       change=dict(type='snapshot',conversationState=dict(threadRuntimeStatus={'type':'active'},
                       turns=[dict(turnId=TURN,status='inProgress',items=[dict(text='隐私\\"内容'*600000)])]))))
        encoded = json.dumps(payload,ensure_ascii=False).encode()
        projected = project_payload(encoded[i:i+777] for i in range(0,len(encoded),777))
        self.assertNotIn('隐私', json.dumps(projected,ensure_ascii=False))
        state = runtime_from_snapshot(projected['params']['change']['conversationState'])
        self.assertEqual(state['status'],'active')
        self.assertEqual(state['turnId'],TURN)
        self.assertEqual(projected['sourceClientId'],'owner')

    def test_missing_runtime_is_unknown_not_idle(self):
        self.assertEqual(runtime_from_snapshot({})['status'],'unknown')

    def test_canonical_history_active_turn_is_projected(self):
        state = dict(threadRuntimeStatus={'type':'active'}, turns=[],turnHistory=dict(kind='canonical',
            history=dict(entitiesByKey={'turn:'+TURN:dict(turnId=TURN,status='inProgress',items=[dict(text='private')])},
                         islands=[dict(entries=[dict(value='turn:'+TURN)])])))
        payload = dict(type='broadcast',method='thread-stream-state-changed',version=11,
                       params=dict(change=dict(type='snapshot',conversationState=state)))
        projected = project_payload([json.dumps(payload).encode()])
        runtime = runtime_from_snapshot(projected['params']['change']['conversationState'])
        self.assertEqual(runtime['turnId'],TURN)
        self.assertEqual(runtime['status'],'active')
        self.assertNotIn('private',json.dumps(projected))

    def test_ambiguous_active_turns_are_not_guessed(self):
        state = runtime_from_snapshot(dict(threadRuntimeStatus={'type':'active'},
                     turns=[dict(turnId=TURN,status='inProgress'),dict(turnId=CHILD_TURN,status='inProgress')]))
        self.assertIsNone(state['turnId'])

    def test_desktop_start_preserves_settings_and_sends_user_input(self):
        desktop=object.__new__(Desktop)
        desktop.owner=Mock(return_value='owner')
        desktop.request=Mock(return_value={'result':{'result':{'turnId':TURN}}})
        desktop.start(ROOT,'继续','message-id')
        call=desktop.request.call_args
        self.assertEqual(call.args[0],'thread-follower-start-turn')
        self.assertEqual(call.args[1]['turnStart']['context'],{'inheritThreadSettings':True})
        self.assertEqual(call.args[1]['turnStart']['request']['input'][0]['text'],'继续')
        self.assertEqual(call.kwargs['version'],2)

    def test_history_reader_rejects_writes_without_opening_transport(self):
        reader = object.__new__(HistoryReader)
        with self.assertRaises(writer.RPCError):
            reader.call('turn/start', {})

    def test_released_lock_succeeds_while_old_process_is_alive(self):
        with patch.object(writer, 'lock_inventory', return_value={}), \
             patch.object(writer, 'process_table', side_effect=AssertionError('should not wait for service exit')):
            writer.wait_released(100, 'old', [ROOT])

    def test_new_holder_does_not_count_as_old_writer(self):
        with patch.object(writer, 'lock_inventory', return_value={ROOT: [dict(pid=200, inode=3)]}):
            writer.wait_released(100, 'old', [ROOT])

    def test_force_revision_includes_turn_scope_and_opt_in(self):
        ctx = fixture()
        ctx['record'] = lambda key: dict(status='active')
        machine = dict(id='local', clientKind='mac', selectable=True)
        scope = force.scope_for(ROOT, machine, ctx)
        scope['runtime'] = [dict(id=ROOT, turnId=TURN, status='active')]
        ordinary = writer.revision_for(ROOT, machine, ctx)
        opted = writer.revision_for(ROOT, machine, ctx, scope)
        self.assertNotEqual(ordinary, opted)
        scope['runtime'][0]['turnId'] = CHILD_TURN
        self.assertNotEqual(opted, writer.revision_for(ROOT, machine, ctx, scope))


class ForcedWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = fixture()
        self.processes = copy.deepcopy(self.context['processes'])
        self.locks = copy.deepcopy(self.context['locks'])
        self.killed = []
        self.gentle_releases = True
        self.extra_lock_on_term = False
        self.machine = dict(id='local', name='mac.local', clientKind='mac')
        scope = force.scope_for(ROOT, self.machine, self.context)
        scope['runtime'] = [dict(id=ROOT, rootId=ROOT, agentPath=None, pid=100, status='active', turnId=TURN),
                            dict(id=CHILD, rootId=ROOT, agentPath='/root/worker', pid=100, status='active', turnId=CHILD_TURN),
                            dict(id=IDLE, rootId=IDLE, agentPath=None, pid=100, status='idle', turnId=None)]
        self.plan = dict(threadId=ROOT, forceScope=scope)
        self.w = types.SimpleNamespace(
            ROOT=Path(self.temp.name), CODEX_ROOT=Path('/fixture-codex'), CONTROL=Path('/fixture-control'),
            CODEX_BINARY=Path('/never-run-codex'), WriterError=writer.WriterError,
            process_table=lambda: copy.deepcopy(self.processes),
            lock_inventory=lambda: copy.deepcopy(self.locks),
            process_kind=lambda process, all_processes: 'ssh-server',
            os=types.SimpleNamespace(kill=self.kill), atomic_json=self.save, read_json=self.read,
            event=Mock(), windows_bridge=Mock(side_effect=AssertionError('no Windows close for orphan')),
            wait_released=self.wait_released, acquire_writer=self.acquire)
        self.reader = Mock()
        self.reader.latest.side_effect = lambda key: dict(turnId=TURN if key == ROOT else CHILD_TURN, turnStatus='interrupted')
        self.desktop = Mock()
        self.history_patch = patch.object(force, 'HistoryReader', return_value=self.reader)
        self.desktop_patch = patch.object(force, 'Desktop', return_value=self.desktop)
        self.history_patch.start()
        self.desktop_patch.start()
        self.addCleanup(self.history_patch.stop)
        self.addCleanup(self.desktop_patch.stop)

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def read(self, path):
        return json.loads(path.read_text()) if path.exists() else {}

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        if self.extra_lock_on_term and sig == signal.SIGTERM:
            self.locks[NEW] = [dict(pid=100, inode=99)]
        elif self.gentle_releases or sig == signal.SIGKILL:
            self.locks = {key: rows for key, rows in self.locks.items() if not any(row['pid'] == pid for row in rows)}
            self.processes.pop(pid, None)

    def wait_released(self, pid, start, ids, timeout):
        if any(any(row['pid'] == pid for row in self.locks.get(key, [])) for key in ids):
            raise writer.WriterError('held', code='WRITER_STILL_HELD')

    def acquire(self, thread_id, machine, context):
        if any(row['pid'] == 100 for rows in self.locks.values() for row in rows):
            raise AssertionError('acquired before complete source release')
        return dict(ok=True, changed=True, pid=200, threadId=thread_id, machineId='local', message='writer acquired')

    def test_gentle_shutdown_does_not_force_kill(self):
        result = force.apply(self.w, self.plan, self.context, self.machine)
        self.assertEqual(self.killed, [(100, signal.SIGTERM)])
        self.assertTrue(result['ok'])
        self.assertFalse(result['continuationComplete'])
        self.desktop.start.assert_called_once()
        self.assertIn(CHILD, self.desktop.start.call_args.args[1])
        self.assertNotIn(IDLE, self.desktop.start.call_args.args[1])
        self.assertEqual(result['deliveries'][0]['status'], 'parent_submitted')
        self.reader.close.assert_called_once()

    def test_kill_only_after_soft_close_still_holds_locks(self):
        self.gentle_releases = False
        result = force.apply(self.w, self.plan, self.context, self.machine)
        self.assertEqual(self.killed, [(100, signal.SIGTERM), (100, signal.SIGKILL)])
        self.assertTrue(result['ok'])

    def test_pid_reuse_never_kills(self):
        self.processes[100]['start'] = 'new-process'
        with self.assertRaises(writer.WriterError):
            force.apply(self.w, self.plan, self.context, self.machine)
        self.assertEqual(self.killed, [])

    def test_new_task_scope_never_force_kills(self):
        self.gentle_releases = False
        self.extra_lock_on_term = True
        with self.assertRaises(writer.WriterError):
            force.apply(self.w, self.plan, self.context, self.machine)
        self.assertEqual(self.killed, [(100, signal.SIGTERM)])
        self.desktop.start.assert_not_called()

    def test_process_reparent_after_shutdown_is_not_pid_reuse(self):
        self.processes[100]['ppid'] = 1
        force.assert_scope(self.w, self.plan['forceScope'], self.context, allow_shrink=True)

    def test_completed_agents_receive_no_continue(self):
        self.reader.latest.side_effect = lambda key: dict(turnId=TURN if key == ROOT else CHILD_TURN, turnStatus='completed')
        result = force.apply(self.w, self.plan, self.context, self.machine)
        self.assertTrue(result['continuationComplete'])
        self.desktop.start.assert_not_called()

    def test_read_failure_does_not_undo_writer_success(self):
        self.reader.latest.side_effect = writer.RPCError('unavailable')
        result = force.apply(self.w, self.plan, self.context, self.machine)
        self.assertTrue(result['ok'])
        self.assertFalse(result['continuationComplete'])
        self.assertEqual(self.read(self.w.ROOT/'logs/force-handoff.json')['stage'], 'partial')
        self.desktop.start.assert_not_called()

    def test_missing_turn_is_pending_not_falsely_complete(self):
        self.reader.latest.side_effect = lambda key: dict(turnId=None,turnStatus=None)
        result = force.apply(self.w,self.plan,self.context,self.machine)
        self.assertTrue(result['ok'])
        self.assertFalse(result['continuationComplete'])
        self.desktop.start.assert_not_called()
        receipt = self.read(self.w.ROOT/'logs/force-handoff.json')
        self.assertEqual(set(receipt['unconfirmedAgentIds']),{ROOT,CHILD})

    def test_unknown_delivery_is_recorded_before_send_and_never_replayed(self):
        observed = []
        def fail(*args):
            observed.append(self.read(self.w.ROOT/'logs/force-handoff.json')['deliveries'][0]['status'])
            raise TimeoutError('response lost')
        self.desktop.start.side_effect = fail
        result = force.apply(self.w, self.plan, self.context, self.machine)
        self.assertTrue(result['ok'])
        self.assertEqual(observed, ['dispatching'])
        self.assertEqual(result['deliveries'][0]['status'], 'unknown')
        with self.assertRaises(writer.WriterError):
            force.apply(self.w, self.plan, self.context, self.machine)
        self.desktop.start.assert_called_once()

    def test_receipt_contains_only_metadata_not_the_prompt(self):
        force.apply(self.w, self.plan, self.context, self.machine)
        text = (self.w.ROOT/'logs/force-handoff.json').read_text()
        self.assertNotIn('collaboration.followup_task', text)
        self.assertNotIn('继续', text)


if __name__ == '__main__':
    unittest.main()
