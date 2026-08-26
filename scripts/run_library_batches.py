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
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXIT_PHOTOS_HUNG = 75
# Photos degrades with time-under-load, not photo count, so the safe batch size
# depends on how slow the current model is. 300 was calibrated when a photo took
# ~7s; at the 26B's ~17s/photo a 300-photo batch runs ~85 minutes and the hangs
# cluster near its end. An explicit size wins; otherwise the run's own recorded
# batch_size wins, so a size chosen once survives every later restart —
# including the unattended ones the launchd health check performs.
DEFAULT_BATCH_SIZE = 300
# Every tagged photo pulls its iCloud original down and Photos keeps it, so a
# long run consumes disk steadily. Below this, exports start returning zero
# files and Photos eventually wedges (measured: 544 failures/day at 12 GB free,
# 0 at 64 GB). Park the run rather than grind into that state.
# Default 20 GB is calibrated for ~30 MB RAW downloads. A small-file run
# (e.g. --only-extensions=heic, ~2 MB each, ~200 MB per 100-photo batch) can
# safely use a lower floor via PHOTOTAGGER_MIN_FREE_GB — the evictable iCloud
# originals sit as purgeable space macOS reclaims under pressure, so df hovering
# near the floor is not real exhaustion. Default unchanged, so RAW/full runs are
# unaffected.
MIN_FREE_GB = float(os.environ.get("PHOTOTAGGER_MIN_FREE_GB", "20"))
# Mirrors phototagger.RETRY_STATUSES — a photo whose latest record has one of
# these is still pending, so it must not count toward a --stop-after target.
RETRY_STATUSES = {"error", "verify-failed", "write-pending"}
MAX_CONSECUTIVE_HANG_RESTARTS = 5
# Sustained iCloud downloading can throttle: exports start returning nothing
# even with plenty of disk and a healthy Photos. It clears on its own, so wait
# between no-progress batches instead of spending all three strikes in minutes.
NO_PROGRESS_BACKOFF_SECONDS = 600


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


def free_gb() -> float:
    stat = os.statvfs("/System/Volumes/Data")
    return stat.f_bavail * stat.f_frsize / 1_073_741_824


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
    usage = (
        "usage: run_library_batches.py RUN_DIRECTORY [BATCH_SIZE] "
        "[--stop-after=N] [--only-extensions=CR2,CR3] [--only-filenames=DJI,GOPR]"
    )
    known_flags = {"--stop-after", "--only-extensions", "--only-filenames"}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {}
    for raw in (a for a in sys.argv[1:] if a.startswith("--")):
        # Require the = form: "--stop-after 300" would silently parse the flag
        # name as its own value and then crash (or filter on nonsense).
        name, sep, value = raw.partition("=")
        if name not in known_flags or not sep or not value:
            print(f"bad flag {raw!r}\n{usage}", file=sys.stderr)
            return 2
        flags[name] = value
    if len(args) not in (1, 2):
        print(usage, file=sys.stderr)
        return 2
    run_dir = Path(args[0]).resolve()
    try:
        stop_after = int(flags["--stop-after"]) if "--stop-after" in flags else None
        if len(args) == 2:
            batch_size = int(args[1])
        else:
            try:
                recorded = json.loads((run_dir / "run.json").read_text()).get("batch_size")
            except (OSError, json.JSONDecodeError):
                recorded = None
            batch_size = int(recorded) if recorded else DEFAULT_BATCH_SIZE
    except ValueError as error:
        print(f"not a number: {error}\n{usage}", file=sys.stderr)
        return 2
    only_ext = flags.get("--only-extensions")
    only_names = flags.get("--only-filenames")
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
    # Photos is a machine-global singleton and this runner force-kills and
    # relaunches it between batches. The per-run lock above cannot stop a
    # second runner on a DIFFERENT run directory from doing the same thing
    # concurrently — each would kill Photos mid-export of the other's batch.
    # One machine, one Photos-lifecycle manager.
    photos_lock_path = ROOT / "runs" / ".photos-lifecycle.lock"
    photos_lock_path.parent.mkdir(parents=True, exist_ok=True)
    photos_lock = photos_lock_path.open("w")
    try:
        fcntl.flock(photos_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(
            "another runner is already managing the Photos lifecycle on this "
            "machine; only one runner may be active at a time",
            file=sys.stderr,
        )
        return 1
    phototagger = ROOT / "phototagger.py"
    hang_restarts = 0
    no_progress_batches = 0
    while True:
        if (run_dir / "STOP").exists():
            log("STOP file found; runner paused")
            return 0
        available = free_gb()
        if available < MIN_FREE_GB:
            # Park rather than starve Photos. Durable by design: this lives in
            # the runner, not in a supervising shell that dies with a session.
            (run_dir / "STOP").touch()
            log(
                f"only {available:.1f} GB free (< {MIN_FREE_GB} GB); "
                "STOP placed to protect Photos. Free space, then restart the runner."
            )
            return 0
        metadata = json.loads(run_file.read_text(encoding="utf-8"))
        if metadata.get("status") == "complete_with_errors":
            # Terminal, but NOT success: every remaining photo exhausted its
            # retry budget. Say so plainly rather than looping on work that
            # can never be picked up, and never call this "complete".
            gave_up = metadata.get("gave_up_count", "some")
            log(
                f"run finished with {gave_up} photo(s) unresolved after "
                "repeated failures; run `coverage` to list them"
            )
            return 0
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
            ]
            + (["--only-extensions", only_ext] if only_ext else [])
            + (["--only-filenames", only_names] if only_names else []),
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
            # A batch where everything fails finishes fast, so retrying
            # immediately just burns the strike count against whatever is
            # temporarily unhappy. Measured: 300 photos failed "empty export"
            # in a row, then every one of them exported fine 3s later once
            # iCloud had a rest. Back off before spending the next strike.
            backoff = NO_PROGRESS_BACKOFF_SECONDS * no_progress_batches
            log(
                f"batch made no progress ({errors} error(s)); "
                f"waiting {backoff // 60} min before retry ({no_progress_batches}/3)"
            )
            time.sleep(backoff)
            continue
        log(f"runner stopped after batch error (exit {result.returncode})")
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
