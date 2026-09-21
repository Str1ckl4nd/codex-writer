"""User-owned configuration and state; never infer a remote machine to stop."""
from pathlib import Path
import json
import os
import re

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('SESSION_WRITER_CONFIG', str(Path.home()/'.config/session-writer/config.json'))).expanduser()
STATE_DIR = Path(os.environ.get('SESSION_WRITER_STATE_DIR', str(Path.home()/'.local/state/session-writer'))).expanduser().absolute()


def read_config(path=CONFIG_PATH):
    if not path.exists():
        return {}
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):
        raise ValueError('Session Writer configuration must be a JSON object')
    return value


CONFIG=read_config()
CODEX_HOME=Path(CONFIG.get('codex_home',os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))).expanduser().absolute()
CODEX_APP=Path(CONFIG.get('codex_app','/Applications/ChatGPT.app')).expanduser().absolute()
CODEX_BINARY=Path(CONFIG.get('codex_binary',str(CODEX_APP/'Contents/Resources/codex'))).expanduser().absolute()
WINDOWS_ALIAS=CONFIG.get('windows_ssh_alias','windows-pc')
PEER_ALIASES=CONFIG.get('windows_peer_aliases',[WINDOWS_ALIAS])
MAC_ALIAS=CONFIG.get('mac_ssh_alias','mac')
ALIAS_PATTERN=re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
if (not isinstance(PEER_ALIASES,list) or not PEER_ALIASES or
        any(not isinstance(item,str) or not ALIAS_PATTERN.fullmatch(item) for item in [WINDOWS_ALIAS,MAC_ALIAS,*PEER_ALIASES])):
    raise ValueError('SSH aliases must be explicit, safe names from your SSH configuration')
if WINDOWS_ALIAS not in PEER_ALIASES:
    raise ValueError('windows_peer_aliases must include windows_ssh_alias')
if 'enable_handoff' in CONFIG and type(CONFIG['enable_handoff']) is not bool:
    raise ValueError('enable_handoff must be a boolean')
EXCLUDED_IDS=CONFIG.get('excluded_thread_ids',[])
if not isinstance(EXCLUDED_IDS,list) or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',value) for value in EXCLUDED_IDS):
    raise ValueError('excluded_thread_ids must contain UUIDs')


def is_windows_peer(peer):
    return bool(set(peer.get('aliases',[])).intersection(PEER_ALIASES))
