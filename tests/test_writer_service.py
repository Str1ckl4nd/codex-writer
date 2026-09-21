"""Pure regression checks: never connect to, resume, or stop a real service."""

import copy
from contextlib import ExitStack
import os
import unittest
from unittest.mock import Mock, mock_open, patch

import writer_service as writer


MAIN = '00000000-0000-0000-0000-000000000001'
OTHER = '00000000-0000-0000-0000-000000000002'
ASTRA = '00000000-0000-0000-0000-000000000003'
WINDOWS = 'ssh:windows-pc'
BINARY = '/Applications/ChatGPT.app/Contents/Resources/codex'


class WriterInvariants(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(writer.CONFIG, {'enable_handoff': True}))
        self.stack.enter_context(patch.object(writer, 'EXCLUDED', {ASTRA}))
        self.addCleanup(self.stack.close)
        self.processes = {
            100: dict(pid=100, ppid=1, uid=os.getuid(), start='original-start',
                      command=BINARY + ' -c features.code_mode_host=true app-server --listen unix://'),
            200: dict(pid=200, ppid=20, uid=os.getuid(), start='desktop-start',
                      command=BINARY + ' -c features.code_mode_host=true app-server --analytics-default-enabled'),
            20: dict(pid=20, ppid=1, uid=os.getuid(), start='app-start',
                     command='/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'),
        }
        self.locks = {MAIN: [dict(pid=100, inode=1)], ASTRA: [dict(pid=100, inode=2)]}
        self.threads = {
            key: dict(id=key, name=name, created_at=1, updated_at=2, thread_source='user',
                      archived=False, agent_path=None)
            for key, name in [(MAIN, 'Main task'), (OTHER, 'Other task'), (ASTRA, 'Hidden fixture')]
        }
        self.statuses = {MAIN: 'idle', ASTRA: 'active'}
        self.peers = [dict(serverPid=100, proxyPid=301, sshPid=302, peerIp='192.0.2.1',
                           aliases=['windows-pc'], verifiedAt=1)]
        self.hosts = {'windows-pc': {'alias': 'windows-pc', 'endpoints': ['192.0.2.1']}}
        self.identity = dict(hostName='windows-pc', available=True, proxyCount=1,
                             schemaVersion=2, apps=[dict(pid=300, start='windows-start')],
                             controllers=[dict(pid=300,start='windows-start',name='ChatGPT.exe',proxyPids=[301])])
        self.machines = [
            dict(id='local', name='mac-pc.local', source='local', clientKind='mac',
                 status='online', statusLabel='本机在线', selectable=True),
            dict(id=WINDOWS, name='windows-pc', source='ssh-config', alias='windows-pc', clientKind='ssh',
                 status='configured', statusLabel='SSH 已配置，未核验', selectable=False),
        ]
        self.rpc = Mock()
        self.stack.enter_context(patch.object(writer,'event'))
        self.rpc.call.side_effect = self.rpc_call
        for name, value in [('process_table', self.processes), ('lock_inventory', self.locks),
                            ('metadata', self.threads), ('socket_server', 100),
                            ('windows_identity', self.identity), ('read_json', {})]:
            self.stack.enter_context(patch.object(writer, name, return_value=value))
        self.stack.enter_context(patch.object(writer, 'RPC', return_value=self.rpc))
        self.discover_mock = self.stack.enter_context(
            patch.object(writer, 'discover', side_effect=self.discover_result))
        # Unexpected operations fail immediately; no real subprocess/network/write fallback.
        for name in ('command', 'windows_bridge', 'atomic_json'):
            self.stack.enter_context(patch.object(writer, name, side_effect=AssertionError(name + ' forbidden')))
        self.kill = self.stack.enter_context(patch.object(writer.os, 'kill', side_effect=AssertionError('kill forbidden')))

    def rpc_call(self, method, params):
        if method == 'thread/loaded/list':
            return {'data': list(self.statuses)}
        if method == 'thread/read':
            status = self.statuses[params['threadId']]
            if isinstance(status, Exception):
                raise status
            return {'thread': {'status': {'type': status}}}
        raise AssertionError('RPC mutation or unexpected method: ' + method)

    def discover_result(self, **kwargs):
        return dict(machines=copy.deepcopy(self.machines), peerConnections=copy.deepcopy(self.peers),
                    diagnostics=[], sshHosts=copy.deepcopy(self.hosts), discoveryMode='ssh-only')

    def test_current_bundled_binary_classifies_server_and_desktop(self):
        self.assertEqual(writer.process_kind(self.processes[100], self.processes), 'ssh-server')
        self.assertEqual(writer.process_kind(self.processes[200], self.processes), 'mac-desktop')
        proxy = dict(self.processes[100], command=BINARY + ' app-server proxy')
        foreign = dict(self.processes[100], uid=os.getuid() + 1)
        self.assertEqual(writer.process_kind(proxy, self.processes), 'unknown')
        self.assertEqual(writer.process_kind(foreign, self.processes), 'unknown')

    def test_excluded_astra_stays_in_complete_process_impact(self):
        plan, snapshot, _, _ = writer.make_plan(MAIN, 'local')
        self.assertNotIn(ASTRA, {row['id'] for row in snapshot['records']})
        self.assertEqual({row['id'] for row in plan['impacted']}, {MAIN, ASTRA})
        self.assertIn('Hidden fixture（运行中）', plan['message'])
        self.assertTrue(plan['canApply'])
        self.assertIn('其他远程主机将断开', plan['message'])

    def test_multiple_peer_endpoints_never_claim_windows_ownership(self):
        self.peers.append(dict(serverPid=100, proxyPid=401, sshPid=402, peerIp='192.0.2.2',
                               aliases=['other-host'], verifiedAt=1))
        plan, snapshot, _, _ = writer.make_plan(MAIN, 'local')
        row = next(row for row in snapshot['records'] if row['id'] == MAIN)
        self.assertEqual(row['ownerState'], 'ambiguous')
        self.assertIsNone(row['ownerMachineId'])
        self.assertFalse(plan['canApply'])

    def test_online_identity_without_desktop_is_not_selectable(self):
        self.identity['apps'] = []
        self.identity['controllers'] = []
        plan, snapshot, _, _ = writer.make_plan(OTHER, WINDOWS)
        target = next(machine for machine in snapshot['machines'] if machine['id'] == WINDOWS)
        self.assertFalse(target['selectable'])
        self.assertFalse(plan['canApply'])

    def test_timeout_reports_connection_failure_not_process_uniqueness(self):
        self.identity.update(available=False,apps=[],controllers=[],error='连接 Windows 的 SSH 主通道超时，尚未取得进程信息。',errorCode='MASTER_CONNECT_TIMEOUT')
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.assertFalse(plan['canApply'])
        self.assertIn('SSH 主通道超时',plan['reason'])
        self.assertNotIn('唯一桌面进程',plan['reason'])

    def test_multiple_desktops_one_correlated_controller_is_allowed(self):
        self.identity['apps'].append(dict(pid=999,start='unrelated-desktop'))
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.assertTrue(plan['canApply'])

    def test_multiple_correlated_controllers_are_not_closed(self):
        self.identity['controllers'].append(dict(pid=999,start='second-controller',proxyPids=[901]))
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.assertFalse(plan['canApply'])
        self.assertIn('2 个桌面客户端',plan['reason'])

    def test_running_desktop_without_proxy_is_specific_failure(self):
        self.identity.update(controllers=[],proxyCount=0)
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.assertFalse(plan['canApply'])
        self.assertIn('尚未找到',plan['reason'])

    def test_one_unreadable_thread_does_not_disable_ssh_discovery(self):
        self.statuses[MAIN] = writer.RPCError('thread/read: not-found')
        snapshot, _ = writer.observe()
        self.assertNotIn('rpc', self.discover_mock.call_args.kwargs)
        self.assertEqual(snapshot['discoveryMode'], 'ssh-only')
        self.assertNotIn('accountDiscovery', snapshot)
        rows = {row['id']: row for row in snapshot['records']}
        self.assertEqual(rows[MAIN]['status'], 'unknown')
        self.assertIn('thread_read_' + MAIN, {item['code'] for item in snapshot['diagnostics']})

    def test_revision_binds_ownership_scope_activity_and_destination(self):
        snapshot, context = writer.observe()
        machine = next(machine for machine in snapshot['machines'] if machine['id'] == WINDOWS)
        baseline = writer.revision_for(MAIN, machine, context)

        def changed_pid(value):
            value['owners'][MAIN]['pid'] = 101

        def changed_start(value):
            value['processes'][100]['start'] = 'replacement-start'

        def changed_inode(value):
            value['locks'][MAIN][0]['inode'] = 9

        def changed_impact(value):
            value['affected'][100].append(OTHER)

        def changed_activity(value):
            original = value['record']
            value['record'] = lambda key: dict(original(key), status='active' if key == MAIN else original(key)['status'])

        def changed_destination(value):
            value['serverPid'] = 200

        def changed_endpoint(value):
            value['peers'][0]['peerIp'] = '192.0.2.99'

        for change in (changed_pid, changed_start, changed_inode, changed_impact,
                       changed_activity, changed_destination, changed_endpoint):
            with self.subTest(change=change.__name__):
                altered = copy.deepcopy(context)
                change(altered)
                self.assertNotEqual(baseline, writer.revision_for(MAIN, machine, altered))
        timestamp_only = copy.deepcopy(context)
        timestamp_only['peers'][0]['verifiedAt'] += 30
        self.assertEqual(baseline, writer.revision_for(MAIN, machine, timestamp_only))

    def test_missing_or_stale_revision_rejects_before_any_effect(self):
        plan = dict(canApply=True, reason='', revision='current-revision', changed=True)
        with patch('builtins.open', mock_open()), patch.object(writer.fcntl, 'flock'), \
                patch.object(writer, 'make_plan', return_value=(plan, {}, {}, {})), \
                patch.object(writer, 'process_table', side_effect=AssertionError('unexpected post-plan read')):
            for revision in ('', None, 'old-revision'):
                with self.subTest(revision=revision), self.assertRaises(writer.WriterError):
                    writer.claim(MAIN, WINDOWS, revision)
        self.kill.assert_not_called()
        self.rpc.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
