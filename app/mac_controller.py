"""One-shot macOS SSH helper. No remote installation or background service.

Inspect the caller's Codex desktop and its SSH proxy ancestry. Closure requires
an exact hostname/PID/start identity and a fresh, unique controller match.
"""
import datetime as dt
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys


def process_rows(output):
    result = []
    for line in output.splitlines():
        fields = line.split(None, 8)
        if len(fields)!=9 or not all(value.isdigit() for value in fields[:3]):
            continue
        try:
            started = dt.datetime.strptime(' '.join(fields[3:8]), '%a %b %d %H:%M:%S %Y').replace(tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            continue
        result.append(dict(pid=int(fields[0]), ppid=int(fields[1]), uid=int(fields[2]),
                           start=started, command=fields[8]))
    return result


def identity(rows, backend_alias, hostname, uid):
    by_id = {row['pid']:row for row in rows}
    app_pattern = re.compile(r'^/(?:Applications|Users/[^/]+/Applications)/(ChatGPT|Codex)\.app/Contents/MacOS/\1$')
    apps = {row['pid']:row for row in rows if row['uid']==uid and app_pattern.fullmatch(row['command'])}
    proxies = []
    for row in rows:
        try:
            argv = shlex.split(row['command'])
        except ValueError:
            continue
        if (row['uid']==uid and argv and os.path.basename(argv[0])=='ssh' and backend_alias in argv
                and 'app-server proxy' in ' '.join(argv[1:])):
            proxies.append(row)
    controllers = {}
    matched = 0
    for proxy in proxies:
        parent = proxy['ppid']
        seen = set()
        for _ in range(12):
            if parent in seen or parent not in by_id:
                break
            seen.add(parent)
            if parent in apps:
                app = apps[parent]
                item = controllers.setdefault(parent, dict(pid=parent,start=app['start'],
                                                           name=os.path.basename(app['command']),proxyPids=[]))
                item['proxyPids'].append(proxy['pid'])
                matched += 1
                break
            parent = by_id[parent]['ppid']
    detection = ('multiple_controllers' if len(controllers)>1 else
                 'confirmed' if len(controllers)==1 and matched==len(proxies) else
                 'unmatched_proxies' if controllers else 'desktop_absent' if not apps else 'proxy_not_connected')
    return dict(schemaVersion=2, hostName=hostname,
                apps=[dict(pid=p['pid'],start=p['start'],name=os.path.basename(p['command'])) for p in apps.values()],
                controllers=sorted(controllers.values(),key=lambda p:p['pid']),
                proxyCount=len(proxies),matchedProxyCount=matched,detection=detection)


def execute(request, rows, hostname, uid, stop):
    alias = request.get('backendAlias','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}',alias):
        raise ValueError('INVALID_ALIAS')
    value = identity(rows,alias,hostname,uid)
    if request.get('operation')=='identity':
        return value
    owners = value['controllers']
    if (request.get('operation')!='close-desktop' or value['detection']!='confirmed' or len(owners)!=1 or
            type(request.get('expectedPid')) is not int or owners[0]['pid']!=request['expectedPid'] or
            owners[0]['start']!=request.get('expectedStart') or hostname!=request.get('expectedHostName')):
        raise ValueError('CONTROLLER_CHANGED')
    stop(owners[0]['pid'],signal.SIGTERM)
    return dict(stopped=owners[0]['pid'])


def main(request):
    try:
        result = subprocess.run(['/bin/ps','-axo','pid=,ppid=,uid=,lstart=,command='],
                                env=dict(os.environ,LC_ALL='C',TZ='UTC'),capture_output=True,text=True,timeout=5,check=True)
        value = execute(request,process_rows(result.stdout),socket.gethostname(),os.getuid(),os.kill)
        print(json.dumps(value,separators=(',',':')))
    except Exception as error:
        code = str(error) if isinstance(error,ValueError) else type(error).__name__
        print(json.dumps(dict(error=code)))
        return 1
    return 0
