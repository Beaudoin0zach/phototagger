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

## Current state (as of the move into ~/projects, 2026-07-16)
- Reconstructed tool; original source was unrecoverable (see `RECOVERY_NOTES.md`).
- A whole-library `--apply` run is in progress and resumable:
  `runs/20260715-193432-Photos-Library/` — newest-to-oldest, batch size 2000,
  cursor at index 77,759 of 77,784.
- **`-1712` timeout handling (fixed 2026-07-16):** batch inventory and post-apply
  verification run as ≤100-item AppleScript chunks with per-call retry/backoff, so
  a timeout retries one chunk instead of killing the batch. `--batch-size` can be
  overridden at `--resume` time. The cursor never advances until every applied
  item in the batch passes read-back verification — failures become retryable
  `verify-failed` records that are reprocessed on resume. After a library run
  completes, `./phototagger.py coverage --run runs/<id>` (read-only) reports any
  photos with no record in the run.

## Resuming the library run
```bash
cd ~/projects/phototagger
rm -f runs/20260715-193432-Photos-Library/STOP     # required before it will proceed
# saved batch size is 2000; override it at resume time (recommended after the -1712 history):
./phototagger.py tag --resume runs/20260715-193432-Photos-Library --batch-size 200
# or run guarded in the background:
./scripts/start_library_runner.sh runs/20260715-193432-Photos-Library
```
