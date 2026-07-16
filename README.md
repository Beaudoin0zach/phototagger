# PhotoTagger

PhotoTagger is a local-first macOS command-line tool that classifies photos in an
Apple Photos album and optionally writes the resulting labels back as Photos
keywords.

This reconstructs the archived project formerly stored as `PhotoTagger/` with
`photos_auto_tagger.py`, `mobilenetv2.mlmodel`, and `photos_env`. The source files
were not included in the April 2026 machine snapshot, but a run log confirms the
tool was used against the **House plants** album on 2025-12-16.

The new implementation is deliberately conservative:

- classification runs entirely on the Mac with Ollama and Gemma 4 vision;
- Apple's built-in Vision classifier remains available as an offline fallback;
- dry-run/review mode is the default;
- Photos is accessed through its supported scripting interface, never by editing
  the Photos database;
- generated keywords are unprefixed by default (a custom prefix remains optional);
- controlled determinations record focus, exposure, orientation, media type, text,
  screenshot subtype, document subtype, and special content;
- screenshot subtypes include messages, web pages, maps, email, memes, social
  media, search results, app screens, data tables, and error messages;
- explicit descriptive labels such as `map`, `spreadsheet`, or `membership card`
  deterministically refine overly broad structured choices;
- the generic `other` screenshot subtype remains report-only to avoid a redundant
  keyword;
- routine negatives such as `not blurry` remain in reports, while useful positive
  flags such as `blurry`, `screenshot`, or `identification card` become keywords;
- `readable text` is report-only to avoid keyword noise; `unreadable text` remains
  an actionable quality keyword;
- orientation is report-only until it proves reliable across a more varied album;
- images are decoded, auto-oriented, and converted to temporary downsized JPEGs
  with ImageMagick for local inference;
- common camera RAW formats (including Canon CR2/CR3, DNG, NEF, ARW, ORF, RAF,
  and RW2) are accepted through macOS Quick Look previews;
- existing keywords are preserved;
- every run produces JSONL and CSV audit files;
- an applied run can restore the exact previous keyword lists.

## Requirements

- macOS with Photos.app
- Apple command-line developer tools (`swiftc`)
- ImageMagick (`magick`) for reliable HEIC decoding and orientation
- Ollama with `gemma4:e4b-it-qat` (recommended)
- Python 3.10+

No Python packages or API keys are required.

## Build

```bash
cd /path/to/PhotoTagger
./scripts/build.sh
```

## Use

First list visible Photos albums. macOS may ask for permission to automate
Photos; approve it for Terminal or Codex.

```bash
./phototagger.py albums
```

Run a small review-only test. This exports temporary copies for classification
but does not change the Photos library:

```bash
./phototagger.py tag --album "House plants" --limit 10
```

Review the generated CSV under `runs/<run-id>/review.csv`. If the labels look
useful, perform a new applied run:

```bash
./phototagger.py tag --album "House plants" --limit 10 --apply
```

Tag the whole album only after the small test succeeds:

```bash
./phototagger.py tag --album "House plants" --limit 0 --apply
```

Start a resumable whole-library run. The first invocation performs a one-time
full sweep that records every photo's id into `manifest.jsonl`; from then on
every read and write is addressed purely by photo id, so the run is unaffected
by the library growing, shrinking, or reordering mid-run (for example from
background duplicate merging). Each invocation processes one batch and verifies
every applied photo by read-back before it counts as done:

```bash
./phototagger.py tag --library --batch-size 25 --order descending --apply
./phototagger.py tag --resume runs/<library-run-id>
```

Use `--order descending` for newest-to-oldest or `--order ascending` for
oldest-to-newest. Each batch CSV records the capture date. Transient AppleEvent
timeouts and dropped Photos connections are retried automatically. Photos that
were deleted after the manifest was built are recorded as `not-found` and
skipped cleanly. Every keyword write journals its intent durably before
touching Photos and only ever *adds* missing keywords, so keywords you add by
hand in Photos.app mid-run are never overwritten.

