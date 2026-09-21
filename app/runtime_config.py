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


def controller_specs(config=None):
    """Explicit SSH controllers; multiple aliases may denote one physical host."""
    config = CONFIG if config is None else config
    entries = config.get('ssh_controllers')
    if entries is None:
        alias = config.get('windows_ssh_alias', 'windows-pc')
        entries = [dict(alias=alias, platform='windows',
                        peer_aliases=config.get('windows_peer_aliases', [alias]),
                        backend_alias=config.get('mac_ssh_alias', 'mac'))]
    if not isinstance(entries, list) or len(entries)>32:
        raise ValueError('ssh_controllers must contain at most 32 explicit hosts')
    result = []
    claimed = set()
    for value in entries:
        if not isinstance(value, dict):
            raise ValueError('each SSH controller must be an object')
        alias = value.get('alias')
        aliases = value.get('peer_aliases', [alias])
        backend = value.get('backend_alias', config.get('mac_ssh_alias', 'mac'))
        platform = value.get('platform')
        if (not isinstance(aliases, list) or not aliases or alias not in aliases or
                any(not isinstance(a,str) or not ALIAS_PATTERN.fullmatch(a) for a in [alias,backend,*aliases]) or
                platform not in ('windows','mac')):
            raise ValueError('SSH controllers need explicit aliases and a windows/mac platform')
        if claimed.intersection(aliases):
            raise ValueError('an SSH alias cannot belong to multiple controllers')
        claimed.update(aliases)
        result.append(dict(id='ssh:'+alias, alias=alias, platform=platform,
                           peer_aliases=sorted(set(aliases)), backend_alias=backend))
    return result


def peer_controller_ids(peer, specs=None):
    specs = controller_specs() if specs is None else specs
    return [spec['id'] for spec in specs if set(peer.get('aliases',[])).intersection(spec['peer_aliases'])]


controller_specs()  # Reject ambiguous configuration before any connection.
