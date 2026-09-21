"""New/old daemon socket regressions; pure metadata fixtures in an isolated VM."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import discovery_adapter as discovery
import writer_service as writer


class SocketDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.real=self.root/'private'/'daemon.sock'
        self.real.parent.mkdir()
        self.real.touch()
        self.alias=self.root/'app-server-control.sock'
        self.alias.symlink_to(self.real)
        self.binary=str(writer.CODEX_ROOT/'packages/standalone/0.156/bin/codex')
        self.process=dict(pid=10,ppid=1,uid=os.getuid(),start='fixed',command=self.binary+' app-server --listen unix://')

    def test_old_socket_name_still_accepted(self):
        self.assertIn(str(self.real),discovery.control_socket_names(self.real))

    def test_symlink_resolves_to_real_daemon_name(self):
        names=discovery.control_socket_names(self.alias)
        self.assertEqual(names,{str(self.alias),str(self.real)})

    def test_relative_symlink_resolves(self):
        relative=self.root/'relative.sock'
        relative.symlink_to('private/daemon.sock')
        self.assertIn(str(self.real),discovery.control_socket_names(relative))

    def test_socket_server_queries_canonical_path(self):
        def run(arguments):
            self.assertEqual(arguments[-1],str(self.real))
            return SimpleNamespace(stdout='10\n10\n')
        with patch.object(writer,'CONTROL',self.alias),patch.object(writer,'command',side_effect=run):
            self.assertEqual(writer.socket_server({10:self.process}),10)

    def test_no_arbitrary_process_is_accepted_as_the_server(self):
        other=dict(self.process,command='/tmp/other app-server --listen unix://')
        with patch.object(writer,'CONTROL',self.alias),patch.object(writer,'command',return_value=SimpleNamespace(stdout='10\n')):
            self.assertEqual(writer.socket_server({10:other}),0)

    def test_multiple_servers_fail_closed(self):
        with patch.object(writer,'CONTROL',self.alias),patch.object(writer,'command',return_value=SimpleNamespace(stdout='10\n11\n')):
            with self.assertRaises(writer.WriterError) as result:
                writer.socket_server({10:self.process,11:dict(self.process,pid=11)})
        self.assertEqual(result.exception.code,'CONTROL_SERVER_AMBIGUOUS')

    def test_dangling_alias_is_not_reported_online(self):
        self.real.unlink()
        with patch.object(writer,'CONTROL',self.alias),patch.object(writer,'command',side_effect=AssertionError('unexpected lsof')):
            self.assertEqual(writer.socket_server({10:self.process}),0)

    def test_new_socket_keeps_complete_proxy_to_windows_proof(self):
        process_text='10 1 '+self.binary+' app-server --listen unix://\n20 30 '+self.binary+' app-server proxy\n30 1 sshd-session: demo@notty\n'
        unix='p10\nf29\nd0xserver\nn'+str(self.real)+'\np20\nf7\nd0xproxy\nn->0xserver\n'
        tcp='p30\nf4\nn192.0.2.2:22->192.0.2.1:50000\n'
        def run(arguments,*args):
            if arguments[0]=='/bin/ps': return process_text
            return unix if '-U' in arguments else tcp
        with patch.object(discovery,'CONTROL_SOCKET',str(self.alias)),patch.object(discovery,'_run',side_effect=run), \
             patch.object(discovery,'configured_ssh_hosts',return_value={'windows-pc':{'endpoints':['192.0.2.1']}}):
            peers=discovery.peer_connections()
        self.assertEqual(len(peers),1)
        self.assertEqual(peers[0]['serverPid'],10)
        self.assertEqual(peers[0]['proxyPid'],20)
        self.assertEqual(peers[0]['sshPid'],30)
        self.assertEqual(peers[0]['aliases'],['windows-pc'])

    def test_wrong_socket_path_does_not_create_a_peer(self):
        process_text='10 1 '+self.binary+' app-server --listen unix://\n20 30 '+self.binary+' app-server proxy\n30 1 sshd-session: demo@notty\n'
        unix='p10\nf29\nd0xserver\nn/unrelated/socket\np20\nf7\nd0xproxy\nn->0xserver\n'
        with patch.object(discovery,'CONTROL_SOCKET',str(self.alias)), \
             patch.object(discovery,'_run',side_effect=lambda args:process_text if args[0]=='/bin/ps' else unix), \
             patch.object(discovery,'configured_ssh_hosts',return_value={}):
            self.assertEqual(discovery.peer_connections(),[])


if __name__=='__main__':unittest.main()
