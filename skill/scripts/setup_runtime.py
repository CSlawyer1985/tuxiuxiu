#!/usr/bin/env python3
"""Dependency-free portable installer and launcher; never installs into system Python."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import venv

SKILL=Path(__file__).resolve().parents[1]
MODEL=json.loads((SKILL/'assets/skin-model.json').read_text())

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024*1024):h.update(chunk)
    return h.hexdigest()

def runtime_root():
    return Path(os.environ.get('EVENT_PHOTO_RUNTIME_ROOT',str(Path.home()/'.cache/event-photo-retouch'))).expanduser().resolve()

def safe_directory(path):
    path=Path(path).absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise ValueError('运行目录不能包含符号链接')
    path.mkdir(parents=True,exist_ok=True)
    return path

def model_entries():
    return [dict(MODEL,id="skin"),*MODEL.get("additional_models",[])]

def model_paths(root=None):
    return {entry["id"]:model_path(root,entry) for entry in model_entries()}

def model_path(root=None,entry=None):
    entry=entry or MODEL
    root=root or runtime_root()
    for path in (SKILL/'assets/models'/entry['filename'],root/'models'/entry['filename']):
        if path.is_file():
            if path.is_symlink() or path.stat().st_size!=entry['bytes'] or digest(path)!=entry['sha256']:
                raise ValueError('模型文件摘要不符，已停用；请保留问题文件并重新准备正版模型')
            return path
    raise ValueError('未准备皮肤模型，请运行 scripts/setup_runtime.py setup')

def active_runtime(root=None):
    root=root or runtime_root();pointer=root/'active.json'
    if not pointer.is_file() or pointer.is_symlink():raise ValueError('尚未准备独立环境，请先运行一键准备')
    value=json.loads(pointer.read_text());name=value.get('environment','')
    if not name or Path(name).name!=name:raise ValueError('运行环境记录无效')
    folder=root/'environments'/name
    if folder.is_symlink():raise ValueError('运行环境不能是符号链接')
    python=folder/'bin/python'
    if not python.is_file():raise ValueError('独立Python环境缺失，请重新运行一键准备')
    if value.get('requirements_sha256')!=digest(SKILL/'assets/runtime-requirements.txt'):
        raise ValueError('运行库版本已更新，请重新运行一键准备')
    return python,value

def smoke(python,model):
    paths=model if isinstance(model,dict) else model_paths()
    result=subprocess.run([str(python),str(SKILL/'scripts/retouch/skin_worker.py'),'--models-json',json.dumps({key:str(path) for key,path in paths.items()}),'--smoke'],capture_output=True,text=True,timeout=90)
    if result.returncode:raise ValueError('模型推理自检失败：'+result.stderr[-1200:])
    value=json.loads(result.stdout)
    if value.get('smoke_passed') is not True:raise ValueError('模型未通过实际推理自检')
    return value

def doctor(root=None):
    root=root or runtime_root()
    try:
        python,record=active_runtime(root);model=model_paths(root);check=smoke(python,model)
        return {'ready':True,'runtime_python':str(python),'model':MODEL['id'],'model_sha256':MODEL['sha256'],
                'models':{key:digest(path) for key,path in model.items()},'quality':'candidate_requires_review','inference_check':check,'installation':record}
    except (ValueError,OSError,subprocess.SubprocessError) as exc:return {'ready':False,'reason':str(exc)}

def download_model(root,offline,entry=None):
    entry=entry or MODEL
    try:return model_path(root,entry)
    except ValueError:
        # Never overwrite a file that failed its hash check.
        if any((p/entry['filename']).exists() for p in (SKILL/'assets/models',root/'models')):raise
    if offline:raise ValueError('离线包缺少官方模型；请使用含模型的分享包')
    directory=safe_directory(root/'models');target=directory/entry['filename'];part=directory/(entry['filename']+'.'+uuid.uuid4().hex+'.partial')
    with urllib.request.urlopen(entry['url'],timeout=60) as response,part.open('xb') as stream:
        total=0
        while chunk:=response.read(1024*1024):
            total+=len(chunk)
            if total>entry['bytes']:raise ValueError('模型下载大小异常，保留下载片段供检查')
            stream.write(chunk)
    if total!=entry['bytes'] or digest(part)!=entry['sha256']:raise ValueError('下载模型摘要不符，未启用')
    part.rename(target);return target

def wheelhouse_for_platform():
    base=SKILL/'assets/wheelhouse';manifest=base/'manifest.json'
    if not manifest.is_file():return None
    value=json.loads(manifest.read_text())
    if value.get('requirements_sha256')!=digest(SKILL/'assets/runtime-requirements.txt'):raise ValueError('离线运行库与锁定版本清单不匹配')
    if value.get('platform')!=sys.platform or value.get('machine')!=platform.machine() or value.get('python')!=f'{sys.version_info.major}.{sys.version_info.minor}':return None
    for item in value['files']:
        name=item['name']
        if Path(name).name!=name:raise ValueError('离线运行库清单路径无效')
        path=base/name
        if path.is_symlink() or not path.is_file() or digest(path)!=item['sha256']:raise ValueError('离线运行库摘要不符：'+name)
    return base

def setup(root,offline=False):
    if sys.platform not in {'darwin','linux'}:raise ValueError('当前分享版支持macOS／Linux；Windows尚未适配')
    if not (3,12)<=sys.version_info[:2]<(3,15):raise ValueError('模型运行环境需要Python3.12—3.14；请先准备受支持的Python')
    root=safe_directory(root)
    with (root/'setup.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=doctor(root)
        if old['ready']:return dict(old,reused=True)
        model={entry["id"]:download_model(root,offline,entry) for entry in model_entries()};wheels=wheelhouse_for_platform()
        if offline and wheels is None:raise ValueError('此离线运行库不匹配当前系统、芯片或Python版本；请使用对应包或联网准备')
        folder=safe_directory(root/'environments'/('py'+str(sys.version_info.minor)+'-'+uuid.uuid4().hex[:10]))
        venv.EnvBuilder(with_pip=True).create(folder);python=folder/'bin/python'
        command=[str(python),'-m','pip','--isolated','install','--disable-pip-version-check','--only-binary=:all:','-r',str(SKILL/'assets/runtime-requirements.txt')]
        if wheels:command+=['--no-index','--find-links',str(wheels)]
        else:command+=['--index-url','https://pypi.org/simple']
        log=root/('setup-'+folder.name+'.log')
        with log.open('x') as stream:
            result=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT)
            if result.returncode:raise ValueError('独立环境安装失败，原环境保留；安装日志：'+str(log))
            subprocess.run([str(python),'-m','pip','check'],check=True,stdout=stream,stderr=subprocess.STDOUT)
        check=smoke(python,model)
        record={'environment':folder.name,'requirements_sha256':digest(SKILL/'assets/runtime-requirements.txt'),
                'model_sha256':MODEL['sha256'],'models':{key:digest(path) for key,path in model.items()},'platform':sys.platform,'machine':platform.machine(),
                'python':platform.python_version(),'offline_wheels':bool(wheels),'installed_at':time.time(),'smoke':check}
        pending=root/('active-'+uuid.uuid4().hex+'.json');pending.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
        pending.replace(root/'active.json')
        return dict(doctor(root),reused=False)

def main():
    parser=argparse.ArgumentParser(description='首次准备模型和运行库，或检查已安装能力')
    parser.add_argument('command',choices=['setup','doctor','run'])
    parser.add_argument('--runtime-root',type=Path);parser.add_argument('--offline',action='store_true');parser.add_argument('--yes',action='store_true')
    parser.add_argument('--human',action='store_true',help='简洁显示准备结果，供双击入口使用')
    args,rest=parser.parse_known_args()
    root=args.runtime_root.expanduser().resolve() if args.runtime_root else runtime_root()
    if args.command=='setup':
        if rest:parser.error('未知安装参数')
        if not args.yes:
            if not sys.stdin.isatty():raise ValueError('请确认安装后使用 --yes；不会自动安装或联网')
            print('将在你的独立缓存目录准备约400MB运行环境；优先使用随包模型与离线库，否则联网下载。照片不上传。')
            if input('输入 y 开始准备：').strip().lower()!='y':return
        value=setup(root,args.offline)
        if args.human:
            print('\n准备完成：官方皮肤模型和独立运行环境已通过实际推理自检。')
            print('下次使用会直接复用，无需重新安装。')
            print('现在可在支持本地Skill的助手中添加本文件夹，然后交给它照片文件夹。')
            print('模型生成的是初选区，修图效果仍需查看样片确认。')
        else:print(json.dumps(value,ensure_ascii=False,indent=2))
    elif args.command=='doctor':print(json.dumps(doctor(root),ensure_ascii=False,indent=2))
    else:
        if rest and rest[0]=='--':rest=rest[1:]
        if not rest:raise ValueError('请在 run -- 后提供修片命令，例如 init、start 或 status')
        python,_=active_runtime(root);model_paths(root)
        env=dict(os.environ,EVENT_PHOTO_RUNTIME_ROOT=str(root))
        raise SystemExit(subprocess.call([str(python),str(SKILL/'scripts/retouch_pipeline.py'),*rest],env=env))

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        if '--human' in sys.argv:print('准备未完成：'+str(exc),file=sys.stderr)
        else:print(json.dumps({'ready':False,'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        sys.exit(1)
