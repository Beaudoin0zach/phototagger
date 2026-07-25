---
name: check-run
description: Diagnose the health and progress of a PhotoTagger whole-library tagging run. Use when asked "how's the tagging run going", to check if the guarded runner is alive, to interpret run status / errors / a Photos hang, to safely pause or resume, or to see how close the ascending and descending fronts are to meeting.
---

# Checking a PhotoTagger library run

A whole-library run lives in a run directory under `runs/` (e.g.
`runs/20260717-195442-Photos-Library/`). Two machines tag concurrently — the
iMac ascends from the oldest photo, the MacBook descends from the newest — and
they meet in the middle. **Every machine only knows about its own run
directory; never inspect, resume, or roll back the other machine's run.**

Read `CLAUDE.md` for the design and safety rules. This skill is the diagnostic
procedure: how to answer "how's it going?" and what to do about the answer.

## Step 1 — find the run directory

```bash
ls -dt runs/*/ | head        # most-recently-touched run first
```

Use the one the current machine has been working. If unsure which order this
machine runs, `python3 -c "import json;print(json.load(open('runs/<id>/run.json'))['order'])"`.

## Step 2 — is the guarded runner alive?

The runner is the background loop that restarts Photos between batches. It is
NOT the same as a single `tag --resume` batch.

```bash
run=runs/<id>
cat "$run/runner.pid" 2>/dev/null                 # last runner PID
ps -p "$(cat "$run/runner.pid")" -o pid=,etime=,command= 2>/dev/null
pgrep -fl run_library_batches.py                  # any runner process at all
```

- PID alive → runner is looping. Good.
- `runner.pid` present but process gone → runner exited (finished, hit a stop
  condition, or was killed). Check the log tail (Step 4) for why.
- No `runner.pid` and no process → run was never handed to the guarded runner,
  or is being driven by one-off `tag --resume` calls. Both are valid; just know
  which you're looking at.

## Step 3 — how much is done?

```bash
python3 - "$run" <<'PY'
import json, sys, pathlib
run = pathlib.Path(sys.argv[1])
m = json.load(open(run/"run.json"))
# Latest record per photo wins (matches the tool). A photo counts as done
# only if its LATEST record is a durable, non-retryable status.
retry = {"error", "verify-failed", "write-pending"}
latest = {}
for line in (run/"results.jsonl").open() if (run/"results.jsonl").exists() else []:
    r = json.loads(line)
    pid = r.get("photo_id") or r.get("identifier")
    if pid:
        latest[pid] = r.get("status")
done = {pid for pid, st in latest.items() if st not in retry}
total = m.get("manifest_total", 0)
print(f"status={m.get('status')} order={m.get('order')}")
print(f"progress: {len(done)}/{total} photos ({100*len(done)/total:.1f}%)" if total else "no manifest yet")
print(f"last invocation: applied={m.get('applied_this_invocation')} errors={m.get('errors_this_invocation')}")
PY
```

`run.json` `status` values and what they mean:

| status | meaning |
|---|---|
| `running` | a batch is executing right now |
| `batch_in_progress` | apply batch started; if no process is running, it died mid-batch — resume re-verifies it |
| `batch_complete` | batch finished cleanly, more photos remain |
| `batch_errors` | batch finished with per-item errors; resume retries them |
| `complete` | every manifest photo has a durable record — done |
| `complete_with_errors` | finished but some photos never succeeded; see coverage |

Per-photo statuses in `results.jsonl`: `applied`/`changed`/`unchanged`/`removed`
are success; `error`/`verify-failed`/`write-pending` are retryable and re-enter
pending automatically on the next resume. `review` records come from dry runs.

## Step 4 — read the tail of the logs

```bash
tail -20 "$run/runner.log"              # runner lifecycle: restarts, hangs, stops
ls -t "$run/batches"/*.csv | head -1    # newest batch CSV — spot-check labels
```

Healthy runner log lines look like `starting next batch of 300` and
`restarting Photos (force=False)`. Trouble signals:

- `batch reported a Photos hang (N in a row); force-restarting` — the runner
  force-kills and relaunches Photos itself. One or two in a row is normal; it
  gives up (`Photos hung N batches in a row; stopping for human eyes`) after 5.
  **Check free disk before anything else** — see below.
- `N consecutive batches made no progress; stopping` — 3 no-progress batches;
  needs human eyes. **Check free disk first**, then that Photos opens.

### Photos hangs are almost always a DISK-SPACE problem

**Check this first, before restarting anything:**

```bash
df -h /System/Volumes/Data | tail -1        # want ≥ 20 GB, comfortably more
```

Measured on the live run (2026-07-25): at 12 GB free, a day produced 675 applied
photos and **544** empty-export failures; the next day at 64 GB free produced 674
applied and **0** failures. Same throughput — the only variable was free space.

Why: iCloud "Optimize Mac Storage" means most photos have no local original, so
every export must first *download* it, and these libraries are RAW-heavy (~30 MB
per CR3). With the disk near full those downloads have nowhere to land, `export`
returns zero files, and Photos eventually wedges into 120s timeouts.

So `Photos export produced 0 candidate still images` means **check free disk**,
not "Photos is cold". An earlier theory blamed sustained AppleEvent load; the
restart-between-batches machinery came from that theory and treats a symptom.
The watchdog's 20 GB disk guard is what matches the real cause.

If space is tight, look at `~/Library/Metadata/CoreSpotlight` first — a bloated
search index (pure derived data; macOS rebuilds it) was 74 GB on the MacBook.
Restart `spotlightknowledged`/`corespotlightd` afterwards to release handles on
the deleted files. Do **not** go hunting in `~/data/public-ledger`: single-copy,
no cloud backup, and its "derived" DBs are cited evidence in `CLAIMS.md`.
Do not manually force iCloud eviction either — with Optimize already on, macOS
evicts under pressure by itself and the run's downloads just refill it.
- `whole-library run complete` — done on this machine.

## Step 5 — how close are the two fronts to meeting?

`coverage` re-sweeps the library and reports which photos this run's manifest
still lacks a record for (read-only, safe anytime):

```bash
./phototagger.py coverage --run "$run"
```

Because the other machine's keywords arrive via iCloud sync as harmless
no-op merges, when the two fronts overlap this machine's remaining count drops
toward zero even for photos it never personally processed.

## Safe interventions (in order of preference)

1. **Wait.** A single hang, a slow batch, or Photos restarting is normal.
2. **Pause:** `touch "$run/STOP"`. The runner finishes nothing new and exits at
   the next loop check; any in-flight batch's work is already durable.
3. **Resume after a pause:** remove the stop file, then restart the runner:
   ```bash
   rm -f "$run/STOP"
   ./scripts/start_library_runner.sh "$run"
   ```
4. **Resume one batch by hand** (no guarded restarts): `./phototagger.py tag --resume "$run"`.

Never delete `results.jsonl`, `manifest.jsonl`, or `run.json` — they are the
durable record of what was applied and what rollback would undo. Do not run
`rollback` here for the other machine's run.
