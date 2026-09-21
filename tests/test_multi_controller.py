"""Multiple SSH controllers, one tool instance; no live SSH or Codex use."""
import copy
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch

import caller_context
import mac_controller
import runtime_config
import ssh_bridge
import writer_force
import writer_service as writer
import test_writer_service as fixtures
from test_writer_service import MAIN, OTHER, WINDOWS


SPECS = [
    dict(alias='office-pc',platform='windows',backend_alias='mac'),
    dict(alias='travel-mac',platform='mac',backend_alias='work-mac'),
    dict(alias='lab-pc',platform='windows',backend_alias='storage'),
]


class Configuration(unittest.TestCase):
    def test_multiple_platforms_and_legacy_config(self):
        specs=runtime_config.controller_specs({'ssh_controllers':SPECS})
        self.assertEqual([s['id'] for s in specs],['ssh:office-pc','ssh:travel-mac','ssh:lab-pc'])
        self.assertEqual(runtime_config.controller_specs({})[0]['id'],WINDOWS)

    def test_shared_alias_is_rejected(self):
        specs=copy.deepcopy(SPECS)
        specs[1]['peer_aliases']=['travel-mac','office-pc']
        with self.assertRaises(ValueError):
            runtime_config.controller_specs({'ssh_controllers':specs})

    def test_peer_must_map_to_one_controller(self):
        specs=runtime_config.controller_specs({'ssh_controllers':SPECS})
        self.assertEqual(runtime_config.peer_controller_ids({'aliases':['travel-mac']},specs),['ssh:travel-mac'])
        self.assertEqual(len(runtime_config.peer_controller_ids({'aliases':['office-pc','travel-mac']},specs)),2)

    def test_unknown_alias_is_never_contacted(self):
        with patch.object(ssh_bridge.subprocess,'run',side_effect=AssertionError('unexpected SSH')):
            with self.assertRaises(ssh_bridge.BridgeError):
                ssh_bridge.request('identity',alias='unregistered')

    def test_mac_uses_one_shot_stdin_helper(self):
        specs=runtime_config.controller_specs({'ssh_controllers':SPECS})
        response=Mock(returncode=0,stdout='{"schemaVersion":2}',stderr='')
        with patch.object(ssh_bridge,'controller_specs',return_value=specs),patch.object(ssh_bridge.subprocess,'run',return_value=response) as run:
            ssh_bridge.request('identity',alias='travel-mac')
        self.assertEqual(run.call_args.args[0][-2:],['travel-mac','python3 -'])
        self.assertIn('def identity(',run.call_args.kwargs['input'])
        self.assertNotIn('scp',run.call_args.args[0])


