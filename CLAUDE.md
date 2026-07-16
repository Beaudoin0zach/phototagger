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

## Current state (2026-07-16, after the second remediation round)
- Reconstructed tool; original source was unrecoverable (see `RECOVERY_NOTES.md`).
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
- The pre-manifest whole-library `--apply` run `runs/20260715-193432-Photos-Library/`
  must be migrated once before resuming:
  `python3 scripts/migrate_run_to_manifest.py runs/20260715-193432-Photos-Library`
  (builds the manifest via one full sweep; preserves all existing results).

## Resuming the library run
```bash
cd ~/projects/phototagger
rm -f runs/20260715-193432-Photos-Library/STOP     # required before it will proceed
./phototagger.py tag --resume runs/20260715-193432-Photos-Library --batch-size 200
# or run guarded in the background:
./scripts/start_library_runner.sh runs/20260715-193432-Photos-Library
```
