#!/usr/bin/env python3
"""One-time migration: convert a positional-cursor library run to manifest traversal.

The original whole-library flow walked Photos by numeric position (media item N),
which silently drifts when the library grows or shrinks mid-run. This migration
builds a fixed photo-id manifest via one full positional sweep and retires the
numeric cursor. All existing results.jsonl records are preserved untouched —
photos already processed stay processed; anything unprocessed (or in a retryable
state) re-enters pending on the next resume, addressed purely by photo id.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_phototagger():
    module_path = Path(__file__).resolve().parents[1] / "phototagger.py"
    spec = importlib.util.spec_from_file_location("phototagger", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: migrate_run_to_manifest.py RUN_DIRECTORY", file=sys.stderr)
        return 2
    pt = load_phototagger()
    run_dir = Path(sys.argv[1]).resolve()
    metadata = pt.load_run(run_dir)
    if metadata.get("source") != "library":
        print("only whole-library runs need migration", file=sys.stderr)
        return 2
    if (run_dir / "manifest.jsonl").exists() and "next_index" not in metadata:
        print("run is already manifest-based; nothing to do")
        return 0

    with pt.run_lock(run_dir):
        order = str(metadata.get("order", "ascending"))
        print(f"Building the {order} manifest via one bulk fetch (a few minutes)")
        manifest_total = pt.build_manifest(run_dir, order)

        # Records already in results.jsonl stay authoritative regardless of
        # whether their photo still exists; the manifest governs only what
        # remains to be processed.
        latest = pt.latest_records_by_photo(pt.read_jsonl(run_dir / "results.jsonl"))
        manifest_ids = {
            str(entry.get("photo_id", "")) for entry in pt.load_manifest(run_dir)
        }
        processed_gone = sum(1 for photo_id in latest if photo_id not in manifest_ids)

        for retired_key in ("next_index", "total_count", "last_batch_start", "last_batch_end"):
            metadata.pop(retired_key, None)
        metadata["manifest_total"] = manifest_total
        metadata["manifest_built_at"] = pt.utc_now()
        metadata["migrated_to_manifest_at"] = pt.utc_now()
        metadata["status"] = "batch_complete"
        metadata["version"] = 5
        pt.save_run(run_dir, metadata)

    print(f"Manifest built: {manifest_total} photos")
    print(f"Existing records preserved: {len(latest)} ({processed_gone} for photos no longer in the library)")
    print(f"Resume normally: ./phototagger.py tag --resume {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
