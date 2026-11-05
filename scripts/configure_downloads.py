#!/usr/bin/env python3
"""Configure local static redirects only; never publish or upload."""
import argparse,json,re
from pathlib import Path
from urllib.parse import urlparse

def configure(root,repository):
 if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('Expected owner/repository')
 root=Path(root);path=root/'site/downloads.json';data=json.loads(path.read_text());version=data['version'];lines=[]
 for package in data['packages']:
  filename=package['filename']
  if Path(filename).name!=filename:raise ValueError('Invalid asset filename')
  target=f'https://github.com/{repository}/releases/download/v{version}/{filename}'
  package['release_url']=target;lines.append(f'/downloads/{filename} {target} 302')
 data['repository']=repository;path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
 (root/'site/_redirects').write_text('\n'.join(lines)+'\n');return lines
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--repository',required=True);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args();print('\n'.join(configure(a.root,a.repository)))
