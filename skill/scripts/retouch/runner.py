"""Durable, one-photo-at-a-time workflow; browser choices never contain executable code."""
from copy import deepcopy
import io
import json
from pathlib import Path
import time
import uuid

import numpy as np
from PIL import Image, ImageDraw

from . import engine,tuning
from .common import artifact, canonical_hash, inside, load_json, save_json, sha256, verify_artifacts, write_new, check_executor,renderer_signature

PROFILES=('natural','clear','portrait','keep')


def smoothing_review_current(project,photo):
    ref=photo.get('assistant_technical_review')
    if not ref:return False
    try:
        verify_artifacts(project,[photo['input'],*photo['masks'].values(),ref])
        record=load_json(inside(project,ref['path']));binding=record.get('binding',{})
        return (record.get('schema_version')=='assistant-technical-mask-review-1' and record.get('actor')=='assistant' and
            binding.get('input')==photo['input'] and binding.get('masks')==photo['masks'] and
            all(record.get('checks',{}).get(key) is True for key in
                ('technical_result','visible_skin_complete','exclusions_checked','transitions_checked','texture_checked')))
    except (ValueError,OSError,KeyError):return False


def smoothing_recipe(project,photo,recipe):
    recipe=deepcopy(recipe);deferred=[]
    if recipe.get('skin_smooth',0)>0 and not smoothing_review_current(project,photo):
        deferred=[{'module':'skin_smooth','requested':recipe['skin_smooth'],'reason':'visible_skin_review_required'}]
        recipe['skin_smooth']=0.
    return recipe,deferred


def refs(value):
    if isinstance(value,dict):
        if {'id','path','sha256','role'} <= value.keys(): yield value
        else:
            for child in value.values(): yield from refs(child)
    elif isinstance(value,list):
        for child in value: yield from refs(child)


def save_mask(mask,path):
    buf=io.BytesIO();mask.save(buf,format='PNG');write_new(path,buf.getvalue())


def covered_by(analysis,scope):
    signature=analysis.get('color_signature')
    colors=[value for value in scope.get('color_signatures',[]) if value]
    color_match=bool(signature and colors and min(float(np.max(np.linalg.norm(np.array(signature)-np.array(value),axis=1))) for value in colors)<=15)
    faces=analysis.get('face_colors',[])
    def face_distance(known):
        if len(known)!=len(faces):return float('inf')
        if not faces:return 0.
        distances=np.linalg.norm(np.array(faces)[:,None,:]-np.array(known)[None,:,:],axis=-1)
        return max(float(distances.min(axis=0).max()),float(distances.min(axis=1).max()))
    face_match=any(face_distance(known)<=12 for known in scope.get('face_color_sets',[]))
    return (color_match and face_match and set(analysis['tags'])<=set(scope.get('tags',[])) and
            scope.get('reference_min',255)-15<=analysis['reference_p50']<=scope.get('reference_max',0)+15 and
            analysis['whole']['bright_fraction']<=scope.get('bright_max',0)+.05)


