#!/usr/bin/env python3
"""Resume one PhotoTagger whole-library batch at a time until stopped or complete."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: run_library_batches.py RUN_DIRECTORY", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1]).resolve()
    run_file = run_dir / "run.json"
    if not run_file.exists():
        print(f"run metadata not found: {run_file}", file=sys.stderr)
        return 2
    lock_handle = (run_dir / "runner.lock").open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another library runner already holds the lock", file=sys.stderr)
        return 1
    phototagger = Path(__file__).resolve().parents[1] / "phototagger.py"
    while True:
        if (run_dir / "STOP").exists():
            print(f"{datetime.now().isoformat()} STOP file found; runner paused", flush=True)
            return 0
        metadata = json.loads(run_file.read_text(encoding="utf-8"))
        if metadata.get("status") == "complete":
            print(f"{datetime.now().isoformat()} whole-library run complete", flush=True)
            return 0
        next_index = int(metadata.get("next_index", 1))
        total_count = int(metadata.get("total_count", 0))
        print(
            f"{datetime.now().isoformat()} starting batch at index {next_index} "
            f"of {total_count}",
            flush=True,
        )
        result = subprocess.run(
            [sys.executable, str(phototagger), "tag", "--resume", str(run_dir)],
            check=False,
        )
        if result.returncode:
            print(
                f"{datetime.now().isoformat()} runner stopped after batch error "
                f"(exit {result.returncode})",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
