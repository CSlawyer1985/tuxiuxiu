#!/usr/bin/env python3
"""Verify manifest bytes and exclude runtime artifacts from a publication."""
import argparse,hashlib,json,zipfile
from pathlib import Path
BLOCKED_DIRS={'__pycache__','.venv','node_modules','scratch','input','output','.git'}
BLOCKED_SUFFIXES={'.pyc','.db','.sqlite','.sqlite3','.log','.bak','.partial','.dylib'}
def verify(destination):
 root=Path(destination).resolve();data=json.loads((root/'PUBLICATION-MANIFEST.json').read_text());repo=root/'repository'
 expected={v['path']:v for v in data['repository_files']};actual={str(p.relative_to(repo)) for p in repo.rglob('*') if p.is_file() and '.git' not in p.relative_to(repo).parts}
 if actual!=set(expected):raise ValueError('Repository file list changed')
 for rel,item in expected.items():
  p=repo/rel
  if p.is_symlink() or set(p.relative_to(repo).parts)&BLOCKED_DIRS or p.suffix in BLOCKED_SUFFIXES or p.name=='.DS_Store':raise ValueError('Runtime artifact: '+rel)
  if p.stat().st_size!=item['bytes'] or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Checksum mismatch: '+rel)
  if p.stat().st_size>=100*1024*1024:raise ValueError('Large binary must be a release attachment')
 for item in data['release_assets']:
  p=root/'release-assets'/item['path']
  if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('Release checksum mismatch')
  if p.suffix=='.zip':
   with zipfile.ZipFile(p) as z:
    if z.testzip() is not None:raise ValueError('Invalid ZIP')
    for name in z.namelist():
     q=Path(name)
     if q.is_absolute() or '..' in q.parts or set(q.parts)&BLOCKED_DIRS or q.suffix in BLOCKED_SUFFIXES or q.name=='.DS_Store':raise ValueError('Runtime artifact in ZIP: '+name)
 return {'verified':True,'repository_files':len(actual),'release_assets':len(data['release_assets']),'runtime_artifacts':0}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('destination',type=Path);a=p.parse_args();print(json.dumps(verify(a.destination)))
