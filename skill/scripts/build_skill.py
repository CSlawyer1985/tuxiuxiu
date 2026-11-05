#!/usr/bin/env python3
"""Build a portable skill from an explicit source allowlist; never installs it."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import platform
import sys
import zipfile

FILES = (
    "README.md", "SKILL.md", "agents/openai.yaml", "references/修图业务规则.md",
    "references/基础调色与肤色精修规范.md", "references/基础调色与肤色精修_记录模板.json",
    "references/执行契约与兼容性.md", "references/批次绑定模板.json", "references/处理能力与效果边界.md",
    "scripts/batch_contract.py", "scripts/photo_inventory.py", "scripts/test_contract.py",
    "scripts/build_skill.py",
    "references/自动批量管线与网页选样.md",
    "references/参数微调与修改要求.md",
    "references/三版水印与网页选择.md",
    "references/确认后归档与同场续跑.md",
    "scripts/retouch_pipeline.py", "scripts/retouch/__init__.py", "scripts/retouch/common.py",
    "scripts/retouch/store.py", "scripts/retouch/engine.py", "scripts/retouch/runner.py",
    "scripts/retouch/callback.py", "scripts/retouch/service.py", "scripts/retouch/vision.swift",
    "scripts/retouch/tuning.py", "scripts/retouch/tests/test_tuning.py",
    "scripts/retouch/watermarks.py", "scripts/retouch/tests/test_watermarks.py",
    "scripts/retouch/retention.py", "scripts/retouch/tests/test_retention.py",
    "scripts/retouch/tests/test_store.py", "scripts/retouch/tests/test_engine.py",
    "scripts/retouch/tests/test_runner.py", "scripts/retouch/tests/test_callback.py", "scripts/retouch/tests/test_service.py",
    "scripts/retouch/tests/test_ui.py", "scripts/retouch/tests/ui_regressions.cjs",
    "assets/photo-review/index.html", "assets/photo-review/style.css", "assets/photo-review/app.js",
    "一键准备.command", "scripts/setup_runtime.py", "scripts/retouch/skin_runtime.py", "scripts/retouch/skin_worker.py",
    "assets/runtime-requirements.txt", "assets/skin-model.json", "assets/models/selfie_multiclass_256x256_float32_v1.tflite",
    "assets/models/LICENSE-Apache-2.0.txt", "assets/models/NOTICE.txt", "references/分享安装与皮肤模型.md",
    "scripts/retouch/tests/test_setup.py", "scripts/retouch/skin_candidates.py",
    "assets/models/hair_segmenter.tflite", "assets/models/face_landmarker.task",
    "assets/models/face_detection_yunet_2026may.onnx", "assets/models/LICENSE-YuNet.txt",
    "scripts/retouch/tests/test_skin_candidates.py",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(destination,wheelhouse=None):
    source = Path(__file__).resolve().parents[1]
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("build destination already exists; choose a new version")
    # Preflight everything before the first write. References must stay in the allowlist.
    for relative in FILES:
        path = source / relative
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source)
                or any(parent.is_symlink() for parent in path.parents if parent != source)):
            raise ValueError(f"missing or linked source: {relative}")
        content = path.read_text(encoding="utf-8") if path.suffix not in ('.tflite','.onnx','.task') else ''
        private_home_prefix = "/" + "Users" + "/"
        private_cache_prefix = "Library" + "/Caches/"
        if private_home_prefix in content or private_cache_prefix in content:
            raise ValueError(f"personal runtime path found: {relative}")
        if path.suffix == ".md":
            for link in re.findall(r"\]\(([^)]+)\)", content):
                if "://" in link or link.startswith("#"):
                    continue
                target = (path.parent / link.split("#", 1)[0]).resolve()
                if not target.is_relative_to(source) or target.relative_to(source).as_posix() not in FILES:
                    raise ValueError(f"unpackaged link from {relative}: {link}")
    package = destination / "event-photo-retouch"
    package.mkdir(parents=True, exist_ok=False)
    entries = []
    for relative in FILES:
        src, dst = source / relative, package / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        checksum = digest(src)
        if checksum != digest(dst):
            raise ValueError(f"copy digest mismatch: {relative}")
        entries.append({"path": relative, "sha256": checksum, "bytes": src.stat().st_size})
    model=json.loads((source/'assets/skin-model.json').read_text())
    for entry in [model,*model.get('additional_models',[])]:
        model_path=package/'assets/models'/entry['filename']
        if digest(model_path)!=entry['sha256'] or model_path.stat().st_size!=entry['bytes']:raise ValueError('bundled model hash mismatch')
    if wheelhouse:
        wheels=Path(wheelhouse).resolve(strict=True);items=[]
        for wheel in sorted(wheels.glob('*.whl')):
            if wheel.is_symlink():raise ValueError('wheel cannot be a symlink')
            relative='assets/wheelhouse/'+wheel.name;target=package/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(wheel,target);checksum=digest(wheel)
            entries.append({'path':relative,'sha256':checksum,'bytes':target.stat().st_size});items.append({'name':wheel.name,'sha256':checksum})
        if not items:raise ValueError('no offline wheels supplied')
        value={'platform':sys.platform,'machine':platform.machine(),'python':f'{sys.version_info.major}.{sys.version_info.minor}',
               'requirements_sha256':digest(source/'assets/runtime-requirements.txt'),'files':items}
        relative='assets/wheelhouse/manifest.json';target=package/relative;target.write_text(json.dumps(value,indent=2)+'\n')
        entries.append({'path':relative,'sha256':digest(target),'bytes':target.stat().st_size})
    archive = destination / "event-photo-retouch.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as stream:
        for relative in [item['path'] for item in entries]:
            stream.write(package / relative, "event-photo-retouch/" + relative)
    with zipfile.ZipFile(archive) as stream:
        if stream.testzip() is not None or len(stream.namelist()) != len(entries):
            raise ValueError("archive verification failed")
        for item in entries:
            if hashlib.sha256(stream.read("event-photo-retouch/" + item["path"])).hexdigest() != item["sha256"]:
                raise ValueError("archive source mismatch")
    manifest = {"schema_version": "skill-build-1", "skill_name": "event-photo-retouch", "files": entries,
                "archive_sha256": digest(archive), "source_copy_and_archive_match": True,
                "installed": False, "contains_activity_assets": False,"contains_proprietary_vendor_assets":False,
                "contains_official_skin_model":True,"model_sha256":model['sha256'],"models_sha256":{entry["filename"]:entry["sha256"] for entry in [model,*model.get("additional_models",[])]},"offline_platform_wheels":bool(wheelhouse)}
    with (destination / "build-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"package": str(package), "archive": str(archive), "files": len(entries), "installed": False,"includes_model":True,"offline_wheels":bool(wheelhouse)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument('--wheelhouse',type=Path)
    args=parser.parse_args()
    print(json.dumps(build(args.destination,args.wheelhouse), ensure_ascii=False, indent=2))
