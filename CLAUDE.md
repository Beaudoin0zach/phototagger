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
- **Known robustness gap:** the earlier library run crashed on `AppleEvent timed out
  (-1712)` from Photos under sustained load. If large-batch runs keep timing out,
  reduce `--batch-size` and/or add per-item timeout + backoff around the AppleScript calls.

## Resuming the library run
```bash
cd ~/projects/phototagger
rm -f runs/20260715-193432-Photos-Library/STOP     # required before it will proceed
./phototagger.py tag --resume runs/20260715-193432-Photos-Library
# or run guarded in the background:
./scripts/start_library_runner.sh runs/20260715-193432-Photos-Library
```
