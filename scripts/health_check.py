#!/usr/bin/env python3
"""Restart a parked-by-accident library runner; leave deliberate stops alone.

Three times this project has lost hours-to-weeks of tagging because the
guarded runner exited and nothing noticed: a mid-batch death (2026-08-03,
unnoticed for two weeks), an interrupted decision on the iMac (~6 days), and
a Photos hang that tripped the circuit breaker (2026-08-20, 5.5 hours). The
runner survives session end, but nobody was watching *it*.

Run this from launchd every few minutes. It restarts the runner only when the
stop looks accidental, and it never fights a stop that was meant:

  * runner or tagger alive  -> work is happening, nothing to do
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
        return 0  # deliberate pause — never override it
    if work_in_progress(run_dir):
        return 0

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
    result = subprocess.run(
        [str(ROOT / "scripts" / "start_library_runner.sh"), str(run_dir)],
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
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
