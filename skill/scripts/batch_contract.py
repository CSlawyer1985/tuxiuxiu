#!/usr/bin/env python3
"""Read-only batch artifact audit. Never renders, approves, copies or deletes photos."""
import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sys

SCHEMA = "retouch-batch-1"
MODULES = {"pre_denoise", "white_balance", "base_tone", "local_mixed_light",
           "hsl_scene_color", "skin_color", "skin_luminance", "skin_texture",
           "lut_style", "output_sharpen", "watermark_export"}
CHECKS = {"source_integrity", "geometry", "color_units", "clipping",
          "protected_content", "skin", "texture", "watermark"}
EXPORT_CHECKS = {"format", "profile", "bit_depth", "quality", "subsampling",
                 "orientation", "openability", "watermark", "visual"}
ROLES = {"input", "recipe", "mask", "resource", "baseline", "master", "preview",
         "detail", "qa", "user_confirmation", "watermark", "output", "runtime", "review"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    def bad_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")
    def finite_float(value):
        number = float(value)
        require(math.isfinite(number), "non-finite JSON number")
        return number
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=unique_pairs, parse_constant=bad_constant,
                      parse_float=finite_float)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inside(root, value):
    require(nonempty(value), "missing relative path")
    part = PurePosixPath(value)
    require(not part.is_absolute() and ".." not in part.parts
            and part.as_posix() == value and "\\" not in value,
            f"noncanonical or escaping path: {value}")
    current = root
    for name in part.parts:
        current = current / name
        require(not current.is_symlink(), f"symlink not allowed: {value}")
    require(current.is_file() and current.resolve().is_relative_to(root),
            f"missing or escaping file: {value}")
    return current


