#!/usr/bin/env python3
"""Resident local CPU segmentation worker. Proposes masks; never approves a photo."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model');parser.add_argument('--models-json');parser.add_argument('--smoke',action='store_true');parser.add_argument('--serve',action='store_true');args=parser.parse_args()
    os.environ.setdefault('MPLCONFIGDIR',str(Path(os.environ.get('EVENT_PHOTO_RUNTIME_ROOT',str(Path.home()/'.cache/event-photo-retouch')))/'matplotlib'))
    import numpy as np
    import cv2
    from PIL import Image,ImageDraw
    import mediapipe as mp
    lock=Path(__file__).resolve().parents[2]/'assets/runtime-requirements.txt'
    versions={}
    for line in lock.read_text().splitlines():
        if line.strip() and not line.startswith('#'):
            name,version=line.strip().split('==');actual=importlib.metadata.version(name)
            if actual!=version:raise ValueError('运行库版本不符：'+name)
            versions[name]=actual
    from skin_candidates import CandidateModels
    paths=json.loads(args.models_json) if args.models_json else {'skin':args.model}
    if set(paths)!={'skin','hair','landmarks','yunet'}:raise ValueError('需要完整四模型清单，请重新准备分享版')
    models=CandidateModels(paths)
    try:
        ready=dict(models.smoke(),ready=True,versions=versions,mediapipe_delegate='CPU')
        print(json.dumps(ready),flush=True)
        if args.smoke:return
        for line in sys.stdin:
            try:
                job=json.loads(line);folder=Path(job['directory'])
                if not folder.is_dir():raise ValueError('本地分割任务目录不存在')
                image=Image.open(folder/'canonical.png').convert('RGB');w,h=image.size
                if list(image.size)!=job['original_size'] or max(w,h)>50000 or w*h>200000000:raise ValueError('原图方向尺寸无效')
                skin,texture,features,report=models.propose(image,job.get('regions',{}))
                skin.save(folder/'skin.png');texture.save(folder/'texture.png');features.save(folder/'features.png')
                report['runtime_versions']=versions
                (folder/'segmentation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps({'ok':True,'job_id':job['job_id']}),flush=True)
            except Exception as exc:print(json.dumps({'ok':False,'error':str(exc)[:800]}),flush=True)
    finally:models.close()

if __name__=='__main__':main()
