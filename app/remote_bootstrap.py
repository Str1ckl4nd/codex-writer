"""Run an owned, bounded backend package over authenticated SSH without install.

Only application code and allowlisted tool settings are transported, never SSH
configuration, private keys, login files, task databases, or session histories.
"""
import base64
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile

PROGRAM_FILES = frozenset((
    'session_writer.py','writer_service.py','writer_force.py','writer_desktop.py',
    'writer_history.py','writer_rpc.py','writer_log.py','caller_context.py',
    'discovery_adapter.py','runtime_config.py','ssh_bridge.py','mac_controller.py',
    'windows_controller.ps1',
))
CONFIG_KEYS = frozenset((
    'codex_home','codex_app','codex_binary','ssh_controllers','enable_handoff',
    'windows_ssh_alias','windows_peer_aliases','mac_ssh_alias','excluded_thread_ids',
))
MAX_BYTES = 2 * 1024 * 1024


def decode_package(payload):
    if not isinstance(payload,dict) or set(payload)!= {'files','config','argv'}:
        raise ValueError('INVALID_PACKAGE')
    files,config,argv=payload['files'],payload['config'],payload['argv']
    if (not isinstance(files,dict) or set(files)!=PROGRAM_FILES or not isinstance(config,dict) or
            not set(config)<=CONFIG_KEYS or not isinstance(argv,list) or not argv or len(argv)>24 or
            any(not isinstance(arg,str) or len(arg)>30000 for arg in argv) or
            argv[0] not in ('snapshot','plan','claim','logs','list','diagnose')):
        raise ValueError('INVALID_PACKAGE')
    decoded={}
    size=0
    for name,value in files.items():
        if not isinstance(value,str):
            raise ValueError('INVALID_PACKAGE')
        data=base64.b64decode(value,validate=True)
        size+=len(data)
        if size>MAX_BYTES:
            raise ValueError('PACKAGE_TOO_LARGE')
        decoded[name]=data
    return decoded,config,argv


def main():
    try:
        raw=sys.stdin.buffer.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:
            raise ValueError('PACKAGE_TOO_LARGE')
        files,config,argv=decode_package(json.loads(raw))
    except Exception:
        print(json.dumps(dict(ok=False,errorCode='INVALID_REMOTE_PACKAGE',message='临时后端包不完整或超过限制；未执行任务操作。')))
        return 1
    with tempfile.TemporaryDirectory(prefix='codex-writer-request-') as directory:
        root=Path(directory)
        for name,data in files.items():
            (root/name).write_bytes(data)
        config_path=root/'request-config.json'
        config_path.write_text(json.dumps(config),encoding='utf-8')
        config_path.chmod(0o600)
        os.environ['SESSION_WRITER_CONFIG']=str(config_path)
        sys.path.insert(0,str(root))
        sys.argv=[str(root/'session_writer.py'),*argv]
        runpy.run_path(sys.argv[0],run_name='__main__')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
