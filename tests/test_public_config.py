import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock,patch

import runtime_config as settings
import writer_service as writer
import discovery_adapter as discovery
import ssh_bridge


class PublicSafety(unittest.TestCase):
    def test_default_handoff_disabled_before_any_process_read(self):
        with patch.dict(writer.CONFIG,{'enable_handoff':False}),patch.object(writer,'process_table',side_effect=AssertionError('unexpected read')):
            with self.assertRaises(writer.WriterError) as error:
                writer.claim('00000000-0000-0000-0000-000000000001','local','unused')
        self.assertEqual(error.exception.code,'HANDOFF_DISABLED')

    def test_legacy_account_flag_cannot_reenable_removed_transport(self):
        with patch.dict(settings.CONFIG,{'account_discovery':True}), \
             patch.object(discovery,'local_hostname',return_value='mac.local'), \
             patch.object(discovery,'configured_ssh_hosts',return_value={}), \
             patch.object(discovery,'peer_connections',return_value=[]), \
             patch.object(discovery.subprocess,'run',side_effect=AssertionError('unexpected command')):
            result=discovery.discover(force=True)
        self.assertEqual([m['id'] for m in result['machines']],['local'])
        self.assertEqual(result['discoveryMode'],'ssh-only')
        self.assertFalse(hasattr(discovery,'_account_environments'))

    def test_configured_executable_with_spaces_is_recognized(self):
        binary=Path('/Applications/My Codex.app/Contents/Resources/codex')
        process=dict(pid=10,uid=os.getuid(),ppid=1,start='x',command=str(binary)+' app-server --listen unix://')
        with patch.object(writer,'CODEX_BINARY',binary):
            self.assertEqual(writer.process_kind(process,{10:process}),'ssh-server')

    def test_source_and_runtime_state_are_separate(self):
        self.assertNotEqual(settings.APP_DIR,settings.STATE_DIR)
        self.assertEqual(writer.ROOT,settings.STATE_DIR)
        self.assertEqual(writer.CODEX_ROOT,settings.CODEX_HOME)

    def test_invalid_ssh_operation_cannot_spawn(self):
        with patch.object(ssh_bridge.subprocess,'run',side_effect=AssertionError('spawn forbidden')):
            with self.assertRaises(ssh_bridge.BridgeError):
                ssh_bridge.request('arbitrary-command')

    def test_close_needs_exact_pid_and_start(self):
        for pid,start in [(0,'2026-01-01T00:00:00Z'),(1,"invalid'"),(True,'2026-01-01T00:00:00Z')]:
            with self.subTest(pid=pid,start=start),self.assertRaises(ssh_bridge.BridgeError):
                ssh_bridge.script_for('close-desktop',pid,start)

    def test_ssh_uses_configured_alias_and_host_key_checks(self):
        result=Mock(returncode=0,stdout='{"schemaVersion":2}',stderr='')
        spec=dict(alias='workstation-alias',platform='windows',backend_alias='storage')
        with patch.object(ssh_bridge,'controller_specs',return_value=[spec]),patch.object(ssh_bridge.subprocess,'run',return_value=result) as run:
            ssh_bridge.request('identity',alias='workstation-alias')
        self.assertIn('workstation-alias',run.call_args.args[0])
        self.assertIn('StrictHostKeyChecking=yes',run.call_args.args[0])
        self.assertNotIn('Bypass',run.call_args.args[0][-1])
        self.assertIn('Invoke-WriterIdentityOperation',run.call_args.kwargs['input'])
        self.assertEqual(run.call_count,1)

    def test_uncertain_close_is_not_replayed(self):
        with patch.object(ssh_bridge.subprocess,'run',side_effect=subprocess.TimeoutExpired('ssh',25)) as run:
            with self.assertRaises(ssh_bridge.BridgeError) as error:
                ssh_bridge.request('close-desktop',expectedPid=1,expectedStart='2026-01-01T00:00:00Z',expectedHostName='workstation')
        self.assertEqual(error.exception.code,'REMOTE_EFFECT_UNKNOWN')
        self.assertEqual(run.call_count,1)

    def test_bad_alias_rejected_on_configuration_load(self):
        with tempfile.TemporaryDirectory() as folder:
            config=Path(folder)/'config.json'
            config.write_text(json.dumps({'windows_ssh_alias':'-oBadOption'}))
            result=subprocess.run([os.sys.executable,'-c','import runtime_config'],env=dict(os.environ,SESSION_WRITER_CONFIG=str(config)),capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('SSH aliases',result.stderr)


if __name__=='__main__':unittest.main()
