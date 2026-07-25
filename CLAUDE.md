# PhotoTagger — project notes for Claude

Local-first macOS CLI that classifies Apple Photos images and (optionally) writes
labels back as Photos keywords. See `README.md` for full usage; this file captures
conventions and safety rules specific to working in this repo.

## What this is
- `phototagger.py` — main CLI (argparse; `main()` entry point, also exposed as the
  `phototagger` console script via `pyproject.toml`).
- `Sources/PhotoClassifier.swift` — Apple Vision classifier, compiled by
  `scripts/build.sh` to `.build/phototagger-classify` (regenerable; gitignored).
- `applescript/` — supported Photos.app scripting interface (never edits the Photos DB).
- `scripts/` — `build.sh`, plus the guarded background library runner.
- `tests/test_phototagger.py` — unit tests (label normalization, keyword merge, Live Photo selection).

## Build & test
```bash
./scripts/build.sh                 # compiles the Swift classifier
python3 -m pytest tests/           # or: python3 tests/test_phototagger.py
```

## Safety rules (important)
- **Dry-run / review is the default.** Only `--apply` writes to the Photos library.
- **Never commit `runs/`** — it contains private photo filenames and Photos identifiers.
  Already gitignored, along with `.build/` and `__pycache__/`.
- Photos is only ever accessed through its scripting interface; existing keywords are preserved.
- Every applied run is reversible: `phototagger.py rollback --run runs/<run-id>`.

## Current state (2026-07-25)
- Reconstructed tool; original source was unrecoverable (see `RECOVERY_NOTES.md`).
- Whole-library `--apply` run in progress: `runs/20260715-193432-Photos-Library/`
  (descending, manifest-based, ~9.8k of 77,753 done). Migration to the manifest
  format is **complete** — no further migration step is needed.
- **Library traversal is manifest + id-based** (v5 runs): a one-time positional
  sweep records every photo id into `run_dir/manifest.jsonl`; all subsequent
  reads/writes address photos purely by id (`media item id ...`, ~0.13s at any
  library position vs seconds for high positional indexes). Live library
  growth/shrinkage no longer skews runs — this replaced the positional cursor
  after background duplicate-merging shrank the library mid-run and a coverage
  sweep showed 79% of a "covered" index range had never actually been processed.
- **Write safety:** every keyword write appends a durable `write-pending`
  journal record before touching Photos, then the atomic sync AppleScript
  (read-current + add-missing, case-insensitive) makes writes idempotent and
  incapable of clobbering concurrent manual keyword edits. `rollback` removes
  exactly the union of generated tags (never blind-restores a stale snapshot).
  All mutating commands take a per-run `command.lock`; `run.json` is written
  atomically. Verification is a subset check (our tags present), so users
  editing keywords mid-run never trigger false failures.
- **AppleScript gotcha learned the hard way:** `set listB to listA` aliases;
  `set end of listB ...` then mutates both, and `is not` compares lists by
  value — always `copy` and use an explicit changed-flag (see
  `applescript/sync_library_keywords_by_id.applescript`).
## Photos hangs are a DISK-SPACE problem, not an AppleEvent-load problem (2026-07-25)

Earlier notes in this file (and the restart machinery in
`scripts/run_library_batches.py`) blamed Photos hangs on sustained AppleEvent
load. **That diagnosis was wrong.** Measured evidence:

| Date | photos applied | empty-export failures | free disk |
|---|---|---|---|
| 2026-07-23 | 900 | 0 | comfortable |
| 2026-07-24 | 675 | **544** | **12 GB** |
| 2026-07-25 | 674 | **0** | 64 GB (after cache cleanup) |

Same throughput, 544 → 0 failures; the only variable was free space.

**Mechanism.** iCloud Photos "Optimize Mac Storage" is ON, so ~93% of the
library has no local original (5,171 local files vs 77,753 items). Every export
must first *download* the original — and this library is RAW-heavy (2,349 CR3
files averaging **30 MB each**, 78% of local bytes). With the disk near full
those downloads have nowhere to land, so `export` returns zero files and Photos
eventually wedges into 120s timeouts.

**Implications for anyone working here:**
- Keep **≥ 20 GB free** (comfortably more) before starting a batch. The runner
  watchdog auto-parks the run below 20 GB.
- "0 candidate still images" means *check free disk first*, not "Photos is cold".
  (A 20s inline "warm-up retry" built on the wrong theory was removed on
  2026-07-25 — a failed export already becomes a retryable `error` record that
  re-enters pending on the next batch, so the inline stall bought nothing.)
- Don't manually force iCloud eviction to reclaim space: with Optimize already
  on, macOS evicts under pressure by itself, and the run's downloads immediately
  refill it — you just create download/evict churn.
- Do NOT hunt for space in `~/data/public-ledger` (332 GB). It is single-copy
  with no cloud backup, and its large "derived" DBs are cited evidence in
  `CLAIMS.md`. See the `public-ledger-depot-single-copy` memory.

Where the space actually was, when this came up: a bloated **CoreSpotlight index**
(74 GB → 3 MB; pure derived data, macOS rebuilds it). Check
`~/Library/Metadata/CoreSpotlight` before anything else, and restart
`spotlightknowledged`/`corespotlightd` afterwards to release deleted-file handles.

## Resuming the library run
```bash
cd ~/projects/phototagger
df -h /System/Volumes/Data | tail -1        # confirm healthy free space FIRST
rm -f runs/20260715-193432-Photos-Library/STOP
./scripts/start_library_runner.sh runs/20260715-193432-Photos-Library
# the runner picks its own batch size (default 300) and manages Photos lifecycle
```
A second front runs on the iMac ascending from the oldest photos (see
`IMAC_SETUP.md`); the two converge in the middle. Concurrent tagging is safe —
writes only ever add missing keywords — but never run one machine's `rollback`
against the other's run directory (photo ids are device-local).
