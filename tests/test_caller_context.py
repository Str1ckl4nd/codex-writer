"""Host-role fixtures, run only in the isolated VM."""
import argparse
import base64
import copy
import json
import os
import unittest
from unittest.mock import Mock, patch

import caller_context as caller
import writer_service as writer
import test_writer_service as fixtures
from test_writer_service import MAIN, WINDOWS


HOSTS={'windows-pc':{'endpoints':['192.0.2.1']}}
NAME='windows-workstation'


def context():
    return dict(schemaVersion=1,platform='windows',hostName=NAME,observedAt=1000,
                identity=dict(schemaVersion=2,hostName=NAME,apps=[dict(pid=300,start='2026-09-17T09:00:00Z',name='ChatGPT.exe')],
                    controllers=[dict(pid=300,start='2026-09-17T09:00:00Z',name='ChatGPT.exe',proxyPids=[301])],
                    proxyCount=1,matchedProxyCount=1,detection='confirmed'))


class CallerRules(unittest.TestCase):
    def test_windows_identity_is_local_not_the_storage_host(self):
        info,identity=caller.resolve_context(context(),'192.0.2.1 55555 192.0.2.2 22',HOSTS,'mac.local',now=1001)
        self.assertEqual(info['hostName'],NAME)
        self.assertEqual(info['platform'],'windows')
        self.assertTrue(info['verified'])
        self.assertTrue(identity['available'])

    def test_unknown_ssh_peer_cannot_supply_controller_identity(self):
        info,identity=caller.resolve_context(context(),'192.0.2.99 555 192.0.2.2 22',HOSTS,'mac.local',now=1001)
        self.assertFalse(info['verified'])
        self.assertIsNone(identity)

    def test_stale_context_cannot_supply_controller_identity(self):
        info,identity=caller.resolve_context(context(),'192.0.2.1 55 192.0.2.2 22',HOSTS,'mac.local',now=2000)
        self.assertFalse(info['verified'])
        self.assertIsNone(identity)

    def test_local_windows_failure_is_not_machine_offline(self):
        value=context()
        value['identity']=None
        info,identity=caller.resolve_context(value,'192.0.2.1 55 192.0.2.2 22',HOSTS,'mac.local',now=1001)
        self.assertTrue(info['verified'])
        self.assertFalse(identity['available'])
        self.assertEqual(identity['errorCode'],'LOCAL_IDENTITY_UNAVAILABLE')

    def test_context_roundtrip_preserves_actual_lan_hostname(self):
        value=context()
        value['identity']['secret']='not to persist'
        encoded=base64.b64encode(json.dumps(value,ensure_ascii=False).encode()).decode()
        decoded=caller.decode_context(encoded)
        self.assertEqual(decoded['hostName'],NAME)
        self.assertNotIn('secret',decoded['identity'])

    def test_bad_context_is_rejected(self):
        for value in ('not-base64','e30=',base64.b64encode(b'[]').decode()):
            with self.subTest(value=value),self.assertRaises(argparse.ArgumentTypeError):
                caller.decode_context(value)

    def test_missing_remote_context_does_not_claim_mac_is_local(self):
        info,_=caller.resolve_context(None,'192.0.2.1 5 192.0.2.2 22',HOSTS,'mac.local')
        self.assertEqual(info['platform'],'remote')
        self.assertNotEqual(info['hostName'],'mac.local')

    def test_native_mac_context_is_explicit(self):
        info,_=caller.resolve_context(dict(platform='mac'),'',HOSTS,'mac.local')
        self.assertEqual(info['machineId'],'local')
        self.assertEqual(info['hostName'],'mac.local')


class HostRoleIntegration(unittest.TestCase):
    setUp=fixtures.WriterInvariants.setUp
    rpc_call=fixtures.WriterInvariants.rpc_call
    discover_result=fixtures.WriterInvariants.discover_result
    def test_configured_host_becomes_selectable_only_with_ssh_process_proof(self):
        snapshot,_=writer.observe(client_context={'platform':'mac'})
        target=next(m for m in snapshot['machines'] if m['id']==WINDOWS)
        self.assertTrue(target['selectable'])
        self.assertNotIn('accountRelayStatus',target)
        self.assertEqual(target['transport'],'ssh')
        self.assertEqual(target['status'],'online')
        self.assertNotIn('离线',target['statusLabel'])

    def test_cloud_only_machine_is_not_listed_or_used_as_writer(self):
        self.machines.append(dict(id='account:cloud-only', name='cloud-only', source='account',
                                  clientKind='windows', status='online', selectable=True))
        snapshot,_=writer.observe(client_context={'platform':'mac'})
        target=next(m for m in snapshot['machines'] if m['id']==WINDOWS)
        self.assertTrue(target['selectable'])
        self.assertEqual(target['name'],'windows-pc')
        self.assertNotIn('account:cloud-only',{m['id'] for m in snapshot['machines']})

    def test_unregistered_host_is_not_queried_even_when_a_peer_is_observed(self):
        self.hosts={}
        with patch.object(writer,'controller_identity',side_effect=AssertionError('unregistered probe')):
            snapshot,_=writer.observe(client_context={'platform':'mac'})
        self.assertEqual([m['id'] for m in snapshot['machines']],['local'])
        self.assertIsNone(next(r for r in snapshot['records'] if r['id']==MAIN)['ownerMachineId'])

    def test_absent_ssh_peer_cannot_reuse_cached_identity_as_online(self):
        self.peers=[]
        with patch.object(writer,'controller_identity',side_effect=AssertionError('no connected peer')):
            snapshot,_=writer.observe(client_context={'platform':'mac'})
        target=next(m for m in snapshot['machines'] if m['id']==WINDOWS)
        self.assertFalse(target['selectable'])
        self.assertEqual(target['status'],'configured')

    def test_legacy_cloud_target_is_rejected_before_any_mutation(self):
        with self.assertRaises(writer.WriterError):
            writer.make_plan(MAIN,'account:old-target')
        self.kill.assert_not_called()

    def test_windows_uses_local_metadata_and_does_not_change_mac_owner(self):
        self.locks[MAIN]=[dict(pid=200,inode=1)]
        self.machines[1]['name']=NAME
        value=context()
        self.hosts=HOSTS
        with patch.dict(os.environ,{'SSH_CONNECTION':'192.0.2.1 55 192.0.2.2 22'}), \
             patch('caller_context.time.time',return_value=1001), \
             patch.object(writer,'controller_identity',side_effect=AssertionError('must not SSH back to caller')), \
             patch.object(writer,'atomic_json'):
            snapshot,_=writer.observe(client_context=value)
        self.assertEqual(snapshot['caller']['hostName'],NAME)
        self.assertEqual(snapshot['caller']['machineId'],WINDOWS)
        self.assertEqual(snapshot['storageHost']['name'],'mac-pc.local')
        mac=next(m for m in snapshot['machines'] if m['id']=='local')
        self.assertNotIn('本机',mac['statusLabel'])
        target=next(m for m in snapshot['machines'] if m['id']==WINDOWS)
        self.assertTrue(target['selectable'])
        self.assertIn('当前操作端',target['statusLabel'])
        task=next(r for r in snapshot['records'] if r['id']==MAIN)
        self.assertEqual(task['writerHostName'],'mac-pc.local')
        self.kill.assert_not_called()


if __name__=='__main__':
    unittest.main()