def image_info(path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required to audit image geometry and masks") from exc
    with Image.open(path) as im:
        im.load()
        size = im.size
        orientation = im.getexif().get(274, 1)
        require(orientation in range(1, 9), f"invalid Exif orientation: {path.name}")
        if orientation in (5, 6, 7, 8):
            size = size[::-1]
        return {"size": list(size), "mode": im.mode, "format": im.format,
                "orientation": orientation}


def check_recipe(recipe, photo, refs):
    require(recipe.get("schema_version") == "retouch-recipe-1", "unsupported recipe")
    executor = recipe.get("executor", {})
    require(nonempty(executor.get("name")) and nonempty(executor.get("version")),
            "recipe executor identity missing")
    color = recipe.get("color", {})
    for key in ("input", "working", "output", "domain", "channels", "range"):
        require(nonempty(color.get(key)), f"recipe color.{key} missing")
    modules = recipe.get("modules", [])
    names = [m.get("name") for m in modules]
    require(set(names) == MODULES and len(names) == len(MODULES),
            "each allowed module must be assessed exactly once")
    for module in modules:
        name = module["name"]
        decision = module.get("decision")
        require(decision in {"applied", "not_needed", "unavailable"},
                f"unassessed or invalid module: {name}")
        require(nonempty(module.get("reason")), f"module reason missing: {name}")
        if decision != "applied":
            continue
        require(nonempty(module.get("operator")), f"operator missing: {name}")
        params = module.get("parameters", [])
        require(params, f"parameter units missing: {name}")
        for param in params:
            require(nonempty(param.get("name")) and nonempty(param.get("unit"))
                    and "value" in param, f"parameter incomplete: {name}")
        mask_ids = module.get("mask_ids", [])
        resource_ids = module.get("resource_ids", [])
        require(set(mask_ids) <= set(photo["mask_ids"]), "unbound recipe mask")
        require(set(resource_ids) <= set(photo["resource_ids"]), "unbound recipe resource")
        if name.startswith("skin_") or name == "local_mixed_light":
            require(mask_ids, f"local operation needs reviewed masks: {name}")
        if name.startswith("skin_"):
            require(any(refs[mid].get("purpose") == "skin" for mid in mask_ids),
                    "skin operation has no actual skin mask")
        if name == "lut_style":
            require(resource_ids and nonempty(module.get("calibration_evidence_id")),
                    "LUT resource or calibration evidence missing")
            require(module["calibration_evidence_id"] in photo["resource_ids"],
                    "LUT calibration evidence is not bound as a resource")


def binding_payload(document, refs):
    selected = set(document["sample_set"])
    photos = [p for p in document["photos"] if p["input_id"] in selected]
    require({p["input_id"] for p in photos} == selected and selected,
            "sample set missing or unknown")
    ids = set(document["scope"]["shared_asset_ids"])
    for photo in photos:
        ids.update(photo[key] for key in ("input_id", "recipe_id", "baseline_id", "master_id",
                                         "preview_id", "base_preview_id", "qa_id"))
        ids.update(photo["mask_ids"] + photo["resource_ids"] + photo["detail_ids"])
    require(ids <= refs.keys(), "unknown sample or shared artifact")
    # Include all artifact metadata, not only the file digest: a mask review or source
    # mapping change must invalidate the previous binding too.
    closure = set(ids)
    for aid in list(ids):
        closure.update(refs[aid].get("review", {}).get("evidence_ids", []))
    require(closure <= refs.keys(), "unknown review artifact")
    return {"schema_version": SCHEMA, "batch_id": document["batch_id"],
            "scope": document["scope"], "samples": sorted(photos, key=lambda p: p["input_id"]),
            "artifacts": [refs[aid] for aid in sorted(closure)]}


def binding_digest(document, refs):
    payload = json.dumps(binding_payload(document, refs), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def audit(root, document):
    root = Path(root).resolve(strict=True)
    require(document.get("schema_version") == SCHEMA, "unsupported batch schema")
    require(nonempty(document.get("batch_id")), "batch id missing")
    stage = document.get("state")
    require(stage in {"draft", "sample_ready", "approved", "delivery_ready", "delivered"},
            "unknown batch state")
    artifacts = document.get("artifacts", [])
    refs = {a["id"]: a for a in artifacts}
    require(len(refs) == len(artifacts), "duplicate artifact id")
    require(len({a["path"] for a in artifacts}) == len(artifacts), "duplicate artifact path")
    paths, images = {}, {}
    for aid, item in refs.items():
        require(nonempty(aid) and item.get("role") in ROLES, "invalid artifact id or role")
        path = inside(root, item["path"])
        parts = PurePosixPath(item["path"]).parts
        role = item["role"]
        if role == "input":
            require(parts[:2] == ("input", "photos"), "original must be input/photos")
        elif role == "user_confirmation":
            require(parts[:2] == ("input", "activity-materials"), "user snapshot location invalid")
        elif role == "output":
            require(parts[0] == "output", "final output location invalid")
        elif role == "watermark":
            require(parts[:2] in (("scratch", "watermark"), ("input", "activity-materials")),
                    "watermark location invalid")
        else:
            require(parts[0] == "scratch", "process artifact must be in scratch")
        require(sha256(path) == item.get("sha256"), f"digest mismatch: {aid}")
        paths[aid] = path
        if role in {"input", "mask", "baseline", "master", "preview", "detail", "output"}:
            images[aid] = image_info(path)
    for aid, item in refs.items():
        if item["role"] != "mask":
            continue
        source = refs.get(item.get("source_id"), {})
        require(source.get("role") == "input", f"mask source missing: {aid}")
        require(item.get("source_sha256") == source["sha256"], "mask belongs to another source")
        require(item.get("coordinates") == "oriented_pixels", "mask coordinate domain unknown")
        require(images[aid]["format"] == "PNG" and images[aid]["mode"] == "L"
                and images[aid]["orientation"] == 1, "mask must be 8-bit grayscale PNG")
        require(images[aid]["size"] == images[item["source_id"]]["size"]
                == item.get("size"), "mask size or orientation mapping mismatch")
        feather = item.get("feather_px")
        require(type(feather) in (int, float) and feather >= 0, "invalid feather radius")
        require(item.get("purpose") in {"skin", "feature_exclusion", "pixel_lock", "local", "subject"},
                "unknown mask purpose")
        review = item.get("review", {})
        if stage == "draft":
            continue
        require(review.get("status") == "reviewed" and review.get("evidence_ids"),
                "mask review missing")
        require(all(e in refs and refs[e]["role"] in {"review", "preview", "detail"}
                    for e in review["evidence_ids"]), "mask review evidence missing")
    if stage == "draft":
        return {"state": "draft", "valid": True, "ready_for_processing_claim": False,
                "artifact_count": len(refs), "note": "draft integrity only; no sample acceptance"}
    photos = document.get("photos", [])
    require(photos, "no processed photos")
    targets = document.get("source_ids", [])
    require(len(targets) == len(set(targets)) and set(targets) ==
            {aid for aid, item in refs.items() if item["role"] == "input"},
            "declared batch source inventory is incomplete")
    pending = document.get("pending_input_ids", [])
    excluded_rows = document.get("excluded_inputs", [])
    excluded = [row["input_id"] for row in excluded_rows]
    processed = [photo["input_id"] for photo in photos]
    partition = processed + pending + excluded
    require(len(partition) == len(set(partition)) and set(partition) == set(targets),
            "every batch source needs exactly one processed/pending/excluded status")
    for row in excluded_rows:
        require(nonempty(row.get("reason")) and row.get("evidence_id") in refs
                and refs[row["evidence_id"]]["role"] in {"review", "user_confirmation"},
                "excluded input has no explanation/evidence")
    scope = document.get("scope", {})
    require(scope.get("conditions") and nonempty(scope.get("color_contract"))
            and nonempty(scope.get("delivery_contract")), "scope incomplete")
    require(scope.get("runtime_id") in refs and refs[scope["runtime_id"]]["role"] == "runtime"
            and scope["runtime_id"] in scope.get("shared_asset_ids", []), "runtime not bound")
    seen = set()
    def reference(photo, key, role):
        aid = photo.get(key)
        require(aid in refs and refs[aid]["role"] == role, f"missing {key}")
        return aid
    for photo in photos:
        iid = reference(photo, "input_id", "input")
        require(iid not in seen, "duplicate photo mapping")
        seen.add(iid)
        rid = reference(photo, "recipe_id", "recipe")
        bid = reference(photo, "baseline_id", "baseline")
        mid = reference(photo, "master_id", "master")
        reference(photo, "preview_id", "preview")
        reference(photo, "base_preview_id", "preview")
        qid = reference(photo, "qa_id", "qa")
        require(type(photo.get("contains_people")) is bool, "people assessment missing")
        require(photo.get("conditions") and set(photo["conditions"]) <= set(scope["conditions"]),
                "new conditions need subgroup sampling")
        require(images[iid]["size"] == images[bid]["size"] == images[mid]["size"],
                "working master geometry mismatch")
        require(images[bid]["format"] in {"PNG", "TIFF"}
                and images[mid]["format"] in {"PNG", "TIFF"}, "working master must be lossless")
        require(images[bid]["orientation"] == images[mid]["orientation"] == 1,
                "baseline/master orientation must be normalized")
        require(not photo["contains_people"] or photo["detail_ids"], "people detail preview missing")
        for key, role in (("mask_ids", "mask"), ("resource_ids", "resource"), ("detail_ids", "detail")):
            for aid in photo[key]:
                require(aid in refs and refs[aid]["role"] == role, "wrong artifact role")
                if role == "mask":
                    require(refs[aid]["source_id"] == iid, "photo uses wrong mask")
        recipe = read_json(paths[rid])
        check_recipe(recipe, photo, refs)
        qa = read_json(paths[qid])
        expected = {"input": refs[iid]["sha256"], "recipe": refs[rid]["sha256"],
                    "baseline": refs[bid]["sha256"], "master": refs[mid]["sha256"],
                    "masks": {aid: refs[aid]["sha256"] for aid in sorted(photo["mask_ids"])},
                    "resources": {aid: refs[aid]["sha256"] for aid in sorted(photo["resource_ids"])}}
        require(qa.get("binding") == expected, "QA refers to a different processing version")
        require(set(qa.get("checks", {})) == CHECKS, "QA checks incomplete")
        local_evidence = {iid, rid, bid, mid, photo["preview_id"], photo["base_preview_id"], qid}
        local_evidence.update(photo["mask_ids"] + photo["resource_ids"] + photo["detail_ids"])
        local_evidence.update(scope["shared_asset_ids"])
        for aid in photo["mask_ids"]:
            local_evidence.update(refs[aid]["review"]["evidence_ids"])
        for key, check in qa["checks"].items():
            require(check.get("result") in {"pass", "not_applicable"}
                    and nonempty(check.get("reason")), f"QA failed, untested or unexplained: {key}")
            require(check.get("result") != "pass" or check.get("evidence_ids"),
                    f"QA pass has no evidence: {key}")
            require(set(check.get("evidence_ids", [])) <= local_evidence,
                    "QA evidence is missing or outside the sample binding")
        # Core protection is checked before final JPEG encoding and watermark composition.
        locks = [aid for aid in photo["mask_ids"] if refs[aid]["purpose"] == "pixel_lock"]
        if locks:
            from PIL import Image, ImageChops
            with Image.open(paths[bid]) as base, Image.open(paths[mid]) as master:
                require(base.mode == master.mode == "RGB", "pixel lock audit supports RGB masters")
                diff = ImageChops.difference(base, master)
                for aid in locks:
                    with Image.open(paths[aid]) as mask:
                        core = mask.point(lambda value: 255 if value == 255 else 0)
                        require(core.getbbox() is not None, "pixel lock mask has no full-weight core")
                        changed = ImageChops.multiply(diff, Image.merge("RGB", (core, core, core)))
                        require(changed.getbbox() is None, "protected core pixels changed")
    digest = binding_digest(document, refs)
    if stage in {"approved", "delivery_ready", "delivered"}:
        approval = document.get("approval", {})
        require(approval.get("decision") == "approved"
                and approval.get("sample_binding_sha256") == digest, "approval missing or stale")
        require(approval.get("scope") == scope, "approval scope mismatch")
        aid = approval.get("user_snapshot_id")
        require(aid in refs and refs[aid]["role"] == "user_confirmation", "approval snapshot missing")
    if stage in {"delivery_ready", "delivered"}:
        require(not pending, "pending batch sources cannot be reported delivered")
        deliveries = document.get("deliveries", [])
        require(deliveries and {row["input_id"] for row in deliveries} == seen,
                "input/output delivery mapping incomplete")
        seen_outputs = set()
        seen_variants = set()
        photo_map = {p["input_id"]: p for p in photos}
        for row in deliveries:
            oid = row.get("output_id")
            require(oid in refs and refs[oid]["role"] == "output" and oid not in seen_outputs,
                    "delivery output missing or duplicated")
            seen_outputs.add(oid)
            require(nonempty(row.get("variant")) and (row["input_id"], row["variant"]) not in seen_variants,
                    "delivery variant missing or duplicated")
            seen_variants.add((row["input_id"], row["variant"]))
            require(images[oid]["size"] == row.get("expected_oriented_size"), "export size mismatch")
            require(images[oid]["orientation"] == 1, "export orientation not normalized")
            eid = row.get("export_review_id")
            require(eid in refs and refs[eid]["role"] == "review",
                    "export review missing")
            review = read_json(paths[eid])
            source_id = row["input_id"]
            master_id = photo_map[source_id]["master_id"]
            require(review.get("schema_version") == "retouch-export-review-1"
                    and review.get("binding") == {"input": refs[source_id]["sha256"],
                    "master": refs[master_id]["sha256"], "output": refs[oid]["sha256"]},
                    "export review is for a different input/master/output")
            require(set(review.get("checks", {})) == EXPORT_CHECKS, "export checks incomplete")
            for key, check in review["checks"].items():
                require(check.get("result") in {"pass", "not_applicable"}
                        and nonempty(check.get("reason")), f"export check untested or failed: {key}")
    return {"valid": True, "state": stage, "artifact_count": len(refs),
            "sample_binding_sha256": digest, "photo_count": len(photos),
            "note": "artifact checks only; approval meaning and visual quality need human/agent review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--record", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.project, read_json(args.record))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
