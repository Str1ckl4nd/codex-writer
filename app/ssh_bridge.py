"""Bounded SSH commands for explicitly registered controllers.

Helpers are sent on stdin: no remote installation, daemon, or automatic retry.
"""
import base64
import json
import re
import subprocess

from runtime_config import APP_DIR, WINDOWS_ALIAS, MAC_ALIAS, controller_specs


class BridgeError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code=code


def script_for(operation, expectedPid=None, expectedStart=None, expectedHostName=None,
               platform='windows', backend_alias=None):
    backend_alias = MAC_ALIAS if backend_alias is None else backend_alias
    if not isinstance(backend_alias,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',backend_alias):
        raise BridgeError('INVALID_ALIAS')
    if operation not in ('identity','close-desktop') or platform not in ('windows','mac'):
        raise BridgeError('INVALID_OPERATION')
    if operation=='close-desktop' and not (
            type(expectedPid) is int and expectedPid>0 and isinstance(expectedStart,str) and
            re.fullmatch(r'\d{4}-[0-9T:.Z+-]{1,76}',expectedStart) and
            isinstance(expectedHostName,str) and 0<len(expectedHostName)<=255 and
            all(ord(c)>=32 for c in expectedHostName)):
        raise BridgeError('INVALID_OPERATION')
    if platform=='mac':
        source=(APP_DIR/'mac_controller.py').read_text(encoding='utf-8')
        data=dict(operation=operation,backendAlias=backend_alias,expectedPid=expectedPid,
                  expectedStart=expectedStart,expectedHostName=expectedHostName)
        encoded=base64.b64encode(json.dumps(data).encode()).decode()
        return source+"\nimport base64\nsys.exit(main(json.loads(base64.b64decode('"+encoded+"'))))\n"
    source=(APP_DIR/'windows_controller.ps1').read_text(encoding='utf-8-sig')
    invoke="Invoke-WriterIdentityOperation -Action "+operation
    if operation=='close-desktop':
        name=expectedHostName.replace("'","''")
        invoke+=f" -PidToStop {expectedPid} -StartToMatch '{expectedStart}' -HostNameToMatch '{name}'"
    return '& {\n'+source+'\n'+invoke+f" -MacAlias '{backend_alias}'\n"+'}\n\n'


def request(operation, alias=None, **kwargs):
    alias=WINDOWS_ALIAS if alias is None else alias
    spec=next((value for value in controller_specs() if value['alias']==alias),None)
    if spec is None:
        raise BridgeError('UNREGISTERED_CONTROLLER')
    script=script_for(operation,platform=spec['platform'],backend_alias=spec['backend_alias'],**kwargs)
    remote='python3 -' if spec['platform']=='mac' else 'powershell -NoLogo -NoProfile -NonInteractive -Command -'
    argv=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
          '-o','ConnectTimeout=8','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',alias,remote]
    try:
        result=subprocess.run(argv,input=script,capture_output=True,text=True,timeout=25 if operation=='close-desktop' else 12)
    except subprocess.TimeoutExpired:
        raise BridgeError('REMOTE_EFFECT_UNKNOWN' if operation=='close-desktop' else 'REMOTE_QUERY_TIMEOUT') from None
    if result.returncode:
        raise BridgeError('CONTROLLER_CHANGED' if 'CONTROLLER_CHANGED' in result.stderr+result.stdout else 'SSH_REQUEST_FAILED')
    if len(result.stdout)>131072:
        raise BridgeError('RESPONSE_TOO_LARGE')
    try:
        value=json.loads(result.stdout)
        if not isinstance(value,dict):raise ValueError()
        if value.get('error'):
            raise BridgeError('CONTROLLER_CHANGED' if value['error']=='CONTROLLER_CHANGED' else 'REMOTE_QUERY_FAILED')
        return value
    except ValueError:
        raise BridgeError('INVALID_RESPONSE') from None