class Runner:
    def __init__(self,store):
        self.store=store;self.project=store.project
        state=store.snapshot()
        def recover(d):
            signature=check_executor()
            changed=d.get('sample_executor')!=signature
            if changed:
                delivered=bool(d['photos']) and all(p['status'] in {'completed','excluded'} for p in d['photos'])
                confirmed=d.get('confirmed_pipeline') or {}
                reusable=confirmed.get('renderer_signature')==renderer_signature()
                d.update(sample_executor=signature,sample_binding=None,samples=[],choice=deepcopy(confirmed.get('choice')) if reusable else None,
                         status='completed' if delivered else 'running' if reusable else 'preparing',revision=d['revision']+1)
                if reusable:d['scope_reuse']=confirmed.get('scope_reuse',False)
            for photo in d['photos']:
                if changed and photo['status'] not in {'completed','excluded'}:
                    photo.update(status='pending',candidates={},approval=None,generation=photo.get('generation',1)+1)
                if photo['status']=='analyzing':photo['status']='pending'
                if photo['status']=='rendering':photo['status']='queued' if photo.get('approval') else 'sample_pending'
                if photo['status']=='completed':
                    try: verify_artifacts(self.project,[photo['input'],photo['output'],photo['export_review']])
                    except (ValueError,OSError):
                        photo.update(status='needs_review',error='原件或成品摘要不符；保留原版本，重试时另存新版')
                        d['status']='needs_review'
        self.store.mutate(recover)

    def _photo(self,state,pid):
        for photo in state['photos']:
            if photo['id']==pid:return photo
        raise ValueError('照片不属于本次活动')

    def _commit_photo(self,pid,photo,expected_input):
        def commit(state):
            current=self._photo(state,pid)
            if current['input']['sha256']!=expected_input:raise ValueError('原图绑定已变化')
            if current.get('generation',1)!=photo.get('generation',1):raise ValueError('工作基准或选区已变化，旧任务结果不再生效')
            current.clear();current.update(photo)
        self.store.mutate(commit)

    def import_folder(self,folder):
        if self.store.snapshot().get('retention_pending'):raise ValueError('正在归档，请稍后追加照片')
        folder=Path(folder).resolve(strict=True)
        if not folder.is_dir():raise ValueError('输入必须为照片文件夹')
        paths=[]
        import os
        for base,dirs,files in os.walk(folder,followlinks=False):
            dirs[:]=sorted(d for d in dirs if not (Path(base)/d).is_symlink())
            paths.extend(Path(base)/f for f in sorted(files) if not f.startswith('.'))
        for source in paths:
            if source.is_symlink():continue
            checksum=sha256(source)
            existing=self.store.snapshot()
            if any(p['input']['sha256']==checksum for p in existing['photos']):continue
            if source.is_relative_to(self.project/'input/photos'):
                target=source
            else:
                relative=source.relative_to(folder)
                target=self.project/'input/photos'/relative
                inside(self.project,target.relative_to(self.project).as_posix(),must_exist=False)
                if target.exists() and sha256(target)!=checksum:
                    target=target.with_name(target.stem+'_'+checksum[:8]+target.suffix)
                inside(self.project,target.relative_to(self.project).as_posix(),must_exist=False)
                write_new(target,source.read_bytes())
            if sha256(source)!=checksum or sha256(target)!=checksum:raise ValueError('复制时源文件变化，不能继续处理')
            item=artifact(self.project,target,'input')
            photo={'id':canonical_hash([item['path'],checksum])[:16],'name':source.name,'input':item,
                   'status':'pending','candidates':{},'error':None,'approval':None,'source_name':source.name,'generation':1}
            def append(state):
                state['photos'].append(photo);state['revision']+=1
                state['status']='running' if state.get('choice') else 'preparing';state['sample_binding']=None
                if not state.get('choice'):state['samples']=[]
            self.store.mutate(append)
        return len(self.store.snapshot()['photos'])

    def _dir(self,state,photo,kind):
        directory=self.project/f'scratch/retouch/runs/{state["run_id"]}/r{state["revision"]}/{photo["id"]}/{kind}-{uuid.uuid4().hex[:8]}'
        inside(self.project,directory.relative_to(self.project).as_posix(),False)
        directory.mkdir(parents=True,exist_ok=False)
        return directory

    def _input(self,photo,config):
        verify_artifacts(self.project,[photo['input']])
        return engine.load_canonical(inside(self.project,photo['input']['path']),config.get('assume_srgb',False))

    def _watermark(self,config):
        value=config.get('watermark')
        if not value:return None
        verify_artifacts(self.project,[value])
        with Image.open(inside(self.project,value['path'])) as im:return im.convert('RGBA')

    def _preview(self,image,path,config,marked=False):
        small=image.copy();small.thumbnail((1600,1600))
        engine.export_image(small,path,{},self._watermark(config) if marked else None,config.get('watermark'))
        return artifact(self.project,path,'preview')

    def _prepare_photo(self,state,photo):
        photo=deepcopy(photo);photo['status']='analyzing';self._commit_photo(photo['id'],photo,photo['input']['sha256'])
        image,meta=self._input(photo,state['config']);directory=self._dir(state,photo,'analysis')
        if state['config'].get('no_vision'):
            regions,person,capability={},None,{'available':False,'reason':'本次未启用系统定位'}
        else:
            inside(self.project,'scratch/retouch/runtime',False)
            regions,person,capability=engine.vision_regions(image,self.project/'scratch/retouch/runtime')
        imported=state['config'].get('regions_file')
        protection_override=None
        if imported:
            document=load_json(inside(self.project,imported))
            prior=document.get(photo['name'])
            if prior:
                if prior.get('source_sha256')!=photo['input']['sha256']:raise ValueError('既有区域不是本张原片，拒绝复用')
                if not regions.get('faces'):regions['faces']=prior.get('faces',[])
                if not regions.get('hands'):regions['hands']=prior.get('hands',[])
                if prior.get('protection'):
                    verify_artifacts(self.project,[prior['protection']])
                    with Image.open(inside(self.project,prior['protection']['path'])) as im:protection_override=im.copy()
        texture=None;skin_override=None;overrides=photo.get('mask_overrides')
        if overrides:
            if (overrides.get('input_sha256')!=photo['input']['sha256'] or overrides.get('coordinates')!='oriented_pixels'
                    or overrides.get('size')!=list(image.size)):
                raise ValueError('人工选区的原片、逻辑方向或尺寸不匹配，拒绝复用')
            verify_artifacts(self.project,[overrides[key] for key in ('skin','protection','texture') if key in overrides])
            if 'texture' in overrides:
                with Image.open(inside(self.project,overrides['texture']['path'])) as im:texture=im.copy()
                if texture.mode!='L' or texture.size!=image.size:raise ValueError('人工纹理选区尺寸或通道不匹配')
            with Image.open(inside(self.project,overrides['skin']['path'])) as im:skin_override=im.copy()
            with Image.open(inside(self.project,overrides['protection']['path'])) as im:protection_override=im.copy()
        skin,protection,coverage=engine.make_masks(image,regions,person,protection_override,skin_override)
        if not overrides and state['config'].get('skin_detector')=='semantic':
            from .skin_runtime import SkinRuntime
            if not hasattr(self,'skin_runtime'):self.skin_runtime=SkinRuntime(self.project)
            proposed,report_ref,segmentation=self.skin_runtime.propose(image,photo,regions)
            skin=Image.fromarray(np.minimum(np.asarray(proposed['skin']),255-np.asarray(protection)))
            texture=Image.fromarray(np.minimum(np.asarray(proposed['texture']),np.asarray(skin)))
            photo['skin_segmentation']=report_ref
            coverage={'method':'four-model original-pixel skin and texture candidates; not automatic approval',
                'skin_fraction':float(np.mean(np.asarray(skin)>128)),'needs_visual_review':True,
                'actual_parts':'whole-image skin plus original-pixel face crops; separate texture eligibility',
                'face_candidates':segmentation['face_candidates'],'mesh_faces':segmentation['mesh_faces'],
                'deferred_texture_faces':segmentation['deferred_texture_faces'],'quality_reasons':segmentation['quality_reasons'],
                'unverified_parts':segmentation['unverified_parts'],'rejected_crops':segmentation['rejected_crops']}
            capability['skin_segmentation']={'available':True,'provider':segmentation['provider'],'quality':'candidate_requires_review'}
        if overrides:
            skin_ref=deepcopy(overrides['skin']);protect_ref=deepcopy(overrides['protection'])
        else:
            save_mask(skin,directory/'skin.png');save_mask(protection,directory/'protection.png')
            skin_ref=artifact(self.project,directory/'skin.png','mask');protect_ref=artifact(self.project,directory/'protection.png','mask')
        mask_refs={'skin':skin_ref,'protection':protect_ref}
        if texture is not None:
            if overrides:mask_refs['texture']=deepcopy(overrides['texture'])
            else:
                save_mask(texture,directory/'texture.png');mask_refs['texture']=artifact(self.project,directory/'texture.png','mask')
        analysis=engine.analyze(image,regions)
        analysis['skin_balance']=engine.balance_recipe(image,regions)
        photo.update(size=list(image.size),analysis=analysis,regions=regions,capability=capability,coverage=coverage,
                     masks=mask_refs,
                     original_preview=self._preview(image,directory/'original.jpg',state['config']),
                     overlay=self._preview(engine.overlay(image,skin,protection),directory/'overlay.jpg',state['config']),
                     input_color=meta['input_color'],status='analyzed',error=None)
        if state.get('choice'):
            scope=state.get('approval_scope',{})
            covered=state.get('scope_reuse',True) and covered_by(analysis,scope)
            if covered:
                photo.update(status='queued',approval=deepcopy(state['approval']),tuning=deepcopy(state.get('confirmed_tuning',{})),
                    scope_inheritance={'scope':scope,'input_sha256':photo['input']['sha256'],'evaluated':analysis})
            else:photo.update(status='sample_pending',approval=None,requires_new_sample=True)
        save_json(directory/'diagnosis.json',{'input':photo['input'],'analysis':analysis,'regions':regions,
                     'capability':capability,'coverage':coverage,'metadata':{k:v for k,v in meta.items() if k!='exif'}})
        photo['diagnosis']=artifact(self.project,directory/'diagnosis.json','report')
        self._commit_photo(photo['id'],photo,photo['input']['sha256'])
        if photo['status']=='sample_pending':
            self.store.mutate(lambda d:d['samples'].append(photo['id']) if photo['id'] not in d['samples'] else None)

    def _choose_samples(self,state):
        available=[p for p in state['photos'] if p.get('analysis') and p['status'] not in {'completed','excluded','error'}]
        if len(available)<=5:return [p['id'] for p in available]
        selections=[]
        for ordered in (sorted(available,key=lambda p:p['analysis']['reference_p50']),
                        sorted(available,key=lambda p:p['analysis']['whole']['bright_fraction'],reverse=True),
                        sorted(available,key=lambda p:len(p['analysis']['subjects']),reverse=True)):
            if ordered[0]['id'] not in selections:selections.append(ordered[0]['id'])
        seen=set()
        for photo in available:
            tags=set(photo['analysis']['tags'])
            if tags-seen and photo['id'] not in selections:selections.append(photo['id'])
            seen|=tags
            if len(selections)>=6:break
        while len(selections)<min(6,len(available)):
            def distance(photo):
                if photo['id'] in selections:return -1
                return min(float(np.max(np.linalg.norm(np.array(photo['analysis']['color_signature'])-np.array(other['analysis']['color_signature']),axis=1))) for other in available if other['id'] in selections)
            farthest=max(available,key=distance)
            if distance(farthest)<=15:break
            selections.append(farthest['id'])
        return selections

    def _candidate(self,state,photo,profile):
        image,meta=self._input(photo,state['config'])
        verify_artifacts(self.project,list(photo['masks'].values()))
        skin=Image.open(inside(self.project,photo['masks']['skin']['path'])).convert('L')
        protection=Image.open(inside(self.project,photo['masks']['protection']['path'])).convert('L')
        if 'skin_balance' not in photo['analysis']:photo['analysis']['skin_balance']=engine.balance_recipe(image,photo['regions'])
        recipe,deferred=self._safe_smoothing_recipe(photo,engine.complete_recipe(profile,photo['analysis'],photo.get('tuning',{}).get(profile,{})))
        binding=canonical_hash({'input':photo['input'],'masks':photo['masks'],'recipe':recipe,
                                'deferred_modules':deferred,'executor':check_executor()})
        prior=photo.get('candidates',{}).get(profile)
        if prior and prior.get('render_binding')==binding:
            verify_artifacts(self.project,list(refs(prior)))
            return prior
        directory=self._dir(state,photo,profile)
        if not photo.get('baseline'):
            engine.save_lossless(image,directory/'baseline.png',meta)
            photo['baseline']=artifact(self.project,directory/'baseline.png','baseline')
        texture=Image.open(inside(self.project,photo['masks']['texture']['path'])).convert('L') if 'texture' in photo['masks'] else None
        output,report=engine.render(image,recipe,skin,protection,texture)
        report['deferred_modules']=deferred
        if deferred:report['notes'].append('可见皮肤覆盖、五官保护、边界过渡及纹理尚未核对通过，磨皮暂不执行')
        engine.save_lossless(output,directory/'master.png',meta)
        save_json(directory/'recipe.json',{'operator_version':'local-pixel-2','input':photo['input'],
                  'baseline':photo['baseline'],'masks':photo['masks'],'parameters':recipe,'units':report['units'],
                  'render_binding':binding})
        save_json(directory/'qa.json',report)
        result={'profile':profile,'executor':check_executor(),'render_binding':binding,'master':artifact(self.project,directory/'master.png','master'),
                'recipe':artifact(self.project,directory/'recipe.json','recipe'),'qa':artifact(self.project,directory/'qa.json','report'),
                'preview':self._preview(output,directory/'preview.jpg',state['config']),
                'report':report,'details':[]}
        if state['config'].get('watermark'):
            result['watermarked_preview']=self._preview(output,directory/'watermarked.jpg',state['config'],True)
            engine.export_image(output,directory/'watermarked-inspection.jpg',meta,self._watermark(state['config']),state['config']['watermark'])
            result['watermarked_inspection']=artifact(self.project,directory/'watermarked-inspection.jpg','detail')
        boxes=[]
        for i,b in enumerate(photo['regions'].get('faces',[])):
            x0,y0,x1,y1=b;fw=x1-x0;fh=y1-y0
            boxes.append((f'面颈 {i+1}',[max(0,x0-.15*fw),max(0,y0-.1*fh),min(1,x1+.15*fw),min(1,y1+.8*fh)]))
        boxes.extend((f'手部 {i+1}',b) for i,b in enumerate(photo['regions'].get('hands',[])))
        if not boxes:boxes=[('画面中心（未可靠定位人物）',[.35,.35,.65,.65])]
        for i,(label,box) in enumerate(boxes[:20]):
            bounds=engine.pixel_box(box,image.size)
            engine.save_lossless(image.crop(bounds),directory/f'detail-{i}-original.png',{})
            engine.save_lossless(output.crop(bounds),directory/f'detail-{i}-result.png',{})
            result['details'].append({'label':label,'pixel_box':list(bounds),
                   'original':artifact(self.project,directory/f'detail-{i}-original.png','detail'),
                   'result':artifact(self.project,directory/f'detail-{i}-result.png','detail')})
        return result

    def _prepare_candidate(self,state,photo,profile):
        photo=deepcopy(photo)
        value=self._candidate(state,photo,profile)
        photo.setdefault('candidates',{})[profile]=value
        if all(k in photo['candidates'] for k in PROFILES):photo['status']='sample_ready'
        self._commit_photo(photo['id'],photo,photo['input']['sha256'])

    def _ready(self):
        state=self.store.snapshot()
        ready=[p for p in state['photos'] if p.get('candidates')]
        if not ready:
            self.store.mutate(lambda d:d.update(status='needs_review',error='没有可处理照片，请检查错误或添加原片'))
            return
        assets=list(refs(state['photos']))
        if state['config'].get('watermark'):assets.append(state['config']['watermark'])
        unique={a['id']:a for a in assets}
        verify_artifacts(self.project,unique.values())
        binding=canonical_hash({'assets':sorted(unique.values(),key=lambda a:a['id']),
                                'samples':state['samples'],'executor':check_executor()})
        def ready_state(d):
            d.update(status='needs_review' if d.get('choice') else 'needs_selection',
                     sample_binding=binding,sample_executor=check_executor(),selection_artifacts=list(unique.values()),error=None)
            request=d.get('pending_request')
            if request and request['status']=='pending':d['status']='needs_changes'
            elif request and request['status'] in {'samples_queued','awaiting_confirmation'}:
                versions={p['id']:p.get('generation',1) for p in d['photos'] if p['id'] in request['photo_ids']}
                approved=[pid for pid in request.get('approved_photo_ids',[]) if request.get('target_generations',{}).get(pid)==versions.get(pid)]
                request.update(status='awaiting_confirmation',sample_binding=binding,target_generations=versions,approved_photo_ids=approved)
        self.store.mutate(ready_state)
        self.store.event('samples_ready',{'run_id':state['run_id'],'revision':state['revision'],'sample_count':len(state['samples'])},
                         f'{state["run_id"]}:samples:{state["revision"]}',notify=state['config'].get('notify_ready',True))

    def _finish_photo(self,state,photo):
        check_executor(state.get('sample_executor'))
        photo=deepcopy(photo);approval=photo.get('approval')
        if not approval:
            photo.update(status='needs_review',error='新条件需要本图代表样片确认')
            self._commit_photo(photo['id'],photo,photo['input']['sha256']);return
        choice=approval['choice'];profile=choice.get('overrides',{}).get(photo['id'],choice['profile'])
        if choice.get('watermark') and approval.get('watermark_snapshot') is not None:
            if approval['watermark_snapshot']!=state['config'].get('watermark'):raise ValueError('水印与确认记录不符，请重新核对样片')
        if approval.get('parameter_snapshot_ref'):verify_artifacts(self.project,[approval['parameter_snapshot_ref']])
        if not photo.get('candidates') and not covered_by(photo['analysis'],state.get('approval_scope',{})):
            photo.update(status='sample_pending',approval=None,requires_new_sample=True,error='本图颜色或场景超出已看样范围，正在补样')
            self._commit_photo(photo['id'],photo,photo['input']['sha256']);return
        recipe=tuning.apply(engine.plan_recipe(profile,photo['analysis']),photo.get('tuning',{}).get(profile,{}))
        needs_skin_review=(profile in {'clear','portrait'} or any(recipe.get(k) for k in ('skin_ev','skin_da','skin_db','skin_smooth')))
        if needs_skin_review and photo['coverage']['skin_fraction']>.0001 and photo['id'] not in choice.get('skin_reviewed',[]) and not self.mask_review_current(photo):
            complete=all(k in photo.get('candidates',{}) for k in PROFILES)
            photo.update(status='needs_review' if complete else 'sample_pending',error='请核对本张皮肤与文字保护选区，或选择基础校正')
            self._commit_photo(photo['id'],photo,photo['input']['sha256']);return
        photo['status']='rendering';self._commit_photo(photo['id'],photo,photo['input']['sha256'])
        candidate=self._candidate(state,photo,profile)
        photo['candidates'][profile]=candidate
        verify_artifacts(self.project,[photo['input']]+list(photo['masks'].values())+list(refs(candidate)))
        report=candidate['report']
        new_clip=max(float(np.max(np.array(report['after'][key])-np.array(report['before'][key]))) for key in ('channel_255','channel_0'))
        if new_clip>.02 and photo['id'] not in choice.get('accept_warnings',[]):
            photo['candidates'][profile]=candidate
            photo.update(status='needs_review',error='新增高亮或暗部截断较多；可降低方案或明确接受此项变化')
            self._commit_photo(photo['id'],photo,photo['input']['sha256']);return
        image,meta=self._input(photo,state['config'])
        with Image.open(inside(self.project,candidate['master']['path'])) as im:master=im.convert('RGB')
        if master.size!=image.size:raise ValueError('工作母版尺寸与本图不符')
        watermark=self._watermark(state['config']) if choice.get('watermark') else None
        name=Path(photo['name']).stem
        suffix='精修_活动水印' if watermark else '精修'
        path=self.project/'output'/f'{name}_{suffix}_{state["run_id"]}_r{approval["revision"]}_{uuid.uuid4().hex[:6]}.jpg'
        inside(self.project,path.relative_to(self.project).as_posix(),False)
        export=engine.export_image(master,path,meta,watermark,state['config'].get('watermark'))
        output_ref=artifact(self.project,path,'output')
        directory=self._dir(state,photo,'export')
        save_json(directory/'export-review.json',{'schema_version':'retouch-export-review-1',
           'binding':{'input':photo['input']['sha256'],'master':candidate['master']['sha256'],'output':output_ref['sha256']},
           'export':export,'approval':approval,'checks':{'geometry':True,'color_profile':True,
           'protected_core_before_watermark':report['protected_core_exact'],'generative_operations':False},
           'visual_review_scope':'human selected candidate and mask version; numeric self-check is not independent photo review'})
        current=self.store.snapshot();live=self._photo(current,photo['id'])
        if live['masks']!=photo['masks'] or live.get('approval')!=photo.get('approval'):
            raise ValueError('处理时选择或选区发生变化，保留版本但不登记为当前成品')
        photo.update(status='completed',output=output_ref,export_review=artifact(self.project,directory/'export-review.json','report'),
                     selected_profile=profile,selected_master=candidate['master'],error=None)
        self._commit_photo(photo['id'],photo,photo['input']['sha256'])
        self.store.event('photo_completed',{'photo_id':photo['id'],'name':photo['name'],'output':output_ref['path']})

    def tick(self):
        state=self.store.snapshot()
        from . import retention
        if (state.get('retention_pending') and state.get('retention_error_binding')!=retention.outputs_binding(state)) or retention.needs_compaction(state):
            try:retention.compact(self.store)
            except Exception as exc:
                self.store.mutate(lambda d:d.update(retention_error=str(exc)[:500],retention_error_binding=retention.outputs_binding(d)))
            return False
        if state['status'] in {'paused','needs_selection','needs_review','needs_changes','completed'} or not state['photos']:return False
        try:check_executor(state.get('sample_executor'))
        except ValueError as exc:
            self.store.mutate(lambda d:d.update(status='needs_review',error=str(exc)));return False
        photos=state['photos']
        pending=next((p for p in photos if p['status']=='pending'),None)
        task=None
        try:
            if pending:
                task=pending;self._prepare_photo(state,pending);return True
            if state['status']=='preparing' and not state.get('samples'):
                selected=self._choose_samples(state)
                if not selected:self._ready();return True
                def choose(d):
                    d['samples']=selected
                    for p in d['photos']:
                        if p['status']=='analyzed':p['status']='sample_pending' if p['id'] in selected else 'queued'
                self.store.mutate(choose);return True
            for photo in photos:
                if photo['status']=='sample_pending':
                    task=photo;profile=next(k for k in PROFILES if k not in photo.get('candidates',{}))
                    self._prepare_candidate(state,photo,profile);return True
            if state['status']=='preparing':self._ready();return True
            photo=next((p for p in photos if p['status'] in {'queued','sample_ready','analyzed'}),None)
            if photo:
                task=photo;self._finish_photo(state,photo);return True
            has_problem=any(p['status'] in {'error','needs_review','change_requested'} for p in photos)
            if has_problem:self._ready()
            else:
                outputs=[p['output'] for p in photos if p.get('output') and p['status']=='completed']
                verify_artifacts(self.project,outputs)
                target=self.project/f'scratch/retouch/delivery-{state["run_id"]}-r{state["revision"]}-{uuid.uuid4().hex[:6]}.json'
                save_json(target,{'run_id':state['run_id'],'outputs':outputs,'photo_count':len(outputs),'activity':state['config']['activity_name'],
                     'excluded':[{'input':p['input'],'reason':p.get('exclude_reason')} for p in photos if p['status']=='excluded']})
                self.store.mutate(lambda d:d.update(status='completed',delivery=artifact(self.project,target,'report')))
                self.store.event('completed',{'run_id':state['run_id'],'photo_count':len(outputs),'delivery':target.relative_to(self.project).as_posix()},
                                 f'{state["run_id"]}:completed:{state["revision"]}',notify=True)
            return True
        except Exception as exc:
            message=str(exc)[:600]
            if task:
                def fail(d):
                    current=self._photo(d,task['id'])
                    if current.get('generation',1)==task.get('generation',1):current.update(status='error',error=message)
                self.store.mutate(fail)
                self.store.event('photo_error',{'photo_id':task['id'],'message':message})
            else:
                self.store.mutate(lambda d:d.update(status='needs_review',error=message))
            return True

    def mask_review_current(self,photo):
        ref=photo.get('assistant_technical_review')
        if not ref:return False
        try:
            verify_artifacts(self.project,[ref]);record=load_json(inside(self.project,ref['path']))
            binding=record.get('binding',{})
            return (record.get('schema_version')=='assistant-technical-mask-review-1' and record.get('actor')=='assistant' and
                    binding.get('input')==photo['input'] and binding.get('masks')==photo['masks'] and
                    record.get('checks',{}).get('technical_result') is True)
        except (ValueError,OSError,KeyError):return False

    def smoothing_review_current(self,photo):
        return smoothing_review_current(self.project,photo)

    def _safe_smoothing_recipe(self,photo,recipe):
        return smoothing_recipe(self.project,photo,recipe)

    def _targets(self,state,scope,pid):
        if scope not in {'batch','current'}:raise ValueError('请选择本批或当前照片')
        if scope=='current':candidates=[self._photo(state,pid)]
        else:
            unfinished=[p for p in state['photos'] if p['status'] not in {'completed','excluded','error'}]
            candidates=unfinished or state['photos']
        result=[p for p in candidates if p['status'] not in {'excluded','error'}]
        if not result or any(not p.get('analysis') for p in result):raise ValueError('请等待目标照片分析完成，或单独处理异常文件')
        return result

    @staticmethod
    def _retain_history(photo):
        if photo.get('output'):photo.setdefault('previous_outputs',[]).append(photo.pop('output'))
        if photo.get('approval'):photo.setdefault('approval_history',[]).append(photo['approval'])
        photo['approval']=None

    def queue_tuning(self,revision,profile,adjustments,scope,pid=None):
        if profile not in PROFILES or profile=='keep':raise ValueError('请先选择一个调色方案，再微调；保留原貌不叠加调色')
        adjustments=tuning.validate(adjustments)
        if adjustments.get('skin_smooth',0)>0 and not tuning.can_smooth():raise ValueError('当前环境没有可用的保边磨皮算子，请将磨皮强度设为0')
        request_id=uuid.uuid4().hex
        def change(d):
            if d['revision']!=revision:raise ValueError('样片版本已变化，请刷新后更新微调')
            check_executor(d.get('sample_executor'))
            if d['status'] not in {'needs_selection','needs_review','needs_changes','completed'}:raise ValueError('请等待当前处理完成后更新微调')
            targets=self._targets(d,scope,pid);ids=[p['id'] for p in targets]
            pending=d.get('pending_request')
            remaining=set(pending['photo_ids'])-set(pending.get('approved_photo_ids',[])) if pending else set()
            if pending and pending['status'] in {'pending','awaiting_confirmation'} and not remaining<=set(ids):raise ValueError('本次微调范围没有覆盖尚待修改的照片，请保持原请求范围')
            for photo in targets:tuning.apply(engine.plan_recipe(profile,photo['analysis']),adjustments)
            record={'schema_version':'retouch-tuning-request-1','request_id':request_id,'source':'tuning_console_or_bound_assistant',
                    'revision_before':revision,'photo_ids':ids,'profile':profile,'adjustments':adjustments,
                    'semantics':'per-photo base deltas; skin_smooth and sharpen are absolute strengths','timestamp':time.time()}
            relative=f'input/activity-materials/参数微调_{request_id}.json';save_json(self.project/relative,record)
            for photo in targets:
                self._retain_history(photo)
                photo.setdefault('tuning',{})[profile]=deepcopy(adjustments)
                photo['candidates']={k:v for k,v in photo.get('candidates',{}).items() if k!=profile and v.get('executor')==check_executor()}
                photo.update(status='sample_pending',error=None,generation=photo.get('generation',1)+1)
                if photo['id'] not in d['samples']:d['samples'].append(photo['id'])
            if pending and pending['status'] in {'pending','awaiting_confirmation'}:
                pending.update(status='samples_queued',tuning_request=relative,
                    approved_photo_ids=[pid for pid in pending.get('approved_photo_ids',[]) if pid not in ids])
            d.update(revision=revision+1,sample_binding=None,status='running' if d.get('choice') else 'preparing')
            return {'accepted':True,'revision':d['revision'],'request_path':relative,'profile':profile,'photo_ids':ids,'adjustments':adjustments}
        return self.store.mutate(change)

    def request_changes(self,revision,text,scope,pid=None,parameter_context=None):
        if not isinstance(text,str) or not text.strip() or len(text)>2000:raise ValueError('请填写简短明确的修改要求')
        if parameter_context is not None:
            if not isinstance(parameter_context,dict) or parameter_context.get('profile') not in PROFILES:raise ValueError('修改要求的方案上下文无效')
            parameter_context={'profile':parameter_context['profile'],'adjustments':tuning.validate(parameter_context.get('adjustments',{})),
                               'watermark':bool(parameter_context.get('watermark',False)),'draft_only':True}
        request_id=uuid.uuid4().hex
        def change(d):
            if d['revision']!=revision:raise ValueError('页面版本已变化，请刷新后提交修改要求')
            was_paused=d['status']=='paused'
            if d.get('pending_request',{}).get('status')=='pending':raise ValueError('已有修改要求待处理，请在当前对话补充，避免重复提交')
            targets=self._targets(d,scope,pid);ids=[p['id'] for p in targets]
            record={'schema_version':'retouch-revision-request-1','request_id':request_id,'status':'pending','text':text.strip(),
                    'photo_ids':ids,'scope':scope,'parameter_context':parameter_context,'sample_binding':d.get('sample_binding'),'revision':revision,'timestamp':time.time()}
            relative=f'input/activity-materials/修改要求_{request_id}.json';save_json(self.project/relative,record)
            for photo in targets:
                self._retain_history(photo);photo.update(status='change_requested',error='修改要求已保存，等待新样片',generation=photo.get('generation',1)+1)
            d.update(pending_request=record,revision=revision+1,sample_binding=None)
            busy=any(p['status'] in {'queued','rendering','pending','sample_pending','analyzing'} for p in d['photos'])
            d['status']='running' if busy else 'needs_changes'
            if was_paused:d.update(paused_from=d['status'],status='paused')
            result={'accepted':True,'request_path':relative,'request_id':request_id,'photo_ids':ids,'text':text.strip(),'parameter_context':parameter_context}
            return result
        result=self.store.mutate(change)
        self.store.event('revision_request',result,f'revision-request:{request_id}',notify=True)
        return result

    def preview_tuning(self,pid,revision,profile,adjustments,marked):
        if profile not in PROFILES or profile=='keep':raise ValueError('保留原貌没有调色微调')
        state=self.store.snapshot()
        if state['revision']!=revision:raise ValueError('快速预览已过期，请刷新')
        check_executor(state.get('sample_executor'));photo=self._photo(state,pid)
        image,_=self._input(photo,state['config']);analysis=deepcopy(photo['analysis'])
        if 'skin_balance' not in analysis:analysis['skin_balance']=engine.balance_recipe(image,photo['regions'])
        image.thumbnail((1200,1200))
        verify_artifacts(self.project,list(photo['masks'].values()))
        masks={key:Image.open(inside(self.project,ref['path'])).convert('L').resize(image.size,Image.Resampling.LANCZOS) for key,ref in photo['masks'].items()}
        recipe,_=self._safe_smoothing_recipe(photo,engine.complete_recipe(profile,analysis,adjustments))
        output,report=engine.render(image,recipe,masks['skin'],masks['protection'],masks.get('texture'))
        if marked:
            mark=self._watermark(state['config'])
            if mark is not None:
                layout=state['config']['watermark'];width=round(min(output.size)*layout.get('width_fraction',.15))
                mark=mark.resize((width,max(1,round(mark.height*width/mark.width))),Image.Resampling.LANCZOS)
                margin=round(min(output.size)*layout.get('margin_fraction',.025));output=output.convert('RGBA')
                output.alpha_composite(mark,(output.width-mark.width-margin,output.height-mark.height-margin));output=output.convert('RGB')
        live=self.store.snapshot()
        if live['revision']!=revision or self._photo(live,pid).get('generation',1)!=photo.get('generation',1):raise ValueError('预览期间版本发生变化，已丢弃旧结果')
        buf=io.BytesIO();output.save(buf,format='JPEG',quality=90,icc_profile=engine.ICC)
        return buf.getvalue(),report['recipe']

    def update_masks(self,pid,revision,input_sha,strokes):
        state=self.store.snapshot();photo=deepcopy(self._photo(state,pid))
        if revision!=state['revision'] or input_sha!=photo['input']['sha256']:raise ValueError('选区页面已过期，请刷新')
        verify_artifacts(self.project,[photo['input']]+list(photo['masks'].values()))
        if not isinstance(strokes,list) or len(strokes)>300:raise ValueError('选区笔画过多')
        masks={key:Image.open(inside(self.project,value['path'])).convert('L') for key,value in photo['masks'].items()}
        for stroke in strokes:
            layer=stroke.get('layer');erase=stroke.get('erase',False)
            if layer not in {'protect','skin'} or stroke.get('tool') not in {'brush','rect'}:raise ValueError('未知选区工具')
            points=np.asarray(stroke.get('points'),float)
            if points.ndim!=2 or points.shape[1]!=2 or not 1<=len(points)<=2000 or not np.isfinite(points).all() or np.any(points<0) or np.any(points>1):raise ValueError('选区笔画坐标无效')
            radius=stroke.get('radius',.02)
            if type(radius) not in (int,float) or not np.isfinite(radius) or not .001<=radius<=.25:raise ValueError('笔刷半径无效')
            keys=['protection'] if layer=='protect' else [key for key in ('skin','texture') if key in masks]
            for key in keys:
                im=masks[key];draw=ImageDraw.Draw(im);w,h=im.size
                coords=[(float(x*w),float(y*h)) for x,y in points];fill=0 if erase else 255
                if stroke['tool']=='rect':
                    xs,ys=zip(*coords);draw.rectangle((min(xs),min(ys),max(xs),max(ys)),fill=fill)
                else:
                    width=max(1,round(radius*min(w,h)*2));draw.line(coords,fill=fill,width=width)
                    for x,y in coords:draw.ellipse((x-width/2,y-width/2,x+width/2,y+width/2),fill=fill)
        directory=self._dir(state,photo,'manual-mask')
        for key,im in masks.items():save_mask(im,directory/(key+'.png'))
        photo['masks']={key:artifact(self.project,directory/(key+'.png'),'mask') for key in masks}
        photo['mask_overrides']={'input_sha256':photo['input']['sha256'],'coordinates':'oriented_pixels',
            'size':list(masks['skin'].size),**deepcopy(photo['masks'])}
        if photo.get('output'):photo.setdefault('previous_outputs',[]).append(photo.pop('output'))
        image,_=self._input(photo,state['config']);photo['overlay']=self._preview(engine.overlay(image,masks['skin'],masks['protection']),directory/'overlay.jpg',state['config'])
        photo.update(status='sample_pending',candidates={},approval=None,error=None,generation=photo.get('generation',1)+1)
        photo['coverage']['skin_fraction']=float(np.mean(np.asarray(masks['skin'])>128))
        def change(d):
            if d['revision']!=revision:raise ValueError('选区版本已变化')
            current=self._photo(d,pid);current.clear();current.update(photo)
            d['revision']+=1;d['sample_binding']=None
            if pid not in d['samples']:d['samples'].append(pid)
            d['status']='running' if d.get('choice') else 'preparing'
        self.store.mutate(change)

    def control(self,action,pid=None,reason=None):
        def change(d):
            if action=='pause':d.update(paused_from=d['status'],status='paused')
            elif action=='resume':
                if d['status']!='paused':raise ValueError('当前未暂停')
                d['status']=d.get('paused_from','running')
            elif action=='retry':
                for photo in d['photos']:
                    if (pid is None or photo['id']==pid) and photo['status'] in {'error','needs_review'}:
                        if not photo.get('analysis'):photo['status']='pending'
                        elif photo.get('approval'):photo['status']='queued'
                        elif not all(k in photo.get('candidates',{}) for k in PROFILES):
                            photo['status']='sample_pending'
                            if photo['id'] not in d['samples']:d['samples'].append(photo['id'])
                        else:photo['status']='needs_review'
                        photo['error']=None
                d['status']='running' if d.get('choice') else 'preparing'
                if not d.get('choice'):d['samples']=[]
            elif action=='exclude':
                if not reason or len(reason)>500:raise ValueError('请填写跳过原因')
                photo=self._photo(d,pid)
                photo.update(status='excluded',error=None,exclude_reason=reason,generation=photo.get('generation',1)+1)
                d['status']='running' if d.get('choice') else 'preparing'
            elif action=='reprepare':
                for photo in d['photos']:
                    if (pid is None or photo['id']==pid) and photo.get('analysis') and photo['status']!='excluded':
                        if photo.get('output'):photo.setdefault('previous_outputs',[]).append(photo.pop('output'))
                        photo.update(status='sample_pending',candidates={},approval=None,error=None,generation=photo.get('generation',1)+1)
                        if photo['id'] not in d['samples']:d['samples'].append(photo['id'])
                d.update(revision=d['revision']+1,sample_binding=None,status='running' if d.get('choice') else 'preparing')
            else:raise ValueError('未知操作')
        self.store.mutate(change)
