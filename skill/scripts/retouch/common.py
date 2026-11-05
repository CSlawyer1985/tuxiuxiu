import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def canonical_hash(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def inside(root, relative, must_exist=True):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('无效的活动相对路径')
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or p.as_posix() != relative:
        raise ValueError('路径不能越出活动目录')
    out = root
    for part in p.parts:
        out = out / part
        if out.is_symlink(): raise ValueError('不读取或覆盖符号链接')
    if not out.resolve().is_relative_to(root): raise ValueError('路径越界')
    if must_exist and not out.is_file(): raise ValueError('活动文件不存在：' + relative)
    return out


def write_new(path, data):
    path = Path(path)
    for parent in path.parents:
        if parent.is_symlink():raise ValueError('写入前发现父目录符号链接，已停止')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink(): raise ValueError('不能覆盖符号链接')
    if path.exists():
        if path.read_bytes() == data: return
        raise ValueError('版本文件已存在，请使用新版本')
    with path.open('xb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())


def save_json(path, value):
    write_new(path, encoded(value) + b'\n')


def decode_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise ValueError('JSON 存在重复字段')
            out[key] = value
        return out
    def invalid(value): raise ValueError('JSON 包含非有限数值')
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    encoded(value)  # Reject overflowed exponents as well as explicit NaN.
    return value


def load_json(path):
    return decode_json(Path(path).read_text())


def executor_signature():
    """Bind approvals to the actual local implementation and color libraries."""
    import importlib.metadata
    import platform
    base=Path(__file__).resolve().parents[2]
    paths=['scripts/retouch/'+name for name in ('common.py','engine.py','runner.py','store.py','service.py','callback.py','tuning.py','watermarks.py','retention.py','vision.swift')]
    paths+=['scripts/retouch_pipeline.py','assets/photo-review/app.js','assets/photo-review/index.html','assets/photo-review/style.css']
    paths+=['scripts/setup_runtime.py','scripts/retouch/skin_runtime.py','scripts/retouch/skin_worker.py','scripts/retouch/skin_candidates.py','assets/skin-model.json','assets/runtime-requirements.txt']
    libraries={}
    for package in ('Pillow','numpy','opencv-python','opencv-python-headless','opencv-contrib-python'):
        try:libraries[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:libraries[package]=None
    return canonical_hash({'files':{p:sha256(base/p) for p in paths},'libraries':libraries,
                           'python':platform.python_version(),'system':platform.platform()})


def renderer_signature():
    """Color/analysis implementation, independent of UI and housekeeping changes."""
    import ast
    import importlib.metadata
    import platform
    root=Path(__file__).parent
    source=(root/'runner.py').read_text();tree=ast.parse(source)
    names={'_candidate','_prepare_photo','_watermark','_preview','_input','covered_by'}
    functions={node.name:ast.get_source_segment(source,node) for node in ast.walk(tree)
               if isinstance(node,ast.FunctionDef) and node.name in names}
    libraries={}
    for name in ('Pillow','numpy','opencv-python','opencv-python-headless','opencv-contrib-python'):
        try:libraries[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:libraries[name]=None
    return canonical_hash({'files':{name:sha256(root/name) for name in ('engine.py','tuning.py','vision.swift','skin_runtime.py','skin_worker.py','skin_candidates.py')},
                           'skin_resources':{name:sha256(root.parents[1]/name) for name in ('assets/skin-model.json','assets/runtime-requirements.txt','scripts/setup_runtime.py')},
                           'functions':functions,'libraries':libraries,'python':platform.python_version(),'platform':platform.platform()})


LOADED_EXECUTOR=executor_signature()


def check_executor(expected=None):
    if executor_signature()!=LOADED_EXECUTOR or expected and expected!=LOADED_EXECUTOR:
        raise ValueError('处理程序已更新，请重启本活动服务并重新核对样片')
    return LOADED_EXECUTOR


def artifact(root, path, role):
    path = Path(path)
    relative = path.relative_to(Path(root)).as_posix()
    inside(root, relative)
    return {'id': canonical_hash([relative, sha256(path)])[:24], 'path': relative,
            'sha256': sha256(path), 'role': role}


def verify_artifacts(root, artifacts):
    for item in artifacts:
        if sha256(inside(root, item['path'])) != item['sha256']:
            raise ValueError('文件在预览后发生变化，请重新分析：' + item['path'])
