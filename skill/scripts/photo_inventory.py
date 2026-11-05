#!/usr/bin/env python3
"""Read-only image inventory; never assumes an unknown color space is sRGB."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def inspect_folder(root):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required; no automatic installation is performed") from exc
    root = Path(root).resolve(strict=True)
    require_directory = root.is_dir()
    if not require_directory:
        raise ValueError("source must be a directory")
    result = []
    # Do not follow directory symlinks or mistake process images for camera originals.
    import os
    def walk_error(error):
        result.append({"path": str(Path(error.filename).relative_to(root)),
                       "state": "unreadable_directory", "error_type": type(error).__name__})
    for base, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        directories.sort()
        for name in list(directories):
            if (Path(base) / name).is_symlink():
                result.append({"path": str((Path(base) / name).relative_to(root)),
                               "state": "symlink_not_followed"})
                directories.remove(name)
        for name in sorted(filenames):
            path = Path(base) / name
            row = {"path": path.relative_to(root).as_posix()}
            if path.is_symlink():
                row["state"] = "symlink_not_followed"
                result.append(row)
                continue
            digest = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            except OSError as exc:
                row.update(state="unreadable_file", error_type=type(exc).__name__)
                result.append(row)
                continue
            row.update({"sha256": digest.hexdigest(), "bytes": path.stat().st_size})
            try:
                with Image.open(path) as im:
                    im.load()
                    exif = im.getexif()
                    orientation = exif.get(274, 1)
                    color = exif.get_ifd(34665).get(40961) if 34665 in exif else None
                    color = color if color is not None else exif.get(40961)
                    icc = im.info.get("icc_profile")
                    row.update({"state": "readable_not_processed", "format": im.format,
                                "mode": im.mode, "stored_size": list(im.size),
                                "orientation": orientation,
                                "oriented_size": list(im.size[::-1] if orientation in (5, 6, 7, 8) else im.size),
                                "icc_sha256": hashlib.sha256(icc).hexdigest() if icc else None,
                                "exif_color_space": color,
                                "color_interpretation": "embedded_icc_requires_validation" if icc else
                                "Exif_declares_sRGB" if color == 1 else "unknown_requires_explicit_assumption",
                                "pixel_handling": "not_selected; inventory support is not processing support"})
            except Exception as exc:
                row.update({"state": "unreadable_or_nonimage", "error_type": type(exc).__name__})
            after = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        after.update(block)
            except OSError as exc:
                row.update(state="source_unavailable_after_inventory", error_type=type(exc).__name__,
                           source_stable_during_inventory=False)
                result.append(row)
                continue
            row["source_stable_during_inventory"] = after.hexdigest() == row["sha256"]
            if not row["source_stable_during_inventory"]:
                row["state"] = "source_changed_during_inventory"
            result.append(row)
    return {"schema_version": "photo-inventory-1", "files": result,
            "readable_images": sum(r["state"] == "readable_not_processed" for r in result),
            "performed_writes": False, "metadata_scope": "orientation and color only; GPS not exported"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_folder(args.source), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
