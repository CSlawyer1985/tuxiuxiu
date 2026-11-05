#!/usr/bin/env python3
"""Synthetic forward checks; retains every fixture under an absent caller-selected path."""
import argparse
import copy
import json
from pathlib import Path
import shutil

from batch_contract import audit, binding_digest, read_json, sha256, MODULES, CHECKS, EXPORT_CHECKS


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def fixture(root):
    from PIL import Image, ImageOps, __version__
    root.mkdir(parents=True, exist_ok=False)
    for name in ("input/photos", "input/activity-materials", "scratch/recipes", "scratch/masks",
                 "scratch/previews", "scratch/masters", "scratch/qa", "scratch/runtime", "output"):
        (root / name).mkdir(parents=True)
    source = Image.new("RGB", (24, 32), (90, 80, 70))
    source.putpixel((0, 0), (255, 0, 0))
    source.putpixel((23, 31), (0, 255, 0))
    exif = Image.Exif()
    exif[274] = 6
    source.save(root / "input/photos/asymmetric.png", exif=exif)
    with Image.open(root / "input/photos/asymmetric.png") as im:
        base = ImageOps.exif_transpose(im).convert("RGB")
    assert base.size == (32, 24) and base.getpixel((31, 0)) == (255, 0, 0)
    base.save(root / "scratch/masters/baseline.png")
    master = base.copy()
    master.putpixel((20, 20), (95, 85, 75))
    master.save(root / "scratch/masters/master.png")
    master.save(root / "scratch/previews/full.png")
    base.save(root / "scratch/previews/base.png")
    master.crop((10, 10, 24, 23)).save(root / "scratch/previews/detail.png")
    mask = Image.new("L", base.size, 0)
    for y in range(3):
        for x in range(3):
            mask.putpixel((x, y), 255)
    mask.save(root / "scratch/masks/protect.png")
    (root / "scratch/previews/overlay-review.txt").write_text("Synthetic fixture only. Not a real photo review.\n")
    (root / "input/activity-materials/approval.txt").write_text("Synthetic approval fixture only.\n")
    write_json(root / "scratch/runtime/runtime.json", {"python_runtime": "synthetic test",
               "Pillow": __version__, "actual_checks": ["PNG decode", "Exif 6 direction", "mask PNG L"]})
    recipe = {"schema_version": "retouch-recipe-1", "executor": {"name": "synthetic-fixture", "version": "1"},
              "color": {"input": "synthetic sRGB", "working": "sRGB", "output": "sRGB",
                        "domain": "encoded", "channels": "RGB", "range": "8-bit 0..255"},
              "modules": [{"name": name, "decision": "not_needed", "reason": "synthetic contract fixture"}
                          for name in sorted(MODULES)]}
    write_json(root / "scratch/recipes/recipe.json", recipe)
    files = [("source", "input", "input/photos/asymmetric.png"),
             ("recipe", "recipe", "scratch/recipes/recipe.json"),
             ("baseline", "baseline", "scratch/masters/baseline.png"),
             ("master", "master", "scratch/masters/master.png"),
             ("full", "preview", "scratch/previews/full.png"),
             ("base", "preview", "scratch/previews/base.png"),
             ("detail", "detail", "scratch/previews/detail.png"),
             ("overlay", "review", "scratch/previews/overlay-review.txt"),
             ("runtime", "runtime", "scratch/runtime/runtime.json"),
             ("approval", "user_confirmation", "input/activity-materials/approval.txt"),
             ("lock", "mask", "scratch/masks/protect.png")]
    artifacts = [{"id": aid, "role": role, "path": path, "sha256": sha256(root / path)}
                 for aid, role, path in files]
    refs = {a["id"]: a for a in artifacts}
    refs["lock"].update({"source_id": "source", "source_sha256": refs["source"]["sha256"],
                         "coordinates": "oriented_pixels", "size": [32, 24], "feather_px": 0,
                         "purpose": "pixel_lock", "review": {"status": "reviewed", "evidence_ids": ["overlay"]}})
    qa = {"binding": {key: refs[aid]["sha256"] for key, aid in
                      (("input", "source"), ("recipe", "recipe"), ("baseline", "baseline"), ("master", "master"))},
          "checks": {key: {"result": "pass", "reason": "synthetic fixture check only",
                            "evidence_ids": ["baseline", "master"]} for key in CHECKS}}
    qa["binding"].update({"masks": {"lock": refs["lock"]["sha256"]}, "resources": {}})
    write_json(root / "scratch/qa/qa.json", qa)
    artifacts.append({"id": "qa", "role": "qa", "path": "scratch/qa/qa.json", "sha256": sha256(root / "scratch/qa/qa.json")})
    document = {"schema_version": "retouch-batch-1", "batch_id": "SYNTHETIC-NOT-REAL-APPROVAL",
                "state": "sample_ready", "scope": {"conditions": ["synthetic"],
                "color_contract": "synthetic encoded sRGB", "delivery_contract": "synthetic tests only",
                "runtime_id": "runtime", "shared_asset_ids": ["runtime"]},
                "artifacts": artifacts, "sample_set": ["source"],
                "source_ids": ["source"], "pending_input_ids": [], "excluded_inputs": [],
                "photos": [{"input_id": "source", "recipe_id": "recipe", "mask_ids": ["lock"],
                            "resource_ids": [], "baseline_id": "baseline", "master_id": "master",
                            "preview_id": "full", "base_preview_id": "base", "detail_ids": ["detail"],
                            "qa_id": "qa", "contains_people": True, "conditions": ["synthetic"]}]}
    write_json(root / "scratch/batch.json", document)
    return document


