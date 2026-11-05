"""Keep the selected processing chain; retire derived files to same-volume Trash."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import time
import uuid
import zipfile

from .common import artifact, canonical_hash, inside, load_json, renderer_signature, save_json, sha256, verify_artifacts

ROOTS=('scratch/retouch/runs','scratch/retouch/runtime','scratch/masters','scratch/samples',
       'scratch/previews','scratch/masks','scratch/lut-assets','scratch/analysis','scratch/watermark')


def refs(value):
    if isinstance(value,dict):
        if {'id','path','sha256','role'}<=value.keys():yield value
        else:
            for child in value.values():yield from refs(child)
    elif isinstance(value,list):
        for child in value:yield from refs(child)


def outputs_binding(state):
    return canonical_hash([p.get('output') for p in state['photos']])


def needs_compaction(state):
    return (state['status']=='completed' and state['config'].get('retention_policy')=='selected_only'
            and state.get('retention_last',{}).get('outputs_binding')!=outputs_binding(state)
            and state.get('retention_error_binding')!=outputs_binding(state))


def adopt_existing_confirmation(store):
    """Explicit legacy migration: verify the human record and unchanged effective recipes."""
    from . import engine
    def adopt(state):
        if state['status']!='completed' or not state.get('approval'):raise ValueError('仅迁移已交付且有真实确认记录的活动')
        approval=state['approval'];request=approval.get('request_id','')
        if not request or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in request):raise ValueError('确认编号无效')
        path=inside(store.project,f'input/activity-materials/网页选择_{request}.json');record=load_json(path)
        if record.get('source')!='human_review_console' or record.get('choice')!=approval['choice']:raise ValueError('原始用户确认不匹配')
        selected=[]
        for photo in state['photos']:
            if photo['status']=='excluded':continue
            if photo['status']!='completed' or photo.get('approval',{}).get('request_id')!=request:raise ValueError('逐图确认不一致，不能统一迁移')
            profile=photo['selected_profile'];candidate=photo['candidates'][profile]
            expected=approval['choice'].get('overrides',{}).get(photo['id'],approval['choice']['profile'])
            if profile!=expected or candidate['master']!=photo['selected_master']:raise ValueError('所选方案与母版不匹配')
            verify_artifacts(store.project,[photo['input'],photo['output'],photo['export_review'],candidate['master'],candidate['recipe']])
            old=candidate['report']['recipe'];analysis=deepcopy(photo['analysis']);analysis['skin_balance']=old.get('skin_balance',[])
            current=engine.complete_recipe(profile,analysis,photo.get('tuning',{}).get(profile,{}))
            if any(old.get(k,0)!=current.get(k,0) for k in set(old)|set(current)):raise ValueError('当前调色实现与旧配方不同，需重新看样')
            selected.append(photo)
        if not selected or not state.get('approval_scope'):raise ValueError('缺少本批适用范围')
        values=[p.get('tuning',{}).get(approval['choice']['profile'],{}) for p in selected]
        uniform=all(value==values[0] for value in values)
        state['choice']=deepcopy(approval['choice'])
        state['confirmed_tuning']={approval['choice']['profile']:deepcopy(values[0])} if uniform else {}
        state['scope_reuse']=state.get('scope_reuse',False) and uniform
        state['confirmed_pipeline']={'choice':deepcopy(state['choice']),'renderer_signature':renderer_signature(),
              'scope_reuse':state['scope_reuse'],'confirmation_ref':artifact(store.project,path,'user_confirmation'),
              'migration':'explicit same-activity continuation; effective recipes verified equal','timestamp':time.time()}
        state['config']['retention_policy']='selected_only'
    store.mutate(adopt)


def _closure(project,value):
    kept={};queue=list(refs(value))
    while queue:
        item=queue.pop();relative=item['path']
        if relative in kept:continue
        verify_artifacts(project,[item]);kept[relative]=item
        path=inside(project,relative)
        if path.suffix=='.json':queue.extend(refs(load_json(path)))
    return kept


def _state_to_keep(store,state):
    if state['status']!='completed' or not state['photos']:raise ValueError('仅在本批导出核验完成后收敛；未完成样片不清理')
    result=deepcopy(state)
    for photo in result['photos']:
        if photo['status'] not in {'completed','excluded'}:raise ValueError('尚有未完成照片')
        if photo['status']=='completed':
            profile=photo.get('selected_profile');candidate=photo.get('candidates',{}).get(profile)
            if not candidate or candidate['master']!=photo.get('selected_master'):raise ValueError('缺少已选方案的完整母版链')
            verify_artifacts(store.project,[photo['input'],photo['output'],photo['export_review']])
            photo['candidates']={profile:candidate}
            photo['retained_selected']=True
        else:photo['candidates']={}
    config=result['config'];options=config.pop('watermark_options',[])
    if options:config['watermark_options_history']=[{k:v for k,v in item.items() if k in {'option_id','kind','label'}} for item in options]
    outputs=[p['output'] for p in result['photos'] if p.get('output') and p['status']=='completed']
    # Reuse an already delivered ZIP instead of keeping a second download cache.
    for path in sorted((store.project/'output').glob('*.zip')):
        inside(store.project,path.relative_to(store.project).as_posix())
        try:
            with zipfile.ZipFile(path) as archive:
                names=archive.namelist()
                if all(Path(item['path']).name in names and hashlib.sha256(archive.read(Path(item['path']).name)).hexdigest()==item['sha256'] for item in outputs):
                    result['archives']={canonical_hash(outputs):artifact(store.project,path,'archive')};break
        except (ValueError,OSError,zipfile.BadZipFile):continue
    result['selection_artifacts']=[]
    live={key:result.get(key) for key in ('photos','config','approval','confirmed_pipeline','delivery','archives')}
    if config.get('regions_file'):live['regions_manifest']=load_json(inside(store.project,config['regions_file']))
    kept=_closure(store.project,live)
    result['selection_artifacts']=list(kept.values())
    return result,kept


def plan(store):
    state=store.snapshot();result,kept=_state_to_keep(store,state)
    managed=[];directories=[]
    for relative in ROOTS:
        root=inside(store.project,relative,False)
        if not root.exists():continue
        directories.append(root)
        for path in root.rglob('*'):
            if path.is_symlink():raise ValueError('清理范围含符号链接，已停止')
            if path.is_file():managed.append(path)
            elif path.is_dir():directories.append(path)
    for path in store.directory.glob('交付_*.zip'):managed.append(path)
    files=[]
    for path in sorted(set(managed)):
        relative=path.relative_to(store.project).as_posix()
        inside(store.project,relative)
        if relative not in kept:files.append({'path':relative,'type':'file','bytes':path.stat().st_size,'sha256':sha256(path)})
    retired=[]
    for directory in sorted(set(directories),key=lambda p:-len(p.parts)):
        relative=directory.relative_to(store.project).as_posix()
        if not any(p.startswith(relative+'/') for p in kept):
            retired.append({'path':relative,'type':'empty_after_file_moves','trash_name':canonical_hash(relative)[:16]+'-'+directory.name[:80]})
    return state,result,{'schema_version':'retouch-retention-plan-1','run_id':state['run_id'],
          'state_seq':state['seq'],'source_project':str(store.project),'files':files,'directories':retired,'file_count':len(files),
          'bytes':sum(item['bytes'] for item in files),'keep_paths':sorted(kept),
          'protected_roots':['input','output'],'reason':'selected-only retention after verified export',
          'authorization':'workspace user requested automatic retirement of unselected and superseded trials',
          'created_at':time.time()}


def _resume(store,pending):
    manifest=load_json(inside(store.project,pending['manifest']))
    trash=Path(manifest['trash_directory'])
    if trash.is_symlink() or not trash.is_dir() or trash.stat().st_dev!=store.project.stat().st_dev:raise ValueError('废纸篓位置不符')
    for item in manifest['files']:
        source=inside(store.project,item['path'],False);target=trash/item['path']
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):raise ValueError('废纸篓目标包含软链')
        if source.exists():
            if target.exists() or sha256(source)!=item['sha256']:raise ValueError('待清理文件已改变或目标已存在，停止并保留清单')
            target.parent.mkdir(parents=True,exist_ok=True);os.rename(source,target)
        if source.exists() or not target.is_file() or sha256(target)!=item['sha256']:raise ValueError('移入废纸篓后校验失败')
    # Move now-empty obsolete directories as well; no permanent directory deletion.
    for item in manifest.get('directories',[]):
        directory=inside(store.project,item['path'],False);target=trash/'empty-directories'/item['trash_name']
        if directory.exists():
            if any(directory.iterdir()) or target.exists():raise ValueError('待归档目录状态变化，保留清单等待核对')
            target.parent.mkdir(parents=True,exist_ok=True);os.rename(directory,target)
        if not target.is_dir() or directory.exists():raise ValueError('空目录移入废纸篓后核验失败')
    record={'schema_version':'retouch-retention-result-1','file_count':manifest['file_count'],'bytes_moved':manifest['bytes'],
            'trash_directory':str(trash),'trash_verified':True,'permanently_deleted':False,
            'outputs_binding':pending['outputs_binding'],'manifest':pending['manifest'],'completed_at':time.time()}
    path=inside(store.project,pending['result'],False)
    if path.exists():
        prior=load_json(path)
        if prior.get('manifest')!=pending['manifest'] or not prior.get('trash_verified'):raise ValueError('归档结果记录冲突')
        record=prior
    else:save_json(path,record)
    def finish(state):
        state['retention_pending']=None;state['retention_last']=dict(record,result_ref=artifact(store.project,path,'retention_report'))
        state.pop('retention_error_binding',None)
        state.pop('retention_error',None)
    store.mutate(finish)
    return record


def compact(store,trash_root=None):
    current=store.snapshot()
    if current.get('retention_pending'):return _resume(store,current['retention_pending'])
    old,new_state,manifest=plan(store)
    trash_root=Path(trash_root) if trash_root is not None else Path.home()/'.Trash'
    if trash_root.is_symlink() or not trash_root.is_dir() or trash_root.stat().st_dev!=store.project.stat().st_dev:
        raise ValueError('需要同一磁盘上可核验的用户废纸篓，不自动永久删除')
    trash_root=trash_root.resolve()
    identifier=uuid.uuid4().hex
    directory=store.directory/'retention'/identifier
    inside(store.project,directory.relative_to(store.project).as_posix(),False);directory.mkdir(parents=True)
    trash=trash_root/f'修图归档_{old["run_id"]}_{identifier[:12]}'
    trash.mkdir(exist_ok=False);manifest['trash_directory']=str(trash)
    save_json(directory/'state-before.json',{k:v for k,v in old.items() if k not in {'events','outbox'}})
    save_json(directory/'plan.json',manifest)
    pending={'manifest':(directory/'plan.json').relative_to(store.project).as_posix(),
             'result':(directory/'result.json').relative_to(store.project).as_posix(),'outputs_binding':outputs_binding(old)}
    def commit(state):
        if state['seq']!=old['seq']:raise ValueError('清理规划后活动状态变化，停止并保留文件')
        # Persist the complete live chain before retiring files. A partial move remains resumable.
        metadata={k:deepcopy(v) for k,v in new_state.items() if k not in {'events','outbox'}}
        state.clear();state.update(metadata);state['retention_pending']=pending
    store.mutate(commit)
    return _resume(store,pending)
