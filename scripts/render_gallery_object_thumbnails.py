#!/usr/bin/env python3
"""Render object thumbnails for every object in the HRDexDB gallery catalog."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


CATALOG_URL = "https://huggingface.co/spaces/HRDexDB/HRDexDB-Visualizer/raw/main/assets/glb_catalog.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog-url", default=CATALOG_URL)
    parser.add_argument("--mesh-root", default="/home/capture15/shared_data/mesh_blender")
    parser.add_argument("--output-root", default="static/gallery-object-thumbs")
    parser.add_argument("--renderer", default="scripts/render_object_thumbnail.py")
    parser.add_argument("--blender", default="blender")
    parser.add_argument("--resolution", type=int, default=768)
    parser.add_argument("--up-axis", default="+x", choices=("+x", "-x", "+y", "-y", "+z", "-z"))
    parser.add_argument("--limit", type=int, default=0, help="Render only the first N objects.")
    parser.add_argument("--skip-existing", action="store_true")
    return parser.parse_args()


def load_catalog_objects(catalog_url: str) -> list[str]:
    with urllib.request.urlopen(catalog_url, timeout=60) as response:
        catalog = json.load(response)
    return sorted({object_id for objects_by_id in catalog.values() for object_id in objects_by_id})


def mesh_path(mesh_root: Path, object_id: str) -> Path | None:
    object_dir = mesh_root / object_id
    viser = object_dir / f"{object_id}_viser.obj"
    if viser.exists():
        return viser
    mesh = object_dir / f"{object_id}.obj"
    if mesh.exists():
        return mesh
    return None


def render_one(args: argparse.Namespace, object_id: str, output_path: Path) -> dict:
    cmd = [
        args.blender,
        "--background",
        "--python",
        args.renderer,
        "--",
        "--object",
        object_id,
        "--mesh-root",
        args.mesh_root,
        "--output",
        str(output_path),
        "--resolution",
        str(args.resolution),
        f"--up-axis={args.up_axis}",
        "--source-x-up",
    ]
    started = time.time()
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {
        "object": object_id,
        "output": str(output_path),
        "returncode": result.returncode,
        "seconds": round(time.time() - started, 3),
        "log_tail": result.stdout.splitlines()[-12:],
    }


def main() -> int:
    args = parse_args()
    mesh_root = Path(args.mesh_root)
    output_root = Path(args.output_root)
    renderer = Path(args.renderer)
    if not renderer.exists():
        raise FileNotFoundError(f"Renderer not found: {renderer}")

    objects = load_catalog_objects(args.catalog_url)
    if args.limit:
        objects = objects[: args.limit]

    output_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "catalog_url": args.catalog_url,
        "mesh_root": str(mesh_root),
        "output_root": str(output_root),
        "resolution": args.resolution,
        "up_axis": args.up_axis,
        "total_objects": len(objects),
        "rendered": [],
        "skipped_existing": [],
        "missing_mesh": [],
        "failed": [],
    }

    for index, object_id in enumerate(objects, start=1):
        output_path = output_root / object_id / "thumb.png"
        mesh = mesh_path(mesh_root, object_id)
        if mesh is None:
            print(f"[{index}/{len(objects)}] missing mesh: {object_id}", flush=True)
            manifest["missing_mesh"].append(object_id)
            continue
        if args.skip_existing and output_path.exists():
            print(f"[{index}/{len(objects)}] skip existing: {object_id}", flush=True)
            manifest["skipped_existing"].append(object_id)
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{index}/{len(objects)}] render {object_id} from {mesh.name}", flush=True)
        result = render_one(args, object_id, output_path)
        if result["returncode"] == 0 and output_path.exists():
            manifest["rendered"].append(result)
        else:
            manifest["failed"].append(result)
            print(f"  failed: {object_id} returncode={result['returncode']}", flush=True)

        manifest_path = output_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        "done: "
        f"rendered={len(manifest['rendered'])} "
        f"skipped={len(manifest['skipped_existing'])} "
        f"missing={len(manifest['missing_mesh'])} "
        f"failed={len(manifest['failed'])} "
        f"manifest={manifest_path}",
        flush=True,
    )
    return 1 if manifest["failed"] or manifest["missing_mesh"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
