#!/usr/bin/env python3
"""Backfill dominant-color palettes from Photos derivative thumbnails.

New tagging batches capture a palette from the full-resolution export at
classification time; this script covers the photos tagged before that existed
(and the not-yet-tagged backlog) without downloading a single original —
Photos keeps a local derivative JPEG for essentially every item even with
"Optimize Mac Storage" on.

Read-only with respect to the Photos library: derivative files are only ever
opened for reading, and nothing here touches the Photos database or the
scripting interface. Output is one JSON line per photo UUID in
<run_dir>/colors.jsonl, joinable to manifest/results records via
photo_id.split("/")[0]. Resumable: UUIDs already present in colors.jsonl are
skipped, so rerunning only processes what's missing.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from phototagger import image_palette  # noqa: E402

DEFAULT_DERIVATIVES = (
    Path.home()
    / "Pictures"
    / "Photos Library.photoslibrary"
    / "resources"
    / "derivatives"
)


def manifest_uuids(run_dir: Path) -> set[str]:
    uuids: set[str] = set()
    with (run_dir / "manifest.jsonl").open() as handle:
        for line in handle:
            record = json.loads(line)
            photo_id = record.get("photo_id")
            if photo_id:
                uuids.add(str(photo_id).split("/")[0].upper())
    return uuids


def completed_uuids(colors_path: Path) -> set[str]:
    done: set[str] = set()
    if not colors_path.exists():
        return done
    with colors_path.open() as handle:
        for line in handle:
            try:
                done.add(str(json.loads(line)["photo_uuid"]).upper())
            except (json.JSONDecodeError, KeyError):
                continue  # torn tail line from an interrupted run; redo it
    return done


def best_derivatives(root: Path, wanted: set[str]) -> dict[str, Path]:
    """Map each wanted UUID to its preferred derivative file.

    The `_105_c` rendition is the standard mid-size thumbnail and exists for
    nearly everything; any other JPEG with the same UUID prefix is the
    fallback.
    """
    chosen: dict[str, tuple[int, Path]] = {}
    for path in root.rglob("*.jpeg"):
        name = path.name
        if len(name) < 36:
            continue
        uuid = name[:36].upper()
        if uuid not in wanted:
            continue
        rank = 0 if name.endswith("_105_c.jpeg") else 1
        current = chosen.get(uuid)
        if current is None or rank < current[0]:
            chosen[uuid] = (rank, path)
    return {uuid: path for uuid, (_, path) in chosen.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="run directory with manifest.jsonl")
    parser.add_argument(
        "--derivatives",
        type=Path,
        default=DEFAULT_DERIVATIVES,
        help="Photos derivatives directory (default: system photo library)",
    )
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    colors_path = run_dir / "colors.jsonl"
    wanted = manifest_uuids(run_dir)
    done = completed_uuids(colors_path)
    remaining = wanted - done
    print(f"manifest photos: {len(wanted)}  already extracted: {len(done)}")
    if not remaining:
        print("nothing to do")
        return 0

    print(f"scanning derivatives under {args.derivatives} ...")
    derivative_for = best_derivatives(args.derivatives, remaining)
    missing = len(remaining) - len(derivative_for)
    print(f"to extract: {len(derivative_for)}  no local derivative: {missing}")

    write_lock = threading.Lock()
    counts = {"ok": 0, "failed": 0}

    def extract(uuid: str, path: Path) -> None:
        palette = image_palette(path)
        if palette is None:
            with write_lock:
                counts["failed"] += 1
            return
        record = {
            "photo_uuid": uuid,
            "palette": palette,
            "derivative": path.name,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        line = json.dumps(record, ensure_ascii=False)
        with write_lock:
            with colors_path.open("a") as handle:
                handle.write(line + "\n")
            counts["ok"] += 1
            total = counts["ok"] + counts["failed"]
            if total % 500 == 0:
                print(f"  {total}/{len(derivative_for)} processed", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for uuid, path in derivative_for.items():
            pool.submit(extract, uuid, path)

    print(
        f"done: {counts['ok']} palettes written, {counts['failed']} decode failures, "
        f"{missing} without a local derivative (rerun later to retry both)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
