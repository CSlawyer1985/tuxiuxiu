#!/usr/bin/env python3
"""Local batch photo workflow: init, prepare, serve, inspect and acknowledge callbacks."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from retouch.common import artifact, load_json, inside
from retouch.store import Store
from retouch.runner import Runner


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--project',type=Path,required=True)
    init.add_argument('--source',type=Path);init.add_argument('--name',required=True)
    init.add_argument('--conversation');init.add_argument('--watermark',type=Path)
    init.add_argument('--regions-file');init.add_argument('--no-vision',action='store_true')
    init.add_argument('--assume-srgb',action='store_true');init.add_argument('--enable-callback',action='store_true')
    init.add_argument('--skin-detector',choices=['legacy','semantic'],default='semantic')
    for command in ('serve','start','prepare','status','callback-test','ack','enable-callback'):
        p=sub.add_parser(command);p.add_argument('--project',type=Path,required=True)
        if command in {'serve','start'}:p.add_argument('--port',type=int,default=0)
        if command=='ack':p.add_argument('--event-id',required=True)
    tune=sub.add_parser('tune');tune.add_argument('--project',type=Path,required=True)
    tune.add_argument('--profile',choices=['natural','clear','portrait'],required=True)
    tune.add_argument('--parameters',required=True,help='activity-relative JSON adjustment file')
    tune.add_argument('--scope',choices=['batch','current'],default='batch');tune.add_argument('--photo-id')
    changes=sub.add_parser('request-changes');changes.add_argument('--project',type=Path,required=True)
    changes.add_argument('--text',required=True);changes.add_argument('--scope',choices=['batch','current'],default='batch');changes.add_argument('--photo-id')
    marks=sub.add_parser('set-watermarks');marks.add_argument('--project',type=Path,required=True)
    marks.add_argument('--manifest',required=True,help='activity-relative three-candidate JSON manifest')
    compact=sub.add_parser('compact');compact.add_argument('--project',type=Path,required=True)
    compact.add_argument('--apply',action='store_true');compact.add_argument('--adopt-existing-confirmation',action='store_true')
    args=parser.parse_args();store=Store(args.project)
    if args.command=='init':
        config={'activity_name':args.name,'conversation_id':args.conversation,'callback_enabled':args.enable_callback,'skin_detector':args.skin_detector,
                'assume_srgb':args.assume_srgb,'no_vision':args.no_vision,'regions_file':args.regions_file,
                'notify_ready':True,'retention_policy':'selected_only'}
        if args.watermark:
            file=args.watermark.resolve()
            if not file.is_relative_to(store.project):raise ValueError('水印先归入活动素材目录')
            config['watermark']=dict(artifact(store.project,file,'watermark'),position='bottom_right',width_fraction=.15,margin_fraction=.025)
        if args.skin_detector=='semantic':
            import setup_runtime
            setup_runtime.active_runtime();setup_runtime.model_path()
        store.initialize(config)
        count=Runner(store).import_folder(args.source) if args.source else 0
        print(json.dumps({'initialized':True,'photos':count,'project':str(store.project)},ensure_ascii=False))
    elif args.command=='tune':
        runner=Runner(store);state=store.snapshot()
        parameters=load_json(inside(store.project,args.parameters))
        print(json.dumps(runner.queue_tuning(state['revision'],args.profile,parameters,args.scope,args.photo_id),ensure_ascii=False))
    elif args.command=='request-changes':
        runner=Runner(store);state=store.snapshot()
        print(json.dumps(runner.request_changes(state['revision'],args.text,args.scope,args.photo_id),ensure_ascii=False))
    elif args.command=='set-watermarks':
        from retouch import watermarks
        print(json.dumps(watermarks.register(store,load_json(inside(store.project,args.manifest))),ensure_ascii=False))
    elif args.command=='compact':
        from retouch import retention
        inside(store.project,'scratch/retouch/service.lock',False)
        with (store.directory/'service.lock').open('a+') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('归档前须停止本活动服务；自动归档由运行服务串行执行')
            if args.adopt_existing_confirmation:
                if not args.apply:raise ValueError('确认迁移仅与 apply 一起执行')
                retention.adopt_existing_confirmation(store)
            result=retention.compact(store) if args.apply else retention.plan(store)[2]
            print(json.dumps({k:v for k,v in result.items() if k not in {'files','keep_paths'}},ensure_ascii=False))
    elif args.command=='prepare':
        runner=Runner(store)
        while runner.tick():
            state=store.snapshot()
            print(json.dumps({'status':state['status'],'processed':sum(bool(p.get('analysis')) for p in state['photos'])}),flush=True)
        print(json.dumps({'status':store.snapshot()['status']},ensure_ascii=False))
    elif args.command=='serve':
        inside(store.project,'scratch/retouch/service.lock',False)
        lock=(store.directory/'service.lock').open('a+')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('本活动审片服务已在运行')
        # A process may have died after sending but before saving the transport receipt.
        with store.transaction() as db:db.execute("UPDATE outbox SET status='delivery_unknown' WHERE status='sending'")
        from retouch.service import create_server
        server=create_server(store,args.port)
        store.mutate(lambda d:d['server'].update(pid=os.getpid()))
        print(json.dumps(store.snapshot()['server']),flush=True)
        try:server.serve_forever()
        finally:server.server_close();lock.close()
    elif args.command=='start':
        state=store.snapshot();old=state.get('server',{})
        if old.get('pid'):
            try:
                import urllib.request
                with urllib.request.urlopen(old['url']+'/api/state',timeout=1) as response:
                    body=json.load(response)
                    if response.status==200 and body.get('run_id')==state['run_id'] and body.get('server',{}).get('pid')==old['pid']:
                        print(json.dumps({'url':old['url'],'pid':old['pid'],'reused':True}));return
            except (OSError,ValueError):pass
        log=(store.directory/f'service-{uuid.uuid4().hex[:8]}.log').open('ab')
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'serve','--project',str(store.project),'--port',str(args.port)],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        for _ in range(100):
            state=store.snapshot();info=state.get('server',{})
            if info.get('pid')==child.pid:
                print(json.dumps({'url':info['url'],'pid':child.pid,'reused':False}));return
            if child.poll() is not None:raise ValueError('服务未能启动，请检查活动内 service 日志')
            time.sleep(.1)
        raise ValueError('服务启动仍未就绪，请检查活动状态；不要重复启动')
    elif args.command=='status':
        state=store.snapshot()
        print(json.dumps({'status':state['status'],'revision':state['revision'],'server':state.get('server'),
              'photos':[{'id':p['id'],'name':p['name'],'status':p['status'],'error':p.get('error')} for p in state['photos']],
              'outbox':[{'event_id':r['event_id'],'status':r['status']} for r in store.outbox()]},ensure_ascii=False,indent=2))
    elif args.command=='callback-test':
        event='callback-test-'+uuid.uuid4().hex[:12]
        store.event('callback_test',{'test_only':True,'photos_approved':False},event,notify=True)
        from retouch.callback import dispatch_outbox
        dispatch_outbox(store)
        print(json.dumps({'event_id':event,'outbox_status':[{'event_id':r['event_id'],'status':r['status']} for r in store.outbox()]},ensure_ascii=False))
    elif args.command=='enable-callback':
        if not store.snapshot()['config'].get('conversation_id'):raise ValueError('须先绑定当前对话 ID')
        store.mutate(lambda d:d['config'].update(callback_enabled=True))
        print(json.dumps({'enabled':True}))
    elif args.command=='ack':
        if not any(r['event_id']==args.event_id for r in store.outbox()):raise ValueError('未知回传事件')
        store.receipt(args.event_id,'observed',{'observed_by':'bound_conversation','timestamp':time.time(),'wake_verified':True})
        print(json.dumps({'observed':args.event_id}))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(1)
