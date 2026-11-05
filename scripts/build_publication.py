#!/usr/bin/env python3
"""Create a new repository and separate release assets from explicit source lists."""
import argparse,hashlib,json,shutil,zipfile
from pathlib import Path
SITE_FILES=('_redirects','index.html','styles.css','app.js','downloads.json','third-party.txt','vendor/gsap.min.js','vendor/ScrollTrigger.min.js','vendor/GSAP-LICENSE.txt','media/training-before.jpg','media/training-after.jpg','media/training-detail-before.jpg','media/training-detail-after.jpg','media/portrait-before.jpg','media/portrait-after.jpg','media/skin-overlay.jpg','media/cover-training.jpg','media/watermark.png','media/compare.gif','media/workbench.png')
DOC_FILES=('INSTALL.md','TESTED-RESULTS.md','THIRD_PARTY_NOTICES.md','PUBLISHING.md','DEMO-ASSETS.md')
SCRIPT_FILES=('preview_publication.py','configure_downloads.py','build_publication.py','verify_publication.py')
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def build(publication,standard,offline,destination):
 publication=Path(publication).resolve();destination=Path(destination).absolute()
 if destination.exists():raise ValueError('Choose a new destination; existing deliveries are preserved')
 standard=Path(standard);offline=Path(offline)
 # Validate exact Skill archive list and hashes against its build manifest before writing.
 core=json.loads(standard.with_name('build-manifest.json').read_text());expected={'event-photo-retouch/'+v['path']:v for v in core['files']}
 with zipfile.ZipFile(standard) as z:
  if set(z.namelist())!=set(expected) or z.testzip() is not None:raise ValueError('Skill archive file list mismatch')
  for name,entry in expected.items():
   if hashlib.sha256(z.read(name)).hexdigest()!=entry['sha256']:raise ValueError('Skill archive hash mismatch')
 for archive in (standard,offline):
  manifest=json.loads(archive.with_name('build-manifest.json').read_text())
  if digest(archive)!=manifest['archive_sha256']:raise ValueError('Archive checksum mismatch')
 files=['README.md','.gitignore',*('site/'+x for x in SITE_FILES),*('docs/'+x for x in DOC_FILES),*('scripts/'+x for x in SCRIPT_FILES)]
 for rel in files:
  src=publication/rel
  if not src.is_file() or src.is_symlink():raise ValueError('Missing plain publication file: '+rel)
 repo=destination/'repository';repo.mkdir(parents=True)
 for rel in files:
  dst=repo/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(publication/rel,dst)
 with zipfile.ZipFile(standard) as z:
  for name,entry in expected.items():
   relative=Path(name).relative_to('event-photo-retouch');dst=repo/'skill'/relative
   dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(z.read(name));dst.chmod((z.getinfo(name).external_attr>>16)&0o777 or 0o644)
 data=json.loads((repo/'site/downloads.json').read_text());assets=destination/'release-assets';assets.mkdir()
 for package,archive in zip(data['packages'],(standard,offline)):
  if package['sha256']!=digest(archive) or package['bytes']!=archive.stat().st_size:raise ValueError('Download metadata mismatch')
  shutil.copy2(archive,assets/package['filename'])
 (assets/'SHA256SUMS.txt').write_text(''.join(p['sha256']+'  '+p['filename']+'\n' for p in data['packages']))
 manifest={'schema':'publication-build-1','version':data['version'],'repository_files':[{'path':str(p.relative_to(repo)),'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(repo.rglob('*')) if p.is_file()], 'release_assets':[{'path':p.name,'bytes':p.stat().st_size,'sha256':digest(p)} for p in sorted(assets.iterdir()) if p.is_file()],'cloud_uploaded':False}
 (destination/'PUBLICATION-MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');return {'repository':str(repo),'assets':str(assets),'files':len(manifest['repository_files'])}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--publication',type=Path,required=True);p.add_argument('--standard',type=Path,required=True);p.add_argument('--offline',type=Path,required=True);p.add_argument('--destination',type=Path,required=True);a=p.parse_args();print(json.dumps(build(a.publication,a.standard,a.offline,a.destination),ensure_ascii=False,indent=2))
