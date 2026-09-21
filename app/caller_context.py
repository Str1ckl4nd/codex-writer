"""Separate the operating computer from the Mac that stores the task files.

Locally gathered controller metadata is usable only over a uniquely configured
host's authenticated SSH connection. No account or cloud device identity is used.
"""
import argparse
import base64
import copy
import ipaddress
import json
import re
import time
from runtime_config import controller_specs


def decode_context(encoded):
    if len(encoded)>24000:
        raise argparse.ArgumentTypeError('客户端上下文超过大小限制')
    try:
        value=json.loads(base64.b64decode(encoded,validate=True).decode('utf-8'))
        if not isinstance(value,dict) or value.get('schemaVersion')!=1 or value.get('platform') not in ('mac','windows'):
            raise ValueError()
        name=value.get('hostName','')
        if not isinstance(name,str) or len(name)>255 or any(ord(c)<32 for c in name):
            raise ValueError()
        if value['platform']=='windows' and (not name or not isinstance(value.get('observedAt'),(int,float))):
            raise ValueError()
        identity=value.get('identity')
        if identity is not None:
            if not isinstance(identity,dict) or identity.get('schemaVersion')!=2 or identity.get('hostName')!=name:
                raise ValueError()
            for field in ('apps','controllers'):
                rows=identity.get(field)
                if not isinstance(rows,list) or len(rows)>16:
                    raise ValueError()
                for row in rows:
                    if (not isinstance(row,dict) or type(row.get('pid')) is not int or row['pid']<1
                            or not isinstance(row.get('start'),str) or not re.fullmatch(r'\d{4}-[0-9T:.Z+-]{1,76}',row['start'])
                            or row.get('name') not in ('ChatGPT.exe','Codex.exe','ChatGPT','Codex')):
                        raise ValueError()
                    if field=='controllers' and (not isinstance(row.get('proxyPids'),list) or len(row['proxyPids'])>64
                            or any(type(pid) is not int or pid<1 for pid in row['proxyPids'])):
                        raise ValueError()
            if any(type(identity.get(key)) is not int or not 0<=identity[key]<=64 for key in ('proxyCount','matchedProxyCount')):
                raise ValueError()
            if identity.get('detection') not in ('confirmed','multiple_controllers','unmatched_proxies','desktop_absent','proxy_not_connected'):
                raise ValueError()
            # Never accept arbitrary caller keys into a cache or diagnostic log.
            value['identity']={key:identity[key] for key in ('schemaVersion','hostName','proxyCount','matchedProxyCount','detection')}
            for field in ('apps','controllers'):
                keys=('pid','start','name','proxyPids') if field=='controllers' else ('pid','start','name')
                value['identity'][field]=[{key:row[key] for key in keys} for row in identity[field]]
        return value
    except (ValueError,TypeError,KeyError,UnicodeError):
        raise argparse.ArgumentTypeError('客户端上下文格式无效') from None


def ip(value):
    try:
        address=ipaddress.ip_address(value)
        return str(getattr(address,'ipv4_mapped',None) or address)
    except ValueError:
        return None


def resolve_context(value, ssh_connection, hosts, local_name, now=None, specs=None):
    fields=ssh_connection.split()
    peer=ip(fields[0]) if fields else None
    specs=controller_specs() if specs is None else specs
    matching=[spec for spec in specs if peer is not None and any(
        ip(endpoint)==peer for alias in spec['peer_aliases'] for endpoint in hosts.get(alias,{}).get('endpoints',[]))]
    if value and (value['platform']=='windows' or fields):
        fresh=abs((time.time() if now is None else now)-value.get('observedAt',0))<=90
        verified=len(matching)==1 and matching[0]['platform']==value['platform'] and fresh
        caller=dict(platform=value['platform'],hostName=value.get('hostName','未确认'),verified=verified,
                    machineId=matching[0]['id'] if verified else None,
                    evidence='local-ui-over-known-ssh' if verified else 'unverified-client')
        identity=None
        if verified:
            identity=copy.deepcopy(value.get('identity'))
            if identity is None:
                identity=dict(schemaVersion=2,hostName=value['hostName'],available=False,apps=[],controllers=[],
                              proxyCount=0,errorCode='LOCAL_IDENTITY_UNAVAILABLE',
                              error='操作端在线，但未读到本机 Codex 进程状态。')
            else:
                identity.update(available=True,checkedAt=time.time(),evidence='windows-local')
        return caller,identity
    if not fields:
        return dict(platform='mac',hostName=local_name,verified=True,machineId='local',evidence='local-ui'),None
    return dict(platform='remote',hostName='未标识的远端操作端',verified=False,machineId=None,evidence='ssh'),None