class MultipleHosts(unittest.TestCase):
    rpc_call=fixtures.WriterInvariants.rpc_call
    discover_result=fixtures.WriterInvariants.discover_result

    def setUp(self):
        fixtures.WriterInvariants.setUp(self)
        self.stack.enter_context(patch.dict(writer.CONFIG,{'ssh_controllers':SPECS}))
        self.specs=runtime_config.controller_specs({'ssh_controllers':SPECS})
        self.hosts={}
        self.peers=[]
        self.machines=self.machines[:1]
        self.identities={}
        for index,spec in enumerate(self.specs):
            self.hosts[spec['alias']]={'alias':spec['alias'],'endpoints':['192.0.2.'+str(index+10)]}
            self.peers.append(dict(serverPid=100,proxyPid=310+index,sshPid=410+index,peerIp='192.0.2.'+str(index+10),
                                   aliases=[spec['alias']],verifiedAt=1))
            self.machines.append(dict(id=spec['id'],name=spec['alias'],alias=spec['alias'],source='ssh-config',
                                      clientKind='ssh',status='configured',statusLabel='configured',selectable=False))
            # Same PID across different computers must never collide.
            self.identities[spec['alias']]=dict(schemaVersion=2,hostName=spec['alias'],available=True,
                apps=[dict(pid=1000,start='2026-09-21T12:00:00Z')],proxyCount=1,matchedProxyCount=1,
                controllers=[dict(pid=1000,start='2026-09-21T12:00:00Z',name='ChatGPT.exe',proxyPids=[2000])])
        self.stack.enter_context(patch.object(writer,'controller_identity',side_effect=lambda spec,*a,**kw:copy.deepcopy(self.identities[spec['alias']])))

    def test_all_registered_controllers_are_selectable_without_remote_tool_install(self):
        snapshot,context=writer.observe()
        self.assertEqual({m['id'] for m in snapshot['machines'] if m['selectable']}, {'local',*[s['id'] for s in self.specs]})
        row=next(row for row in snapshot['records'] if row['id']==MAIN)
        self.assertEqual(row['ownerState'],'shared')
        self.assertEqual(len(row['ownerMachineIds']),3)
        self.assertIn('travel-mac',row['writerHostName'])

    def test_plan_excludes_selected_target_from_controller_closures(self):
        plan,_,_,_=writer.make_plan(MAIN,'ssh:lab-pc')
        self.assertTrue(plan['canApply'])
        self.assertEqual({row['machineId'] for row in plan['controllerImpacts']},{'ssh:office-pc','ssh:travel-mac'})

    def test_local_takeover_lists_every_connected_controller(self):
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.assertTrue(plan['canApply'])
        self.assertEqual(len(plan['controllerImpacts']),3)

    def test_unregistered_fourth_peer_blocks_all_source_termination(self):
        self.peers.append(dict(serverPid=100,proxyPid=999,sshPid=999,peerIp='192.0.2.99',aliases=['unknown'],verifiedAt=1))
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.assertFalse(plan['canApply'])
        self.kill.assert_not_called()

    def test_changed_remote_process_invalidates_confirmation(self):
        plan,_,_,_=writer.make_plan(MAIN,'local')
        self.identities['travel-mac']['controllers'][0]['start']='2026-09-21T12:01:00Z'
        changed,_,_,_=writer.make_plan(MAIN,'local')
        self.assertNotEqual(plan['revision'],changed['revision'])

    def test_target_choice_keeps_its_shared_server_alive(self):
        plan,_,_,_=writer.make_plan(MAIN,'ssh:lab-pc')
        closed=[]
        def close(operation,spec,**kwargs):
            self.assertEqual(operation,'close-desktop')
            self.assertEqual(kwargs['expectedPid'],1000)
            self.assertEqual(kwargs['expectedHostName'],spec['alias'])
            self.assertNotEqual(spec['alias'],'lab-pc')
            closed.append(spec['alias'])
            self.peers[:]=[peer for peer in self.peers if spec['alias'] not in peer['aliases']]
            return {'stopped':1000}
        with tempfile.TemporaryDirectory() as root,patch.object(writer,'ROOT',Path(root)), \
             patch.object(writer,'controller_bridge',side_effect=close),patch.object(writer,'atomic_json'), \
             patch('discovery_adapter.peer_connections',side_effect=lambda:copy.deepcopy(self.peers)):
            result=writer.claim(MAIN,'ssh:lab-pc',plan['revision'])
        self.assertTrue(result['ok'])
        self.assertEqual(set(closed),{'office-pc','travel-mac'})
        self.assertEqual(result['pid'],100)
        self.kill.assert_not_called()

    def test_acquisition_cannot_claim_one_host_while_others_remain(self):
        snapshot,context=writer.observe()
        target=next(m for m in snapshot['machines'] if m['id']=='ssh:lab-pc')
        with patch('discovery_adapter.peer_connections',return_value=self.peers),self.assertRaises(writer.WriterError) as error:
            writer.acquire_writer(MAIN,target,context)
        self.assertEqual(error.exception.code,'DESTINATION_SHARED')

    def test_force_plan_preserves_target_server_and_lists_source_hosts(self):
        snapshot,context=writer.observe()
        target=next(m for m in snapshot['machines'] if m['id']=='ssh:lab-pc')
        with patch.object(writer_force,'runtime_records',return_value=[]):
            scope=writer_force.prepare(writer,MAIN,target,context)
        self.assertEqual(scope['preservedPids'],[100])
        self.assertEqual({r['machineId'] for r in scope['controllerImpacts']},{'ssh:office-pc','ssh:travel-mac'})

    def test_unowned_task_still_detaches_other_destination_controllers(self):
        plan,_,_,_=writer.make_plan(OTHER,'ssh:travel-mac')
        self.assertTrue(plan['canApply'])
        self.assertEqual({r['machineId'] for r in plan['controllerImpacts']},{'ssh:office-pc','ssh:lab-pc'})
        closed=[]
        def close(operation,spec,**kwargs):
            closed.append(spec['alias'])
            self.peers[:]=[peer for peer in self.peers if spec['alias'] not in peer['aliases']]
            return {'stopped':1000}
        base=self.rpc.call.side_effect
        def rpc(method,params):
            if method=='thread/resume':
                self.locks[OTHER]=[dict(pid=100,inode=999)]
                return {}
            return base(method,params)
        with tempfile.TemporaryDirectory() as root,patch.object(writer,'ROOT',Path(root)), \
             patch.object(writer,'controller_bridge',side_effect=close),patch.object(writer,'atomic_json'), \
             patch.object(self.rpc,'call',side_effect=rpc), \
             patch('discovery_adapter.peer_connections',side_effect=lambda:copy.deepcopy(self.peers)):
            result=writer.claim(OTHER,'ssh:travel-mac',plan['revision'])
        self.assertTrue(result['ok'])
        self.assertEqual(set(closed),{'office-pc','lab-pc'})
        self.kill.assert_not_called()

    def test_local_source_also_lists_destination_side_closures(self):
        self.locks[MAIN]=[dict(pid=200,inode=1)]
        plan,_,_,_=writer.make_plan(MAIN,'ssh:lab-pc')
        self.assertTrue(plan['canApply'])
        self.assertEqual({r['machineId'] for r in plan['controllerImpacts']},{'ssh:office-pc','ssh:travel-mac'})
        self.assertGreaterEqual(len(plan['impacted']),2)

    def test_force_already_unique_target_does_not_invent_source_release(self):
        self.peers=self.peers[-1:]
        snapshot,context=writer.observe()
        target=next(m for m in snapshot['machines'] if m['id']=='ssh:lab-pc')
        scope=writer_force.scope_for(MAIN,target,context)
        self.assertEqual(scope['sourcePids'],[])

    def test_same_endpoint_for_two_declared_hosts_is_not_a_caller_identity(self):
        self.hosts['travel-mac']['endpoints']=self.hosts['office-pc']['endpoints']
        value=dict(platform='windows',hostName='office-pc',observedAt=1000)
        caller,identity=caller_context.resolve_context(value,'192.0.2.10 55 192.0.2.1 22',self.hosts,'local',now=1001,specs=self.specs)
        self.assertFalse(caller['verified'])
        self.assertIsNone(identity)


