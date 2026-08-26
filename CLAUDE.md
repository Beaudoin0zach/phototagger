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

## Current state (2026-08-03)
- Reconstructed tool; original source was unrecoverable (see `RECOVERY_NOTES.md`).
- **RAW archive complete; video pass complete.** The whole-library run
  `runs/20260715-193432-Photos-Library/` finished its `--only-extensions=CR2`
  pass (9,554 of 9,567 tagged; 11 not-found, 2 unrecoverable) and, overnight
  2026-08-02→03, its `--only-extensions=MP4,MOV,M4V,AVI` pass: **all 1,215
  manifest videos applied, zero errors** — including the 202 once stuck as
  `skipped-unsupported`. 24,262 of 77,753 manifest photos now have durable
  records; the remaining backlog is **53,491 older JPG/HEIC stills**. The run
  is **parked** (STOP in place, placed by the filter-exhaustion path); resume
  without any filter to continue. The video pass drove disk to ~25 GB free —
  let macOS evict the Photos cache (do NOT force-evict) before resuming. The
  iMac front is tagging HEIC ascending; its keywords sync via iCloud but its
  records stay device-local.
- **Videos are tagged now** (2026-08-02): ffmpeg extracts one frame 10% into the
  clip and that frame runs through the normal classifier; everything downstream
  is identical to a still. Device detection can't use EXIF — iPhone footage sets
  `com.apple.quicktime.make`, while **DJI writes no maker at all** and is
  recognized by its stream handler names (`DJI.AVC`). A truncated clip (missing
  `moov` atom) makes ffmpeg exit 0 having written nothing, so the extractor
  checks the file on disk, not the return code. ffmpeg/ffprobe are now
  dependencies for video runs only.
- **`skipped-unsupported` is not permanent.** It is not a retry status, so the
  202 movies skipped by earlier runs would have stayed untagged forever after
  video support landed. `pending_items` now keys that exclusion on the *current*
  supported extension set, re-opening them automatically. Any future type
  addition behaves the same way — don't "fix" this by hand-editing records.
- **Printed coverage counts were wrong with a filter** (fixed 2026-08-02). Both
  the batch header and `Library progress:` subtracted the *filtered* pending set
  from the *unfiltered* manifest, so a 54-photo filtered batch printed
  "77753 of 77753" while ~54.5k photos had never been processed. `run.json` was
  always correct; only the display lied. Given this project already shipped one
  false coverage claim (see the id-based traversal note below), treat any
  progress number that looks suspiciously complete as a bug until proven.
- **Drone media is tagged — all 72 items.** The 54 DJI stills and 14 videos in
  the manifest, plus 4 imported on 2026-08-02 (3 healthy orphans off the
  Desktop and `DJI_0007_recovered.MP4`, 7:54 of footage rebuilt with untrunc
  from a moov-less 2 GB file, donor DJI_0005). All carry `Drone` plus
  descriptive tags. The imports are NOT in the whole-library manifest — the
  manifest is fixed at build time — so they were tagged via the "DJI Imports"
  album run (`runs/20260802-174724-DJI-Imports`, independently rollback-able).
  Any future imports need the same album treatment or a fresh manifest.
  `--only-filenames=SUBSTR` (mirrors `--only-extensions`, same non-destructive
  semantics) is how the manifest DJI items were scoped.
- **Color palettes are captured** (2026-08-16). Every tagged photo's record now
  carries a `palette` field (dominant + 5 weighted hex swatches), computed via
  ImageMagick from the same export the classifier consumes — fail-soft, so a
  palette failure can never fail a photo (`image_palette` in `phototagger.py`).
  The backlog was backfilled by `scripts/extract_colors.py`, which reads Photos
  derivative thumbnails (read-only file access, no scripting interface, no
  iCloud downloads) into `runs/<id>/colors.jsonl`: 76,700 of 77,753 manifest
  photos covered; rerun the script anytime to retry the ~1k without a local
  derivative. Join to manifest/results via `photo_id.split("/")[0]`. The
  "Field Colors" artifact dashboard was built from this data.