def run_tests(destination):
    from PIL import Image
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    original = destination / "fixture"
    document = fixture(original)
    source_before = sha256(original / "input/photos/asymmetric.png")
    results = []
    def case(name, change, should_pass=False):
        root = destination / name
        shutil.copytree(original, root)
        value = copy.deepcopy(document)
        change(value, root)
        # Each case is immutable once recorded. Mutations below are confined to synthetic scratch.
        write_json(root / "scratch/test-record.json", value)
        try:
            result = audit(root, read_json(root / "scratch/test-record.json"))
            passed, detail = True, result
        except (ValueError, KeyError, TypeError, OSError) as exc:
            passed, detail = False, str(exc)
        correct = passed == should_pass
        results.append({"name": name, "expected_valid": should_pass, "observed_valid": passed,
                        "test_passed": correct, "detail": detail})
    def ref(doc, name):
        return next(a for a in doc["artifacts"] if a["id"] == name)
    def approve(doc, root):
        doc["state"] = "approved"
        doc["approval"] = {"decision": "approved", "sample_binding_sha256": binding_digest(doc, {a["id"]: a for a in doc["artifacts"]}),
                           "scope": copy.deepcopy(doc["scope"]), "user_snapshot_id": "approval"}
    case("valid-sample", lambda d, r: None, True)
    case("valid-synthetic-approval", approve, True)
    case("missing-approval", lambda d, r: d.update(state="approved"))
    case("wrong-source-digest", lambda d, r: ref(d, "source").update(sha256="0" * 64))
    case("wrong-mask-source", lambda d, r: ref(d, "lock").update(source_sha256="0" * 64))
    case("wrong-mask-coordinates", lambda d, r: ref(d, "lock").update(coordinates="thumbnail"))
    case("unreviewed-mask", lambda d, r: ref(d, "lock")["review"].update(status="not_reviewed"))
    case("missing-mask-review", lambda d, r: ref(d, "lock")["review"].update(evidence_ids=[]))
    case("new-lighting", lambda d, r: d["photos"][0].update(conditions=["uncovered-blue-light"]))
    case("missing-detail", lambda d, r: d["photos"][0].update(detail_ids=[]))
    case("missing-base-candidate", lambda d, r: d["photos"][0].update(base_preview_id="absent"))
    case("path-traversal", lambda d, r: ref(d, "master").update(path="../escape.png"))
    case("stale-review-metadata", lambda d, r: (approve(d, r), ref(d, "lock").update(feather_px=2)))
    def scratch_edit(doc, root, aid, transform):
        item = ref(doc, aid)
        value = read_json(root / item["path"])
        transform(value)
        new_path = "scratch/edited-" + aid + ".json"
        write_json(root / new_path, value)
        item.update(path=new_path, sha256=sha256(root / new_path))
    case("failed-qa", lambda d, r: scratch_edit(d, r, "qa", lambda q: q["checks"]["skin"].update(result="fail")))
    case("untested-qa", lambda d, r: scratch_edit(d, r, "qa", lambda q: q["checks"]["texture"].update(result="not_tested")))
    case("unassessed-module", lambda d, r: scratch_edit(d, r, "recipe", lambda q: q["modules"][0].update(decision="unassessed")))
    case("unknown-module", lambda d, r: scratch_edit(d, r, "recipe", lambda q: q["modules"][0].update(name="reshape_face")))
    case("wrong-color-contract", lambda d, r: scratch_edit(d, r, "recipe", lambda q: q["color"].update(domain=None)))
    case("unbound-lut-calibration", lambda d, r: scratch_edit(d, r, "recipe", lambda q: next(m for m in q["modules"] if m["name"] == "lut_style").update(decision="applied", operator="synthetic", parameters=[{"name": "mix", "value": .2, "unit": "ratio"}])))
    def mask_size(doc, root):
        path = "scratch/masks/wrong-size.png"
        Image.new("L", (8, 8), 255).save(root / path)
        ref(doc, "lock").update(path=path, sha256=sha256(root / path))
    case("mask-size", mask_size)
    def core_change(doc, root):
        path = "scratch/masters/changed-core.png"
        with Image.open(root / ref(doc, "master")["path"]) as im:
            im.putpixel((0, 0), (1, 2, 3))
            im.save(root / path)
        ref(doc, "master").update(path=path, sha256=sha256(root / path))
        scratch_edit(doc, root, "qa", lambda q: q["binding"].update(master=ref(doc, "master")["sha256"]))
    case("protected-core-changed", core_change)
    def symlink(doc, root):
        path = root / "scratch/alias.png"
        path.symlink_to("masters/master.png")
        ref(doc, "master").update(path="scratch/alias.png")
    case("symlink", symlink)
    case("partial-delivery", lambda d, r: (approve(d, r), d.update(state="delivery_ready", deliveries=[])))
    def deliver(doc, root):
        shutil.copy2(root / "scratch/masters/master.png", root / "output/synthetic.png")
        doc["artifacts"].append({"id": "output", "role": "output", "path": "output/synthetic.png",
                                 "sha256": sha256(root / "output/synthetic.png")})
        write_json(root / "scratch/qa/export.json", {"schema_version": "retouch-export-review-1",
                   "binding": {"input": ref(doc, "source")["sha256"], "master": ref(doc, "master")["sha256"],
                               "output": ref(doc, "output")["sha256"]},
                   "checks": {key: {"result": "pass", "reason": "synthetic export fixture only"}
                              for key in EXPORT_CHECKS}})
        doc["artifacts"].append({"id": "export", "role": "review", "path": "scratch/qa/export.json",
                                 "sha256": sha256(root / "scratch/qa/export.json")})
        approve(doc, root)
        doc.update(state="delivery_ready", deliveries=[{"input_id": "source", "output_id": "output",
                   "variant": "synthetic-only", "expected_oriented_size": [32, 24], "export_review_id": "export"}])
    case("valid-synthetic-delivery", deliver, True)
    case("export-review-is-input", lambda d, r: (deliver(d, r), d["deliveries"][0].update(export_review_id="source")))
    case("export-review-wrong-binding", lambda d, r: (deliver(d, r), scratch_edit(d, r, "export", lambda q: q["binding"].update(output="0" * 64))))
    def second_source(doc, root):
        shutil.copy2(root / "input/photos/asymmetric.png", root / "input/photos/second.png")
        doc["artifacts"].append({"id": "second", "role": "input", "path": "input/photos/second.png",
                                 "sha256": sha256(root / "input/photos/second.png")})
    case("unlisted-original", lambda d, r: (second_source(d, r), deliver(d, r)))
    case("pending-original", lambda d, r: (second_source(d, r), deliver(d, r),
         d.update(source_ids=["source", "second"], pending_input_ids=["second"])))
    case("duplicate-delivery-variant", lambda d, r: (deliver(d, r), d["deliveries"].append(copy.deepcopy(d["deliveries"][0]))))
    case("pending-sample-is-allowed", lambda d, r: (second_source(d, r),
         d.update(source_ids=["source", "second"], pending_input_ids=["second"])), True)
    case("explained-subset-delivery", lambda d, r: (second_source(d, r), deliver(d, r),
         d.update(source_ids=["source", "second"], excluded_inputs=[{"input_id": "second",
                  "reason": "synthetic duplicate intentionally excluded", "evidence_id": "overlay"}])), True)
    def second_variant(doc, root):
        deliver(doc, root)
        shutil.copy2(root / "output/synthetic.png", root / "output/synthetic-v2.png")
        doc["artifacts"].append({"id": "output2", "role": "output", "path": "output/synthetic-v2.png",
                                 "sha256": sha256(root / "output/synthetic-v2.png")})
        doc["deliveries"].append({"input_id": "source", "output_id": "output2", "variant": "second-authorized-fixture",
                                 "expected_oriented_size": [32, 24], "export_review_id": "export"})
    case("multiple-output-variants", second_variant, True)
    case("draft-not-completion", lambda d, r: d.update(state="draft"), True)
    case("draft-mask-candidate", lambda d, r: (d.update(state="draft"),
         ref(d, "lock").update(review={"status": "not_reviewed"})), True)
    case("empty-record-not-completion", lambda d, r: (d.clear(), d.update(
        schema_version="retouch-batch-1", batch_id=None, state="draft", artifacts=[])))
    # Parser checks prevent silent reinterpretation before artifact auditing.
    for name, raw in (("duplicate-json-key", '{"state":"draft","state":"approved"}'),
                      ("nan-json", '{"value":NaN}'), ("overflow-json", '{"value":1e999}')):
        path = destination / (name + ".json")
        path.write_text(raw)
        try:
            read_json(path)
            okay = False
        except ValueError:
            okay = True
        results.append({"name": name, "test_passed": okay})
    assert source_before == sha256(original / "input/photos/asymmetric.png")
    report = {"all_passed": all(r["test_passed"] for r in results), "tests": results,
              "scope": "synthetic artifact contract and orientation only; no real retouch quality or approval proof",
              "source_fixture_unchanged": True, "fixtures_retained": True}
    write_json(destination / "results.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    report = run_tests(parser.parse_args().destination)
    print(json.dumps({"all_passed": report["all_passed"], "test_count": len(report["tests"])}, indent=2))
    raise SystemExit(0 if report["all_passed"] else 1)
