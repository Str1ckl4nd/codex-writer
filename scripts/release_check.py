"""Check the selected release sources; diagnostics never print matched secrets.

This is a bounded release guard, not a claim of comprehensive secret detection.
"""
from pathlib import Path
import re
import subprocess
import sys

root=Path(__file__).resolve().parent.parent
skip={'.git','dist','build','__pycache__','.venv'}
patterns={
    'private-key':re.compile(rb'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----'),
    'credential':re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}|sk-proj-[A-Za-z0-9_-]{30,})'),
    'mac-user-path':re.compile(rb'/'+rb'Users/(?!YOUR_MAC_USER(?:/|\b))[^/\s\x27\x22]+/'),
    'windows-user-path':re.compile(rb'C:'+rb'\\+Users\\+(?!demo\\)[^\\\s\x27\x22]+\\'),
    'live-task-id':re.compile(rb'\b01[a-f0-9]{6}-[a-f0-9-]{27}\b'),
}
forbidden_suffixes={'.sqlite','.db','.jsonl','.pem','.key','.png','.jpg','.asar','.sock','.lock'}
issues=[]
paths=[p for p in root.rglob('*') if p.is_file() and not any(part in skip for part in p.relative_to(root).parts)]
for path in paths:
    rel=path.relative_to(root)
    if path.is_symlink() or path.suffix in forbidden_suffixes or path.name in ('config.json','last-handoff.json') or 'cache' in path.name:
        issues.append((str(rel),'runtime-or-unexpected-artifact'))
    data=path.read_bytes()
    for label,pattern in patterns.items():
        if pattern.search(data):issues.append((str(rel),label))
if issues:
    for file,label in issues:print(f'{file}: {label}',file=sys.stderr)
    raise SystemExit(1)
print(f'Release source guard: PASS ({len(paths)} files). Human review is still required.')
