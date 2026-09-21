#!/usr/bin/env python3
"""Merge Robotiq grasp results for published GLBs into the gallery manifest."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE_ROOT = Path(
    "/home/capture13/shared_data/capture/eccv2026/robotiq_2f85"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-root", type=Path, default=DEFAULT_CAPTURE_ROOT)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=REPO_ROOT / "static/gallery-glb-catalog.json",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "static/gallery-grasp-results/manifest.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    catalog = json.loads(args.catalog.read_text())["robotiq_2f85"]
    manifest = json.loads(args.manifest.read_text())

    results = {
        key: value
        for key, value in manifest.get("results", {}).items()
        if not key.startswith("robotiq_2f85/")
    }
    missing = []
    added = 0

    for object_id, episodes in sorted(catalog.items()):
        for episode in episodes:
            source = args.capture_root / object_id / str(episode) / "grasp_result.json"
            key = f"robotiq_2f85/{object_id}/{episode}"
            if not source.is_file():
                missing.append(key)
                continue
            raw = json.loads(source.read_text())
            grasp_success = raw.get("grasp_success")
            if not isinstance(grasp_success, bool):
                raise ValueError(f"{source}: grasp_success must be Boolean")
            results[key] = {
                "graspSuccess": grasp_success,
                "humanPairedEpisode": raw.get("human_paired_episode"),
            }
            added += 1

    if missing:
        raise FileNotFoundError(
            "Missing grasp results for published GLBs:\n" + "\n".join(missing)
        )

    manifest["generatedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest["results"] = results
    manifest["resultCount"] = len(results)
    manifest["totalTargets"] = len(results) + len(manifest.get("missing", []))
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Added {added} Robotiq results; manifest now has {len(results)} results")


if __name__ == "__main__":
    main()