- **Four backends** since the bring-your-own-API merge (2026-08-02, was unmerged
  on `claude/angry-jang-97cca4` since 07-28): `ollama` (default, local Gemma),
  `openai-compatible` (one adapter for OpenAI/OpenRouter/Groq/Together/
  DeepInfra/LM Studio/vLLM and Ollama's own `/v1`), `anthropic`, and `apple`
  (local Vision). Keys live in the macOS Keychain, never a plaintext file.
  **Run `test-backend` before any bulk run** on a new provider/model/key — it
  costs one API call and touches nothing in Photos. Note `openai-compatible`
  and `anthropic` send photos to a third party; the other two do not.
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
  itself checks free space before every batch and places `STOP` below
  `MIN_FREE_GB` (20). This lives in `scripts/run_library_batches.py` so it
  survives session end — an earlier version lived in a supervising shell loop,
  which silently died with its session and let a run grind down to 18 GB.
- "0 candidate still images" means *check free disk first*, not "Photos is cold".
  (A 20s inline "warm-up retry" built on the wrong theory was removed on
  2026-07-25 — a failed export already becomes a retryable `error` record that
  re-enters pending on the next batch, so the inline stall bought nothing.)
- Don't manually force iCloud eviction to reclaim space: with Optimize already
  on, macOS evicts under pressure by itself, and the run's downloads immediately
  refill it — you just create download/evict churn.
- `~/data/public-ledger` was migrated to the Seagate on 2026-07-27: `federal/`,
  `state/`, `foreign/` are now **symlinks** to the external drive (272 GB
  reclaimed); `unmasker/` stays local because it is not re-downloadable. Don't
  look for more space there — what remains is deliberate. See the
  `public-ledger-depot-single-copy` memory, and never delete against a backup
  that hasn't been checksum-verified (that drive had a silently corrupt file
  with matching size and mtime).

Where the space actually was, when this came up: a bloated **CoreSpotlight index**
(74 GB → 3 MB; pure derived data, macOS rebuilds it). Check
`~/Library/Metadata/CoreSpotlight` before anything else, and restart
`spotlightknowledged`/`corespotlightd` afterwards to release deleted-file handles.

- **Model upgraded mid-run** (2026-08-16): the whole-library run switched from
  `gemma4:e4b-it-qat` to `gemma4:26b-a4b-it-qat` at ~25,700 photos complete —
  photos before that boundary carry the smaller model's blander vocabulary;
  everything after gets the 26B's scene-level labels. Same family on purpose, to
  minimize vocabulary drift. Throughput is unchanged (~20 s/photo; MoE, ~4B
  active). The e4b was deleted from Ollama to free disk — deleting it is also
  what finally triggered the lazy iCloud eviction that a parked run and 14 GB
  free had failed to trigger for an hour. Resume reads the model from
  `run.json`, so future model changes are: park (STOP), `test-backend` the new
  model, edit `run.json`'s `model` field, resume.

- **A launchd health check now supervises the runner** (2026-08-20). The runner
  survives session end but nothing watched *it*: three separate stops went
  unnoticed for two weeks, ~6 days (iMac), and 5.5 hours. Install with
  `./scripts/install_health_check.sh <run-dir>` (`--uninstall` to remove); it
  runs `scripts/health_check.py` every 5 minutes and restarts the runner only
  when the stop looks accidental. It refuses to act on a `STOP` file, a
  completed run, or a `NEEDS_ATTENTION` marker, and it backs off 15m/1h/3h
  before giving up — a wedged Photos recovers with idle time, not retries.
  Two traps found while building it, both worth not re-learning:
  - **launchd kills a job's whole process group when the job exits**, so a
    plain `nohup` runner died seconds after each health-check tick. The spawn
    uses `start_new_session=True` to escape the job's group.
  - **Killing a runner orphans its `tag --resume` child**, which keeps tagging
    and keeps `command.lock`. A replacement runner cannot take that lock and
    force-restarts Photos in a loop *while the orphan is mid-export*. The
    health check therefore treats any live runner **or tagger** as "busy".
    When stopping by hand, `pkill` both patterns.
  - **"Busy" must mean progressing, not merely alive** (learned 2026-08-24, at
    the cost of ~12 hours). A tagger wedged on Photos stays up and writes
    nothing, and the liveness-only check above called that busy and waited all
    night. The check now also requires `results.jsonl` to grow: quiet for over
    `STALL_MINUTES` (45) with something alive means wedged, so it kills the
    run's runner and tagger and starts fresh. 45 is deliberate — the runner's
    own no-progress backoff legitimately goes silent for ~22 minutes (10m then
    20m waits plus a failing batch), so anything tighter kills healthy waits.
    Kills are scoped by run directory, never a blanket `pkill`.
  - **A disk park clears itself** (2026-08-26). The runner's low-disk `STOP` is
    a condition, not a decision, and every one used to need a human to clear
    it — the iMac lost days to that. The health check now re-reads the
    runner's own `only N GB free (< M GB); STOP placed` line and removes the
    `STOP` once free space reaches the floor plus `DISK_RESUME_MARGIN_GB` (6);
    resuming exactly at the floor just re-parks on the next batch. A `STOP`
    without that line — a human pause, or a `--stop-after` target — is never
    touched.

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
