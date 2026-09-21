"""One bounded request to an explicitly configured, authenticated Windows host.

No private broker dependency, arbitrary targets, host-key bypass, or mutation retry.
"""
import json
import re
import subprocess

from runtime_config import APP_DIR, WINDOWS_ALIAS, MAC_ALIAS


class BridgeError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code=code


def script_for(operation, expectedPid=None, expectedStart=None):
    source=(APP_DIR/'windows_controller.ps1').read_text(encoding='utf-8-sig')
    if operation=='identity':
        invoke="Invoke-WriterIdentityOperation -Action identity"
    elif (operation=='close-desktop' and type(expectedPid) is int and expectedPid>0
          and isinstance(expectedStart,str) and re.fullmatch(r'\d{4}-[0-9T:.Z+-]{1,76}',expectedStart)):
        invoke=f"Invoke-WriterIdentityOperation -Action close-desktop -PidToStop {expectedPid} -StartToMatch '{expectedStart}'"
    else:
        raise BridgeError('INVALID_OPERATION')
    return '& {\n'+source+'\n'+invoke+f" -MacAlias '{MAC_ALIAS}'\n"+'}\n\n'


def request(operation, **kwargs):
    script=script_for(operation,**kwargs)
    argv=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
          '-o','ConnectTimeout=8','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',
          WINDOWS_ALIAS,'powershell -NoLogo -NoProfile -NonInteractive -Command -']
    try:
        result=subprocess.run(argv,input=script,capture_output=True,text=True,timeout=25)
    except subprocess.TimeoutExpired:
        raise BridgeError('REMOTE_EFFECT_UNKNOWN' if operation=='close-desktop' else 'REMOTE_QUERY_TIMEOUT') from None
    if result.returncode:
        code='CONTROLLER_CHANGED' if 'CONTROLLER_CHANGED' in result.stderr else 'SSH_REQUEST_FAILED'
        raise BridgeError(code)
    if len(result.stdout)>131072:
        raise BridgeError('RESPONSE_TOO_LARGE')
    try:
        value=json.loads(result.stdout)
        if not isinstance(value,dict):raise ValueError()
        return value
    except ValueError:
        raise BridgeError('INVALID_RESPONSE') from None
