"""Bridge to the separately installed, checksum-bound local model runtime."""
import json
import os
from pathlib import Path
import select
import subprocess
import threading
import uuid

from PIL import Image
import setup_runtime as bootstrap
from .common import artifact,canonical_hash,inside,load_json,save_json,sha256,verify_artifacts

class SkinRuntime:
    def __init__(self,project):
        self.project=project;self.process=None;self.lock=threading.Lock();self.log=None

    def close(self):
        if self.process:
            self.process.stdin.close()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=3)
            self.process=None
        if self.log:self.log.close();self.log=None

    def receive(self):
        if not select.select([self.process.stdout],[],[],90)[0]:
            self.close();raise ValueError('本地皮肤模型响应超时；请检查运行环境')
        line=self.process.stdout.readline(2*1024*1024)
        if not line:raise ValueError('本地皮肤模型已停止，请运行模型自检')
        return json.loads(line)

    def start(self):
        if self.process and self.process.poll() is None:return
        if self.process:self.close()
        python,_=bootstrap.active_runtime();models=bootstrap.model_paths()
        worker=Path(__file__).with_name('skin_worker.py')
        runtime=self.project/'scratch/retouch/runtime';runtime.mkdir(parents=True,exist_ok=True)
        self.log=(runtime/('skin-worker-'+uuid.uuid4().hex[:8]+'.log')).open('xb')
        self.process=subprocess.Popen([str(python),str(worker),'--models-json',json.dumps({key:str(path) for key,path in models.items()}),'--serve'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,bufsize=1,
            env=dict(os.environ,EVENT_PHOTO_RUNTIME_ROOT=str(bootstrap.runtime_root())))
        if self.receive().get('smoke_passed') is not True:raise ValueError('模型运行自检失败')

    def propose(self,image,photo,regions):
        models=bootstrap.model_paths();python,environment=bootstrap.active_runtime()
        binding={'input':photo['input'],'size':list(image.size),'regions':regions,'models_sha256':{key:sha256(path) for key,path in models.items()},'candidate_sha256':sha256(Path(__file__).with_name('skin_candidates.py')),
                 'worker_sha256':sha256(Path(__file__).with_name('skin_worker.py')),
                 'requirements_sha256':environment['requirements_sha256'],'python':environment['python']}
        key=canonical_hash(binding);relative=f'scratch/retouch/skin-cache/{key}'
        folder=self.project/relative;inside(self.project,relative+'/manifest.json',False)
        manifest=folder/'manifest.json'
        if manifest.is_file():
            value=load_json(manifest)
            if value['binding']!=binding:raise ValueError('缓存选区与本张原片或模型不匹配')
            verify_artifacts(self.project,[value[key] for key in ('skin','texture','features','report')])
        else:
            folder.mkdir(parents=True,exist_ok=True)
            # Each attempt gets a new directory; interrupted evidence is retained.
            task=folder/uuid.uuid4().hex[:12];task.mkdir()
            image.save(task/'canonical.png')
            job_id=uuid.uuid4().hex
            with self.lock:
                self.start();self.process.stdin.write(json.dumps({'directory':str(task),'job_id':job_id,'regions':regions,'original_size':list(image.size)})+'\n');self.process.stdin.flush()
                result=self.receive()
            if not result.get('ok') or result.get('job_id')!=job_id:raise ValueError('模型分割失败：'+result.get('error','任务版本不匹配'))
            value={'binding':binding,**{key:artifact(self.project,task/(key+'.png'),'mask') for key in ('skin','texture','features')},'report':artifact(self.project,task/'segmentation.json','report')}
            save_json(manifest,value)
        masks={}
        for key in ('skin','texture','features'):
            with Image.open(inside(self.project,value[key]['path'])) as im:masks[key]=im.copy()
            if masks[key].mode!='L' or masks[key].size!=image.size:raise ValueError('模型蒙版通道或方向尺寸不匹配')
        return masks,value['report'],load_json(inside(self.project,value['report']['path']))
