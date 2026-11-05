import http.cookies
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid
import zipfile

from .callback import dispatch_outbox
from .common import artifact, encoded, inside, save_json, sha256, verify_artifacts, write_new, decode_json
from .runner import Runner, refs
from . import tuning,engine,watermarks

UI_ROOT=Path(__file__).resolve().parents[2]/'assets/photo-review'


class ReviewServer(ThreadingHTTPServer):
    daemon_threads=True
    def server_close(self):
        self.stopping.set()
        super().server_close()
        if hasattr(self,'runner') and hasattr(self.runner,'skin_runtime'):self.runner.skin_runtime.close()


def create_server(store,port=0,start_worker=True):
    runner=Runner(store)
    cookie_name='photo_session_'+store.snapshot()['run_id']
    sessions={};session_lock=threading.Lock();operation_lock=threading.Lock();archive_lock=threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version='PhotoReview/1'
        def log_message(self,*args): pass

        def session(self):
            cookie=http.cookies.SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except http.cookies.CookieError:pass
            sid=cookie.get(cookie_name);sid=sid.value if sid else None
            with session_lock:
                if sid not in sessions:
                    sid=secrets.token_urlsafe(24);sessions[sid]=secrets.token_urlsafe(24);self.new_cookie=sid
                self.csrf=sessions[sid]
            return sid

        def valid_host(self):
            port=self.server.server_address[1]
            return self.headers.get('Host') in {f'127.0.0.1:{port}',f'localhost:{port}'}

        def send_headers(self,status,kind,length):
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(length));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if getattr(self,'new_cookie',None):
                self.send_header('Set-Cookie',f'{cookie_name}={self.new_cookie}; HttpOnly; SameSite=Strict; Path=/')

        def respond(self,status,value):
            body=encoded(value);self.send_headers(status,'application/json; charset=utf-8',len(body));self.end_headers();self.wfile.write(body)

        def file(self,path,download=False):
            kind=mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            self.send_headers(200,kind,path.stat().st_size)
            if download:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(path.name))
            self.end_headers()
            with path.open('rb') as f:
                while data:=f.read(1024*256):self.wfile.write(data)

        def do_GET(self):
            if not self.valid_host():self.respond(403,{'error':'无效的本地访问地址'});return
            self.session();parsed=urlsplit(self.path);path=unquote(parsed.path)
            try:
                if path=='/api/state':
                    state=store.snapshot();state['csrf']=self.csrf
                    state['tuning_controls']=tuning.CONTROLS
                    state['tuning_defaults']={p:tuning.defaults(engine.plan_recipe(p,{'reference_p50':100,'whole':{'bright_fraction':0}})) for p in ('natural','clear','portrait','keep')}
                    state['capabilities']={'skin_smooth':tuning.can_smooth()}
                    for photo in state['photos']:
                        photo['mask_review_current']=runner.mask_review_current(photo)
                        photo['smoothing_review_current']=runner.smoothing_review_current(photo)
                    config=state['config'];config['conversation_bound']=bool(config.pop('conversation_id',None))
                    config.pop('source_folder',None)
                    self.respond(200,state)
                elif path in {'/','/app.js','/style.css'}:
                    self.file(UI_ROOT/('index.html' if path=='/' else path[1:]))
                elif path.startswith('/media/'):
                    identifier=path[len('/media/'):]
                    item=next((a for a in refs(store.snapshot()) if a['id']==identifier),None)
                    if not item:self.respond(404,{'error':'文件未登记或版本已变化'});return
                    verify_artifacts(store.project,[item])
                    self.file(inside(store.project,item['path']),parse_qs(parsed.query).get('download')==['1'])
                elif path=='/api/download-all':
                    with archive_lock:
                        state=store.snapshot();outputs=[p['output'] for p in state['photos'] if p.get('output') and p['status']=='completed']
                        if not outputs:raise ValueError('尚无正式成品，请先确认样片并完成处理')
                        verify_artifacts(store.project,outputs)
                        key=sha256_json(outputs)
                        known=state.get('archives',{}).get(key)
                        if known:
                            verify_artifacts(store.project,[known]);target=inside(store.project,known['path'])
                        else:
                            target=store.directory/f'交付_{state["run_id"]}_{uuid.uuid4().hex[:8]}.zip'
                            inside(store.project,target.relative_to(store.project).as_posix(),False)
                            with zipfile.ZipFile(target,'x',zipfile.ZIP_STORED,allowZip64=True) as archive:
                                for output in outputs:archive.write(inside(store.project,output['path']),Path(output['path']).name)
                                archive.writestr('交付清单.json',encoded({'activity':state['config']['activity_name'],'outputs':outputs}))
                            item=artifact(store.project,target,'archive')
                            store.mutate(lambda d:d.setdefault('archives',{}).update({key:item}))
                    self.file(target,True)
                else:self.respond(404,{'error':'页面不存在'})
            except BrokenPipeError:pass
            except (ValueError,OSError,KeyError) as exc:self.respond(409,{'error':str(exc)[:500]})

        def do_POST(self):
            self.session()
            origin=self.headers.get('Origin')
            if (not self.valid_host() or origin!=f'http://{self.headers.get("Host")}' or
                    not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),self.csrf)):
                self.respond(403,{'error':'页面已失效或来源不符，请刷新本地审片页'});return
            path=urlsplit(self.path).path
            try:
                length=int(self.headers.get('Content-Length','0'))
                limit=256*1024*1024 if path=='/api/import' else 2*1024*1024
                if not 0<length<=limit:raise ValueError('请求为空或文件过大')
                raw=self.rfile.read(length)
                if len(raw)!=length:raise ValueError('文件接收不完整')
                if path=='/api/import':
                    name=unquote(self.headers.get('X-File-Name',''))
                    if not name or len(name)>240 or '/' in name or '\\' in name or name.startswith('.') or '\x00' in name:
                        raise ValueError('文件名无效')
                    target=store.project/'input/photos'/name
                    inside(store.project,target.relative_to(store.project).as_posix(),False)
                    if target.exists() and sha256(target)!=__import__('hashlib').sha256(raw).hexdigest():
                        target=target.with_name(target.stem+'_'+__import__('hashlib').sha256(raw).hexdigest()[:8]+target.suffix)
                    inside(store.project,target.relative_to(store.project).as_posix(),False)
                    write_new(target,raw);self.respond(200,{'received':True,'name':target.name});return
                value=decode_json(raw)
                if store.snapshot().get('retention_pending'):raise ValueError('正在收敛归档，请稍后操作')
                if path=='/api/import-done':
                    with operation_lock:count=runner.import_folder(store.project/'input/photos')
                    self.respond(200,{'count':count});return
                if path=='/api/decision':
                    result=store.decide(value['revision'],value['binding'],value['choice'],value['request_id'])
                elif path=='/api/tuning-preview':
                    picture,parameters=runner.preview_tuning(value['photo_id'],value['revision'],value['profile'],value['adjustments'],value.get('watermark',False))
                    result={'image_url':'data:image/jpeg;base64,'+base64.b64encode(picture).decode(),
                            'parameters':parameters,'draft_only':True,'revision':value['revision']}
                elif path=='/api/tuning':
                    with operation_lock:result=runner.queue_tuning(value['revision'],value['profile'],value['adjustments'],value['scope'],value.get('photo_id'))
                elif path=='/api/watermark-selection':
                    with operation_lock:result=watermarks.select(store,value['revision'],value['option_id'])
                elif path=='/api/revision-request':
                    with operation_lock:result=runner.request_changes(value['revision'],value['text'],value['scope'],value.get('photo_id'),value.get('parameter_context'))
                elif path=='/api/mask':
                    with operation_lock:
                        runner.update_masks(value['photo_id'],value['revision'],value['input_sha256'],value['strokes'])
                        result={'accepted':True,'status':'preparing','revision':store.snapshot()['revision']}
                elif path=='/api/control':
                    action=value.get('action')
                    if action=='callback-test':
                        config=store.snapshot()['config']
                        if not config.get('conversation_id') or not config.get('callback_enabled'):raise ValueError('此活动尚未启用对话回传，请由当前对话完成绑定')
                        event_id='callback-test-'+uuid.uuid4().hex[:12]
                        store.event('callback_test',{'test_only':True,'photos_approved':False},event_id,notify=True)
                        result={'event_id':event_id}
                    elif action=='callback-retry':
                        with store.transaction() as db:
                            row=db.execute('SELECT status FROM outbox WHERE event_id=?',(value['event_id'],)).fetchone()
                            if not row or row[0] not in {'failed','delivery_unknown'}:raise ValueError('该事件不需要重发')
                        store.receipt(value['event_id'],'pending',{'explicit_human_retry':True});result={'accepted':True}
                    elif action=='assume-srgb':
                        store.mutate(lambda d:d['config'].update(assume_srgb=True))
                        runner.control('retry');result={'accepted':True}
                    else:
                        runner.control(action,value.get('photo_id'),value.get('reason'));result={'accepted':True}
                    event={'source':'human_review_console','request':value,'time':time.time()}
                    save_json(store.project/f'input/activity-materials/网页操作_{uuid.uuid4().hex}.json',event)
                else:self.respond(404,{'error':'未知操作'});return
                self.respond(200,result)
            except (ValueError,KeyError,TypeError,OSError) as exc:self.respond(409,{'error':str(exc)[:500]})

    server=ReviewServer(('127.0.0.1',port),Handler)
    server.stopping=threading.Event();server.runner=runner
    url=f'http://127.0.0.1:{server.server_address[1]}'
    store.mutate(lambda d:d.update(server={'url':url,'started':time.time()}))
    if start_worker:
        def work():
            while not server.stopping.is_set():
                try:
                    with operation_lock:busy=runner.tick()
                except Exception as exc:
                    store.event('worker_error',{'message':str(exc)[:400]});busy=False
                server.stopping.wait(.05 if busy else .7)
        def notify():
            while not server.stopping.is_set():
                try:busy=dispatch_outbox(store)
                except Exception:busy=False
                server.stopping.wait(.2 if busy else 1.)
        threading.Thread(target=work,daemon=True).start();threading.Thread(target=notify,daemon=True).start()
    return server


def sha256_json(value):
    from .common import canonical_hash
    return canonical_hash(value)
