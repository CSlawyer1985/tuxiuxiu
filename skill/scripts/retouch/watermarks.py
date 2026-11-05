"""Register separately designed transparent assets; a browser choice never generates pixels."""
from copy import deepcopy
import math
import re
import time
import uuid

from PIL import Image

from . import engine
from .common import artifact, check_executor, inside, save_json, verify_artifacts

KINDS = {'text_only', 'with_logo', 'creative'}


def _unconfirmed(state):
    if state.get('choice') or any(p.get('approval') or p.get('output') or p['status']=='completed' for p in state['photos']):
        raise ValueError('本活动已有确认或成品，沿用已确认水印；改版须另行明确授权')
    if state['status'] not in {'needs_selection', 'needs_review', 'needs_changes'}:
        raise ValueError('请等待代表样片准备完成后选择水印')


def register(store, manifest):
    """Called by the assistant after three assets and representative samples exist."""
    state=store.snapshot();_unconfirmed(state);check_executor(state.get('sample_executor'))
    if state['config'].get('watermark') or state['config'].get('watermark_options'):
        raise ValueError('已有用户提供的水印或候选，不自动替换')
    if not isinstance(manifest,dict) or set(manifest)!={'candidates'}:
        raise ValueError('水印清单须包含 candidates')
    items=manifest['candidates']
    if not isinstance(items,list) or len(items)!=3 or any(not isinstance(v,dict) for v in items):
        raise ValueError('请提供三版独立水印')
    if {v.get('kind') for v in items}!=KINDS:raise ValueError('三版须分别为无 Logo、有 Logo、自由设计')
    options=[];ids=set();hashes=set()
    samples=[p for p in state['photos'] if p['id'] in state['samples'] and p.get('baseline')]
    if not samples:raise ValueError('请先生成本批代表样片')
    for item in items:
        if set(item)-{'id','label','kind','path','width_fraction','margin_fraction'}:raise ValueError('水印清单包含未知字段')
        identifier=item.get('id');label=item.get('label')
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,60}',identifier) or identifier in ids:
            raise ValueError('水印编号无效或重复')
        if not isinstance(label,str) or not 1<=len(label)<=60:raise ValueError('水印名称无效')
        path=inside(store.project,item['path'])
        if not path.is_relative_to(store.project/'scratch/watermark'):raise ValueError('新水印放在活动 scratch/watermark 内')
        with Image.open(path) as image:
            image.load()
            if image.format!='PNG' or image.mode!='RGBA' or max(image.size)>8192:
                raise ValueError('候选须为不超过8192像素的透明 RGBA PNG')
            alpha=image.getchannel('A');low,high=alpha.getextrema()
            if low==255 or high==0:raise ValueError('水印须包含透明背景与可见内容')
        ref=artifact(store.project,path,'watermark')
        if ref['sha256'] in hashes:raise ValueError('三版不能是同一张水印')
        layout={}
        for key,default,minimum,maximum in [('width_fraction',.18,.05,.30),('margin_fraction',.025,.01,.08)]:
            value=item.get(key,default)
            if type(value) not in {int,float} or not math.isfinite(value) or not minimum<=value<=maximum:
                raise ValueError('水印比例或边距超出范围')
            layout[key]=value
        mark=dict(ref,position='bottom_right',**layout)
        options.append({'option_id':identifier,'label':label,'kind':item['kind'],'asset':mark,'previews':[]})
        ids.add(identifier);hashes.add(ref['sha256'])
    directory=store.project/'scratch/watermark'/('网页候选_'+uuid.uuid4().hex[:12])
    inside(store.project,directory.relative_to(store.project).as_posix(),False);directory.mkdir()
    for option in options:
        with Image.open(inside(store.project,option['asset']['path'])) as mark:
            for photo in samples:
                base=photo.get('candidates',{}).get('natural',{}).get('master') or photo['baseline']
                verify_artifacts(store.project,[base,option['asset']])
                with Image.open(inside(store.project,base['path'])) as im:
                    picture=im.convert('RGB');picture.thumbnail((1200,1200))
                target=directory/f'{option["option_id"]}-{photo["id"]}.jpg'
                engine.export_image(picture,target,{},mark,option['asset'])
                option['previews'].append({'photo_id':photo['id'],'name':photo['name'],'image':artifact(store.project,target,'watermark_preview')})
    def commit(current):
        if current['revision']!=state['revision']:raise ValueError('样片已变化，请重新制作水印排版预览')
        _unconfirmed(current)
        if current['config'].get('watermark') or current['config'].get('watermark_options'):raise ValueError('已有水印，不覆盖')
        current['config']['watermark_options']=options
        current['config']['watermark_selection']=None
        current['revision']+=1;current['sample_binding']=None;current['status']='preparing'
        return {'registered':3,'revision':current['revision'],'photos_approved':False}
    return store.mutate(commit)


def select(store, revision, option_id):
    """Human chooses a watermark draft, then must confirm the rebuilt full samples."""
    request_id=uuid.uuid4().hex
    def commit(state):
        if state['revision']!=revision:raise ValueError('样片版本已变化，请刷新后选择')
        _unconfirmed(state);check_executor(state.get('sample_executor'))
        option=next((v for v in state['config'].get('watermark_options',[]) if v['option_id']==option_id),None)
        if option is None:raise ValueError('水印候选不属于本活动')
        verify_artifacts(store.project,[option['asset']]+[p['image'] for p in option['previews']])
        if state['config'].get('watermark_selection')==option_id:
            return {'accepted':True,'unchanged':True,'revision':revision,'photos_approved':False}
        record={'schema_version':'watermark-draft-selection-1','request_id':request_id,'source':'human_review_console',
                'revision_before':revision,'option_id':option_id,'label':option['label'],'kind':option['kind'],
                'asset':deepcopy(option['asset']),'photos_approved':False,'timestamp':time.time()}
        relative=f'input/activity-materials/水印选版_{request_id}.json';save_json(store.project/relative,record)
        state['config'].update(watermark=deepcopy(option['asset']),watermark_selection=option_id,
                               watermark_selection_ref=artifact(store.project,store.project/relative,'watermark_selection'))
        for photo in state['photos']:
            if photo['status'] in {'error','excluded'}:continue
            photo.update(candidates={},approval=None,generation=photo.get('generation',1)+1,
                         status='sample_pending' if photo['id'] in state['samples'] else 'analyzed')
        state.update(revision=revision+1,sample_binding=None,status='preparing',choice=None,error=None)
        pending=state.get('pending_request')
        if pending and pending['status'] in {'samples_queued','awaiting_confirmation'}:
            pending.update(status='samples_queued',approved_photo_ids=[])
        return {'accepted':True,'revision':state['revision'],'option_id':option_id,'record':relative,'photos_approved':False}
    result=store.mutate(commit)
    if not result.get('unchanged'):store.event('watermark_draft_selected',result,notify=False)
    return result