class MacHelper(unittest.TestCase):
    def setUp(self):
        self.rows=[dict(pid=10,ppid=1,uid=501,start='2026-09-21T12:00:00+00:00',command='/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'),
                   dict(pid=20,ppid=10,uid=501,start='same',command='/usr/bin/ssh work-mac codex app-server proxy')]

    def test_mac_controller_is_exactly_correlated(self):
        value=mac_controller.identity(self.rows,'work-mac','travel-mac',501)
        self.assertEqual(value['detection'],'confirmed')
        self.assertEqual(value['controllers'][0]['pid'],10)
        self.assertEqual(mac_controller.identity(self.rows,'different-host','travel-mac',501)['detection'],'proxy_not_connected')

    def test_mac_close_rechecks_host_pid_and_start(self):
        request=dict(operation='close-desktop',backendAlias='work-mac',expectedPid=10,
                     expectedStart=self.rows[0]['start'],expectedHostName='travel-mac')
        stop=Mock()
        self.assertEqual(mac_controller.execute(request,self.rows,'travel-mac',501,stop),{'stopped':10})
        stop.assert_called_once_with(10,signal.SIGTERM)
        for field,value in [('expectedPid',11),('expectedStart','changed'),('expectedHostName','another-mac')]:
            stop.reset_mock()
            with self.subTest(field=field),self.assertRaises(ValueError):
                mac_controller.execute(dict(request,**{field:value}),self.rows,'travel-mac',501,stop)
            stop.assert_not_called()

    def test_foreign_user_desktop_is_not_a_controller(self):
        self.assertEqual(mac_controller.identity(self.rows,'work-mac','travel-mac',502)['detection'],'desktop_absent')
