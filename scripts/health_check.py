#!/usr/bin/env python3
"""Restart a parked-by-accident library runner; leave deliberate stops alone.

Three times this project has lost hours-to-weeks of tagging because the
guarded runner exited and nothing noticed: a mid-batch death (2026-08-03,
unnoticed for two weeks), an interrupted decision on the iMac (~6 days), and
a Photos hang that tripped the circuit breaker (2026-08-20, 5.5 hours). The
runner survives session end, but nobody was watching *it*.

Run this from launchd every few minutes. It restarts the runner only when the
stop looks accidental, and it never fights a stop that was meant:

  * runner or tagger alive  -> work is happening, unless it has stalled
  * STOP file present       -> deliberate pause (human or the disk guard)
  * run.json says complete  -> finished
  * NEEDS_ATTENTION present -> already gave up; a human must clear it

When it does restart, it escalates the wait between attempts (15m, 1h, 3h)
and gives up after that, because the failure this most often meets is a
wedged Photos, which recovers with idle time rather than with retries.
Progress is judged by results.jsonl growing since the last restart — cheap,
and true regardless of which statuses the batch produced.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# launchd hands us a minimal PATH; the runner needs python3/osascript/ollama.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Attempt N waits BACKOFF_MINUTES[N] after the previous restart; past the end
# of the list the runner is left alone for human eyes.
BACKOFF_MINUTES = [15, 60, 180]

# A live-but-wedged tagger is indistinguishable from a working one by process
# liveness alone, and that blind spot cost ~12 hours of tagging on 2026-08-24:
# the tagger stayed up, tagged nothing, and this script dutifully called it
# busy. Progress is the real signal.
#
# The bar must clear the longest silence the tool itself permits, which is a
# single export, not the runner's backoff: exports run with timeout=1800 and
# up to three retries (phototagger.run_applescript), so ~121 minutes of quiet
# is legal while one iCloud original crawls down. An earlier value of 45 was
# measured against the runner's 10/20-minute backoff alone and would kill a
# slow-but-healthy export. 150 clears the real ceiling with margin; the
# runner's own circuit breaker catches wedged Photos long before this does,
# so this only needs to be the backstop for a wedged *runner*.
STALL_MINUTES = 150

# The runner parks itself below MIN_FREE_GB to protect Photos, and until now
# every such park needed a human to clear STOP once space came back — the iMac
# was losing a day at a time to exactly this. A disk park is a condition, not a
# decision, so it can be cleared automatically once the condition passes. The
# margin matters: resuming at the floor just re-parks on the next batch.
DISK_STOP_RE = re.compile(r"only ([\d.]+) GB free \(< ([\d.]+) GB\); STOP placed")
DISK_RESUME_MARGIN_GB = 6.0


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} {message}", flush=True)


def work_in_progress(run_dir: Path) -> bool:
    """True if anything is still tagging THIS run directory.

    Both the supervising runner and the `tag --resume` child it spawns count.
    Matching only the runner is not enough: killing a runner orphans its
    child, which keeps tagging and keeps command.lock. A replacement runner
    started underneath it cannot take that lock, so it force-restarts Photos
    in a tight loop *while the orphan is mid-export* — worse than the outage
    this script exists to fix. An orphan is still doing the work, so the
    honest answer is "busy, leave it alone".

    The pid file is deliberately not consulted: it can name a process that
    has exited, and could once name a stale pid while a healthy runner ran on.
    """
    try:
        # macOS pgrep has no -a; -f -l is the portable "full command line"
        # spelling here. Exit 1 means genuinely no match, anything above that
        # is pgrep itself failing — treat only the former as "gone".
        found = subprocess.run(
            ["pgrep", "-fl", "run_library_batches.py|phototagger.py tag"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return True  # can't tell; assume alive rather than double-start
    if found.returncode > 1:
        log(f"pgrep failed ({found.returncode}); assuming work in progress")
        return True
    target = str(run_dir.resolve())
    return any(target in line for line in found.stdout.splitlines())


def free_gb() -> float:
    stat = os.statvfs("/System/Volumes/Data")
    return stat.f_bavail * stat.f_frsize / 1_073_741_824


def disk_park_cleared(run_dir: Path) -> bool:
    """True if STOP was placed by the disk guard and space has since recovered.

    Only the runner's own low-disk message counts. A STOP a human placed, or
    one from a --stop-after target, carries no such line and is left alone —
    this must never override a deliberate pause.
    """
    log_path = run_dir / "runner.log"
    stop_path = run_dir / "STOP"
    if not log_path.exists() or not stop_path.exists():
        return False
    try:
        with log_path.open(errors="replace") as handle:
            lines = [line for line in handle.readlines() if line.strip()]
    except OSError:
        return False
    if not lines:
        return False

    # Only the LAST line counts. Scanning backwards let a stale disk-stop from
    # hours earlier authorise clearing a STOP placed since for a different
    # reason — filter exhaustion, or a person.
    match = DISK_STOP_RE.search(lines[-1])
    if not match:
        return False

    # The runner touches STOP and then logs, so its own STOP is never newer
    # than the log. A STOP touched afterwards belongs to someone else, and log
    # text cannot prove ownership on its own.
    try:
        # No tolerance: the runner touches STOP before it logs, so its own
        # STOP is always older than the log. Any slack here lets a STOP placed
        # moments later be mistaken for the runner's.
        if stop_path.stat().st_mtime > log_path.stat().st_mtime:
            log("STOP is newer than the runner's log; leaving it to its owner")
            return False
    except OSError:
        return False

    floor = float(match.group(2))
    return free_gb() >= floor + DISK_RESUME_MARGIN_GB


def stop_everything(run_dir: Path) -> int:
    """Kill the runner AND its tagger child, for THIS run only.

    Killing only the runner orphans the tagger, which keeps command.lock and
    leaves the replacement runner force-restarting Photos in a loop against a
    lock it can never take — so both must go.

    Scoped by run directory rather than a blanket pkill: a blanket kill would
    also take down a second run (or the other machine's, on a shared home) that
    this supervisor knows nothing about.
    """
    target = str(run_dir.resolve())
    try:
        found = subprocess.run(
            ["pgrep", "-fl", "run_library_batches.py|phototagger.py tag"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return 0
    killed = 0
    for line in found.stdout.splitlines():
        pid, _, rest = line.partition(" ")
        if target not in rest or not pid.isdigit():
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    if killed:
        time.sleep(5)
    return killed


def read_state(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(path: Path, state: dict[str, object]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2))
    os.replace(temp, path)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: health_check.py RUN_DIRECTORY", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1]).expanduser().resolve()
    if not (run_dir / "run.json").exists():
        log(f"no run.json in {run_dir}; nothing to supervise")
        return 2

    attention = run_dir / "NEEDS_ATTENTION"
    if attention.exists():
        return 0  # already escalated; stay quiet until a human clears it
    if (run_dir / "STOP").exists():
        if not disk_park_cleared(run_dir):
            return 0  # deliberate pause, or still short of space
        log(f"disk recovered to {free_gb():.1f} GB; clearing the disk-guard STOP")
        (run_dir / "STOP").unlink()
    try:
        status = str(json.loads((run_dir / "run.json").read_text()).get("status", ""))
    except (OSError, json.JSONDecodeError):
        status = ""
    if status == "complete":
        return 0

    state_path = run_dir / ".healthcheck.json"
    state = read_state(state_path)
    results = run_dir / "results.jsonl"
    size = results.stat().st_size if results.exists() else 0

    # Track progress on every tick, whether or not anything is running, so the
    # stall clock is always current.
    if size != state.get("last_seen_size"):
        state["last_seen_size"] = size
        state["last_progress_epoch"] = time.time()
        write_state(state_path, state)
    last_progress = float(state.get("last_progress_epoch", 0) or time.time())

    if work_in_progress(run_dir):
        stalled_minutes = (time.time() - last_progress) / 60
        if stalled_minutes < STALL_MINUTES:
            return 0  # genuinely working (or inside a normal backoff wait)
        log(
            f"tagging is alive but has written nothing for {stalled_minutes:.0f} "
            "minutes; treating as wedged and replacing it"
        )
        stop_everything(run_dir)
    failures = int(state.get("consecutive_failures", 0))
    last_restart = float(state.get("last_restart_epoch", 0) or 0)
    size_at_restart = state.get("results_size_at_restart")

    # Did the previous auto-restart accomplish anything?
    if size_at_restart is not None:
        failures = 0 if size > int(size_at_restart) else failures + 1

    if failures >= len(BACKOFF_MINUTES):
        attention.write_text(
            f"{datetime.now(timezone.utc).isoformat()}\n"
            f"{failures} auto-restarts in a row made no progress; stopping.\n"
            "Check runner.log, confirm Photos responds, then delete this file.\n"
        )
        log(f"gave up after {failures} fruitless restarts; wrote NEEDS_ATTENTION")
        state.update({"consecutive_failures": failures})
        write_state(state_path, state)
        return 0

    wait_seconds = BACKOFF_MINUTES[failures] * 60
    since = time.time() - last_restart
    if last_restart and since < wait_seconds:
        return 0  # inside the backoff window; stay quiet

    log(f"nothing is tagging (attempt {failures + 1}); restarting {run_dir.name}")
    # Reproduce the runner's original scope. Restarting bare would drop
    # --only-extensions / --only-filenames / --stop-after and the batch size,
    # turning a filtered or target-limited run into an unrestricted one.
    extra_args: list[str] = []
    args_file = run_dir / "runner.args"
    if args_file.exists():
        try:
            extra_args = [
                line.strip() for line in args_file.read_text().splitlines() if line.strip()
            ]
        except OSError:
            extra_args = []
    env = dict(os.environ)
    env_file = run_dir / "runner.env"
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key:
                    env[key] = value
        except OSError:
            pass
    if extra_args:
        log(f"restoring runner scope: {' '.join(extra_args)}")
    result = subprocess.run(
        [str(ROOT / "scripts" / "start_library_runner.sh"), str(run_dir), *extra_args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        # setsid the whole tree. launchd tears down a job's process group when
        # the job exits, which would kill the runner seconds after this script
        # returns — nohup alone does not prevent that, since the runner stays
        # in the job's group. A new session puts it out of launchd's reach.
        start_new_session=True,
    )
    log((result.stdout + result.stderr).strip()[:400])
    write_state(
        state_path,
        {
            "consecutive_failures": failures,
            "last_restart_epoch": time.time(),
            "last_restart_iso": datetime.now(timezone.utc).isoformat(),
            "results_size_at_restart": size,
            # Give the fresh runner a full stall window before judging it.
            "last_seen_size": size,
            "last_progress_epoch": time.time(),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
