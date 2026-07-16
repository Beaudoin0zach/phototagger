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

Start a resumable 25-item whole-library run. Each invocation processes one batch,
verifies Photos read-back, and advances the saved cursor only after success:

```bash
./phototagger.py tag --library --batch-size 25 --order descending --apply
./phototagger.py tag --resume runs/<library-run-id>
```

Use `--order descending` for newest-to-oldest or `--order ascending` for
oldest-to-newest. Each batch CSV records the capture date. Transient AppleEvent
timeouts are retried automatically.

Change an existing stopped whole-library run to newest-to-oldest while preserving
its prior results:

```bash
./phototagger.py set-library-order --run runs/<library-run-id> --order descending
```

For a large library, the guarded runner can continue batches in the background.
It stops on any error, a change in the Photos library count, or a `STOP` file:

```bash
./scripts/start_library_runner.sh runs/<library-run-id>
touch runs/<library-run-id>/STOP
```

Resume an interrupted run:

```bash
./phototagger.py tag --resume runs/<run-id>
```

After a whole-library run completes, confirm nothing was skipped (for example
by photos shifting position mid-run). This sweep is read-only and writes a CSV
report of any library photos that have no record in the run:

```bash
./phototagger.py coverage --run runs/<library-run-id>
```

Restore the exact keyword lists recorded before an applied run. Rollback uses
each photo's earliest recorded snapshot, verifies every restore by reading it
back from Photos, and writes a `rollback-<timestamp>.jsonl` audit file:

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
--model NAME        Ollama model name (default: gemma4:e4b-it-qat)
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

- `run.json` — settings and progress metadata;
- `results.jsonl` — one durable record per attempted photo;
- `review.csv` — human-readable predictions and statuses;
- `exports/` — present only with `--keep-exports`.

Do not commit run directories containing private filenames or Photos identifiers.
