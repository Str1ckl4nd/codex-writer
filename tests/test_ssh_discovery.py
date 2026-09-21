"""SSH-only boundary checks using local fixtures, never live hosts or accounts."""
import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import discovery_adapter as discovery


class SshDiscovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'ssh-config'
        self.config.write_text('Host windows-pc\n  HostName 192.0.2.1\n  User demo\n')
        self.patch = patch.object(discovery, 'SSH_CONFIG', self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def snapshot(self, peers=None, force=False):
        with patch.object(discovery, 'local_hostname', return_value='mac.local'), \
             patch.object(discovery, 'peer_connections', return_value=peers or []), \
             patch.object(discovery.subprocess, 'run', side_effect=AssertionError('no network probes')):
            return discovery.discover(force=force)

    def test_registered_alias_needs_no_codex_registration(self):
        result = self.snapshot()
        self.assertEqual({m['id'] for m in result['machines']}, {'local', 'ssh:windows-pc'})
        remote = result['machines'][1]
        self.assertEqual(remote['status'], 'configured')
        self.assertFalse(remote['selectable'])
        self.assertEqual(remote['transport'], 'ssh')
        self.assertNotIn('accountDiscovery', result)

    def test_config_refresh_adds_and_removes_hosts_without_a_cache(self):
        self.assertEqual(len(self.snapshot()['machines']), 2)
        self.config.write_text('Host replacement\n  HostName 192.0.2.5\n')
        result = self.snapshot(force=True)
        self.assertEqual({m['id'] for m in result['machines']}, {'local', 'ssh:replacement'})
        self.config.write_text('')
        self.assertEqual([m['id'] for m in self.snapshot()['machines']], ['local'])

    def test_missing_config_has_no_cloud_fallback(self):
        self.config.unlink()
        self.assertEqual([m['id'] for m in self.snapshot()['machines']], ['local'])

    def test_unreadable_config_is_reported_not_reported_as_offline(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError('unreadable')):
            result = self.snapshot()
        self.assertEqual([m['id'] for m in result['machines']], ['local'])
        self.assertIn('ssh_config_unreadable', {d['code'] for d in result['diagnostics']})

    def test_cloud_cache_global_state_and_auth_files_are_never_read(self):
        for name in ('auth.json', '.codex-global-state.json', 'machine-discovery-cache.json'):
            (self.root / name).write_text(json.dumps({'items': [{'name': 'cloud-only'}]}))
        read_text = Path.read_text

        def only_ssh(path, *args, **kwargs):
            self.assertEqual(path, self.config, 'discovery attempted a non-SSH file read')
            return read_text(path, *args, **kwargs)

        with patch.object(Path, 'read_text', autospec=True, side_effect=only_ssh):
            result = self.snapshot(force=True)
        self.assertEqual({m['id'] for m in result['machines']}, {'local', 'ssh:windows-pc'})

    def test_connection_evidence_does_not_grant_handoff_by_itself(self):
        result = self.snapshot([{'aliases': ['windows-pc']}])
        self.assertEqual(result['machines'][1]['status'], 'connected')
        self.assertFalse(result['machines'][1]['selectable'])

    def test_unregistered_peer_is_not_promoted_into_inventory(self):
        result = self.snapshot([{'aliases': ['unregistered'], 'peerIp': '192.0.2.99'}])
        self.assertEqual({m['id'] for m in result['machines']}, {'local', 'ssh:windows-pc'})
        self.assertEqual(result['machines'][1]['status'], 'configured')

    def test_same_endpoint_does_not_merge_registered_aliases(self):
        self.config.write_text('Host one two\n  HostName 192.0.2.1\n')
        self.assertEqual({m['id'] for m in self.snapshot()['machines']}, {'local', 'ssh:one', 'ssh:two'})

    def test_bare_host_block_is_a_registered_candidate(self):
        self.config.write_text('Host my-workstation\n')
        self.assertIn('ssh:my-workstation', {m['id'] for m in self.snapshot()['machines']})

    def test_patterns_conditionals_and_commands_are_not_evaluated(self):
        self.config.write_text('''Host * !excluded *.example
  HostName 192.0.2.99
Include unexpected-include
Match originalhost conditional exec "unexpected-command"
  HostName 192.0.2.88
Host windows-pc
  HostName 192.0.2.1
  ProxyCommand unexpected-command
Match exec "unexpected-command"
  HostName 192.0.2.77
''')
        with patch.object(discovery.subprocess, 'run', side_effect=AssertionError('config evaluation forbidden')):
            hosts = discovery.configured_ssh_hosts()
        self.assertEqual(set(hosts), {'windows-pc'})
        self.assertEqual(hosts['windows-pc']['endpoints'], ['192.0.2.1'])

    def test_first_literal_endpoint_wins(self):
        self.config.write_text('Host windows-pc\n  HostName 192.0.2.1\nHost windows-pc\n  HostName 192.0.2.2\n')
        self.assertEqual(discovery.configured_ssh_hosts()['windows-pc']['endpoints'], ['192.0.2.1'])

    def test_production_sources_have_no_account_discovery_request_path(self):
        app = Path(discovery.__file__).parent
        forbidden = ('chatgpt.com/backend-api', 'getAuthStatus', 'includeToken',
                     'accountDiscovery', 'sameAccount', 'accountRelayStatus')
        for path in app.glob('*.py'):
            source = path.read_text()
            for marker in forbidden:
                self.assertNotIn(marker, source, str(path.name))
        tree = ast.parse(Path(discovery.__file__).read_text())
        imports = {name.name.split('.')[0] for node in ast.walk(tree)
                   if isinstance(node, ast.Import) for name in node.names}
        imports.update(node.module.split('.')[0] for node in ast.walk(tree)
                       if isinstance(node, ast.ImportFrom) and node.module)
        self.assertFalse(imports.intersection({'urllib', 'http', 'requests', 'httpx'}))

    def test_both_frontends_require_the_ssh_only_snapshot_version(self):
        app = Path(discovery.__file__).parent
        swift = (app / 'session_writer_ui.swift').read_text()
        windows = (app / 'unlock.ps1').read_text()
        self.assertIn('snapshot.schemaVersion == 5', swift)
        self.assertIn('$NewSnapshot.schemaVersion -ne 5', windows)
        for source in (swift, windows):
            self.assertIn('刷新 SSH 主机', source)
            self.assertNotIn('账号已连接机器', source)
            self.assertNotIn('重新发现机器', source)


if __name__ == '__main__':
    unittest.main()
