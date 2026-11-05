#!/usr/bin/env python3
"""Preview the static site and its exact release attachments on localhost."""
import argparse,functools,http.server,json,mimetypes,os,re
from pathlib import Path
from urllib.parse import unquote,urlsplit

def create_server(root,assets,port=0):
 root=Path(root).resolve();site=root/'site';assets=Path(assets).resolve()
 metadata=json.loads((site/'downloads.json').read_text());allowed={p['filename']:p for p in metadata['packages']}
 class Handler(http.server.SimpleHTTPRequestHandler):
  def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(site),**kwargs)
  def end_headers(self):
   self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');super().end_headers()
  def send_head(self):
   path=unquote(urlsplit(self.path).path)
   if path.startswith('/downloads/'):
    name=path[len('/downloads/'):];item=allowed.get(name);file=assets/name
    if not item or Path(name).name!=name or not file.is_file() or file.is_symlink() or file.stat().st_size!=item['bytes']:
     self.send_error(404,'Release attachment unavailable');return None
    stream=file.open('rb');size=file.stat().st_size;start=0;end=size-1
    value=self.headers.get('Range')
    if value:
     match=re.fullmatch(r'bytes=(\d+)-(\d*)',value)
     if not match:stream.close();self.send_error(416);return None
     start=int(match[1]);end=min(int(match[2]) if match[2] else size-1,size-1)
     if start>=size or end<start:stream.close();self.send_error(416);return None
    self.send_response(206 if value else 200);self.send_header('Content-Type','application/zip');self.send_header('Accept-Ranges','bytes')
    self.send_header('Content-Length',str(end-start+1));self.send_header('Content-Disposition','attachment; filename="'+name+'"')
    if value:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
    self.end_headers();stream.seek(start);self.remaining=end-start+1;return stream
   if path.startswith('/docs/') or path=='/README.md':
    relative=path.lstrip('/');file=root/relative
    if file.is_file() and file.resolve().is_relative_to(root) and not file.is_symlink():
     stream=file.open('rb');self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8');self.send_header('Content-Length',str(file.stat().st_size));self.end_headers();return stream
    self.send_error(404);return None
   return super().send_head()
  def copyfile(self,source,outputfile):
   remaining=getattr(self,'remaining',None)
   if remaining is None:return super().copyfile(source,outputfile)
   while remaining:
    block=source.read(min(1024*1024,remaining))
    if not block:break
    outputfile.write(block);remaining-=len(block)
 server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler);return server

def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--assets',type=Path,required=True);p.add_argument('--port',type=int,default=0);p.add_argument('--receipt',type=Path);a=p.parse_args()
 server=create_server(a.root,a.assets,a.port);record={'pid':os.getpid(),'url':f'http://127.0.0.1:{server.server_port}/','root':str(a.root.resolve())}
 if a.receipt:a.receipt.write_text(json.dumps(record)+'\n')
 print(json.dumps(record),flush=True)
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
if __name__=='__main__':main()