A run created before manifest traversal must be migrated once before it can
resume:

```bash
python3 scripts/migrate_run_to_manifest.py runs/<library-run-id>
```

Change a stopped whole-library run to newest-to-oldest while preserving its
prior results (this reverses the manifest traversal order):

```bash
./phototagger.py set-library-order --run runs/<library-run-id> --order descending
```

For a large library, the guarded runner can continue batches in the background.
It stops on any error or a `STOP` file; every mutating command also takes a
per-run lock, so two commands can never write the same run concurrently:

```bash
./scripts/start_library_runner.sh runs/<library-run-id>
touch runs/<library-run-id>/STOP
```

Resume an interrupted run:

```bash
./phototagger.py tag --resume runs/<run-id>
```

After a whole-library run completes, confirm nothing was skipped. For a
manifest run this is a pure local diff (instant, no Photos access) reporting
any manifest photo with no record; it writes a CSV report:

```bash
./phototagger.py coverage --run runs/<library-run-id>
```

Undo an applied run. Rollback surgically removes exactly the keywords this run
generated (union across every write attempt, case-insensitive), leaving all
other keywords — including anything you added by hand since — untouched. Each
removal is confirmed by read-back and logged to a `rollback-<timestamp>.jsonl`
audit file:

```bash
./phototagger.py rollback --run runs/<run-id>
```

Rename generated keywords from an applied run—for example, remove an earlier
`AI: ` prefix while leaving unrelated keywords untouched:

```bash
./phototagger.py rename-prefix --run runs/<run-id> --from-prefix "AI: "
```

## Useful controls

```text
--confidence 0.65   Minimum confidence for the apple backend (default: 0.65).
                    Ollama tags always carry confidence 1.0, so this gate only
                    filters Apple Vision classifications.
--backend ollama    Use Gemma 4 locally (default); apple for Apple's classifier;
                    anthropic to use the Claude API instead (requires
                    ANTHROPIC_API_KEY; pass --model, e.g. claude-sonnet-5).
                    anthropic sends photos to a third-party API — a different
                    privacy posture than the local-first ollama/apple backends.
--batch-size 25     Whole-library items per verified batch (default: 25).
                    With --resume it overrides the value saved in run.json.
--order descending  Whole-library traversal direction (default: ascending)
--model NAME        Vision model. Defaults to gemma4:e4b-it-qat for the ollama
                    backend; required explicitly for anthropic.
--max-tags 5        Maximum descriptive tags per image (default: 5).
                    Determination flags such as `screenshot`, `blurry`, or
                    `identification card` are additive on top of this cap.
--prefix "AI: "     Optional prefix for generated keywords (default: none)
--limit 25          Process at most this many images; 0 means the full album
--keep-exports      Retain exported image copies for troubleshooting
--apply             Write merged keywords to Photos
```

## Why the old version may have struggled

MobileNetV2 is an ImageNet classifier: its vocabulary is small and often produces
generic or awkward labels for personal photos, especially plant species. Other
common failure points are Live Photos exporting both a still image and companion
video, iCloud originals that must download before export, duplicate filenames,
Photos automation permissions, and accidental replacement of existing keywords.

This version addresses the operational failures and uses a multimodal model for
more useful natural-language tags. The Apple Vision fallback is faster but its
fixed classification vocabulary is more generic.

## Run artifacts

Each run directory contains:

- `run.json` — settings and progress metadata (written atomically);
- `results.jsonl` — one durable record per attempt, including `write-pending`
  journal entries appended before every Photos mutation;
- `manifest.jsonl` — whole-library runs only: the fixed photo-id worklist;
- `review.csv` / `batches/*.csv` — human-readable predictions and statuses;
- `command.lock` / `runner.lock` — concurrency locks;
- `exports/` — present only with `--keep-exports`.

Do not commit run directories containing private filenames or Photos identifiers.
