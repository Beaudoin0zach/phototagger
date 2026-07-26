#!/usr/bin/env python3
"""Resume PhotoTagger whole-library batches until stopped or complete.

Photos.app reliably hangs after a few hundred photos of sustained AppleEvent
load, so (with the user's standing authorization) this runner manages the
Photos lifecycle itself: it gracefully restarts Photos between batches to
stay under the hang threshold, and when a batch exits with EXIT_PHOTOS_HUNG
(75) it force-kills the wedged process, relaunches, and resumes. All batch
state is durable in the run directory, so restarts never lose work.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXIT_PHOTOS_HUNG = 75
DEFAULT_BATCH_SIZE = 300  # comfortably under the observed ~350-450 hang threshold
# Mirrors phototagger.RETRY_STATUSES — a photo whose latest record has one of
# these is still pending, so it must not count toward a --stop-after target.
RETRY_STATUSES = {"error", "verify-failed", "write-pending"}
MAX_CONSECUTIVE_HANG_RESTARTS = 5


def log(message: str) -> None:
    print(f"{datetime.now().isoformat()} {message}", flush=True)


def photos_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-x", "Photos"], capture_output=True, text=True, check=False
    )
    return [int(pid) for pid in result.stdout.split()]


def quit_photos(*, force: bool) -> None:
    if not photos_pids():
        return
    if not force:
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Photos" to quit'],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            pass  # wedged; fall through to the force kill
        for _ in range(15):
            if not photos_pids():
                return
            time.sleep(2)
        log("graceful quit did not finish; force-killing Photos")
    subprocess.run(["pkill", "-x", "Photos"], check=False)
    for _ in range(15):
        if not photos_pids():
            return
        time.sleep(2)
    subprocess.run(["pkill", "-9", "-x", "Photos"], check=False)
    time.sleep(3)


def wait_for_photos_ready(timeout_seconds: int = 300) -> bool:
    """Launch Photos (background) and wait until it answers a trivial query."""
    subprocess.run(["open", "-g", "-a", "Photos"], check=False)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(
                ["osascript", str(ROOT / "applescript" / "library_count.applescript")],
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            time.sleep(5)
            continue
        if probe.returncode == 0 and probe.stdout.strip().isdigit():
            log(f"Photos ready ({probe.stdout.strip()} items)")
            return True
        time.sleep(5)
    return False


def restart_photos(*, force: bool) -> bool:
    log(f"restarting Photos (force={force})")
    try:
        quit_photos(force=force)
    except subprocess.TimeoutExpired:
        subprocess.run(["pkill", "-9", "-x", "Photos"], check=False)
        time.sleep(3)
    return wait_for_photos_ready()


def photos_done(run_dir: Path) -> int:
    """Count photos whose LATEST record is a durable, non-retryable status."""
    results = run_dir / "results.jsonl"
    if not results.exists():
        return 0
    latest = {}
    with results.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            photo_id = record.get("photo_id")
            if photo_id:
                latest[photo_id] = record.get("status")
    return sum(1 for status in latest.values() if status not in RETRY_STATUSES)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a.split("=")[0]: a.split("=", 1)[-1] for a in sys.argv[1:] if a.startswith("--")}
    if len(args) not in (1, 2):
        print(
            "usage: run_library_batches.py RUN_DIRECTORY [BATCH_SIZE] [--stop-after=N]",
            file=sys.stderr,
        )
        return 2
    run_dir = Path(args[0]).resolve()
    batch_size = int(args[1]) if len(args) == 2 else DEFAULT_BATCH_SIZE
    stop_after = int(flags["--stop-after"]) if "--stop-after" in flags else None
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
    phototagger = ROOT / "phototagger.py"
    hang_restarts = 0
    no_progress_batches = 0
    while True:
        if (run_dir / "STOP").exists():
            log("STOP file found; runner paused")
            return 0
        metadata = json.loads(run_file.read_text(encoding="utf-8"))
        if metadata.get("status") == "complete":
            log("whole-library run complete")
            return 0
        if stop_after is not None:
            done = photos_done(run_dir)
            if done >= stop_after:
                # Target reached. Park the run so nothing restarts it by
                # accident; delete STOP and relaunch to continue later.
                (run_dir / "STOP").touch()
                log(
                    f"reached target: {done} photos done (>= {stop_after}); "
                    "STOP placed, runner exiting"
                )
                return 0
            log(f"{done}/{stop_after} toward target")
        # Preventive restart before every batch: a fresh Photos process stays
        # comfortably under the sustained-load hang threshold.
        if not restart_photos(force=False):
            log("Photos did not become ready; stopping")
            return 1
        manifest_total = int(metadata.get("manifest_total", 0))
        log(f"starting next batch of {batch_size} ({manifest_total} photos in manifest)")
        result = subprocess.run(
            [
                sys.executable,
                str(phototagger),
                "tag",
                "--resume",
                str(run_dir),
                "--batch-size",
                str(batch_size),
            ],
            check=False,
        )
        metadata = json.loads(run_file.read_text(encoding="utf-8"))
        applied = int(metadata.get("applied_this_invocation", 0))
        errors = int(metadata.get("errors_this_invocation", 0))
        if result.returncode == 0:
            hang_restarts = 0
            no_progress_batches = 0
            continue
        if result.returncode == EXIT_PHOTOS_HUNG:
            hang_restarts += 1
            if hang_restarts > MAX_CONSECUTIVE_HANG_RESTARTS:
                log(
                    f"Photos hung {hang_restarts} batches in a row; "
                    "stopping for human eyes"
                )
                return 1
            log(f"batch reported a Photos hang ({hang_restarts} in a row); force-restarting")
            if not restart_photos(force=True):
                log("Photos did not recover after force-restart; stopping")
                return 1
            continue
        if result.returncode == 1 and applied > 0:
            # The batch had per-item errors but still made real progress.
            # Errors are durable, retryable records — keep going and let the
            # retry machinery (and coverage reporting) handle the stragglers.
            hang_restarts = 0
            no_progress_batches = 0
            log(
                f"batch finished with {errors} per-item error(s) but applied "
                f"{applied} photo(s); continuing"
            )
            continue
        if result.returncode == 1:
            no_progress_batches += 1
            if no_progress_batches >= 3:
                log(
                    f"{no_progress_batches} consecutive batches made no progress; "
                    "stopping for human eyes"
                )
                return 1
            log(
                f"batch made no progress ({errors} error(s)); "
                f"retrying ({no_progress_batches}/3)"
            )
            continue
        log(f"runner stopped after batch error (exit {result.returncode})")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
