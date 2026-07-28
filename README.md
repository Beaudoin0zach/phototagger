# PhotoTagger

PhotoTagger is a local-first macOS command-line tool that classifies photos in an
Apple Photos album and optionally writes the resulting labels back as Photos
keywords.

This reconstructs the archived project formerly stored as `PhotoTagger/` with
`photos_auto_tagger.py`, `mobilenetv2.mlmodel`, and `photos_env`. The source files
were not included in the April 2026 machine snapshot, but a run log confirms the
tool was used against the **House plants** album on 2025-12-16.

The new implementation is deliberately conservative:

- classification runs entirely on the Mac by default, with Ollama and Gemma 4 vision;
- bring your own API instead if you prefer: any OpenAI-compatible provider or the
  Claude API, with keys read from the macOS Keychain rather than a plaintext file;
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
- Ollama with `gemma4:e4b-it-qat` (recommended), **or** an API key for any
  hosted provider — see [Choosing a backend](#choosing-a-backend)
- Python 3.10+

No Python packages are required. No API key is required for the default local
backend.

## Build

```bash
cd /path/to/PhotoTagger
./scripts/build.sh
```

## Choosing a backend

PhotoTagger runs classification through one of four backends. Tag quality is the
whole point of the tool, so pick deliberately.

| Backend | What it is | Key needed | Notes |
|---|---|---|---|
| `ollama` (default) | Local Gemma 4 vision via Ollama | none | Good tags, private, slow-ish. **See RAM below.** |
| `openai-compatible` | Any provider speaking the OpenAI chat API | yes (or none for a local server) | OpenAI, OpenRouter, Groq, Together, DeepInfra, LM Studio, vLLM, Ollama's own `/v1` |
| `anthropic` | Claude API | yes | Highest tag quality in testing |
| `apple` | Apple's built-in Vision classifier | none | Compatibility floor only — see below |

`openai-compatible` and `anthropic` send your photos to a third-party API. That
is a materially different privacy posture from the local-first `ollama` and
`apple` backends. Decide that before pointing either one at a whole library.

### Local model RAM requirements

Disk size badly understates what a local model needs. Measured here:

| Model | On disk | Resident while running |
|---|---|---|
| `gemma4:e4b-it-qat` | 5.7 GB | **~11 GB** |

A 16 GB Mac runs that under heavy memory pressure; an 8 GB Mac cannot run it at
all. If you have 16 GB and want the machine to stay usable, a hosted backend is
the better trade.

### About the `apple` backend

Apple's Vision classifier is a compatibility floor, not a real option for a
searchable library. Its vocabulary is fixed and generic: asked to label a photo
of a Canada goose, it returned `land, outdoor, grass`. Nothing in that is
findable later. Use it only when no model backend is available at all.

## Use

First list visible Photos albums. macOS may ask for permission to automate
Photos; approve it for Terminal or Codex.

```bash
./phototagger.py albums
```

### Validate a backend before any bulk run

**Always run `test-backend` first when using a new provider, model, or key.** It
classifies exactly one photo, prints the tags it would write, and touches
nothing in Photos. A bad key or an unsupported parameter costs one API call to
find here, versus discovering it partway into a nine-thousand-photo run:

```bash
./phototagger.py test-backend --backend openai-compatible --model gpt-5-mini
```

If you are unsure what the provider calls its models, ask it:

```bash
./phototagger.py test-backend --backend openai-compatible --list-models
```

To validate a credential without involving Photos at all, point it at any image
file on disk:

```bash
./phototagger.py test-backend --backend openai-compatible --model gpt-5-mini --image ~/Desktop/test.jpg
```

### API keys

Keys are read from the **macOS Keychain first**, then from the environment.
Never put a key in a plaintext file in this repo.

Add a key to the keychain (this prompts for the value, so it stays out of your
shell history):

```bash
security add-generic-password -s phototagger-openai -a phototagger -w
```

The service name is `phototagger-<provider>`, where the provider is derived from
`--api-base`: `phototagger-openai`, `phototagger-openrouter`, `phototagger-groq`,
`phototagger-anthropic`, and so on. `test-backend` prints the provider name it
resolved, so you can confirm which item it will read.

Environment fallback, checked in order: the provider-specific variable
(`OPENROUTER_API_KEY`, `GROQ_API_KEY`, …), then `OPENAI_API_KEY`. The
`anthropic` backend uses `ANTHROPIC_API_KEY`. Keys are stripped of surrounding
whitespace, and a key containing an interior space is rejected up front rather
than sent to produce a confusing `401`.

### Non-OpenAI providers

Point `--api-base` at any endpoint ending in `/v1`:

```bash
# OpenRouter
./phototagger.py test-backend --backend openai-compatible \
  --api-base https://openrouter.ai/api/v1 --model qwen/qwen3-vl-235b-a22b-instruct

# LM Studio or vLLM on this machine (no key required)
./phototagger.py test-backend --backend openai-compatible \
  --api-base http://localhost:1234/v1 --model local-vision-model

# Ollama's own OpenAI-compatible endpoint
./phototagger.py test-backend --backend openai-compatible \
  --api-base http://127.0.0.1:11434/v1 --model gemma4:e4b-it-qat
```

Once `test-backend` prints sensible tags, use the same flags with `tag`:

```bash
./phototagger.py tag --album "House plants" --limit 10 \
  --backend openai-compatible --api-base https://openrouter.ai/api/v1 \
  --model qwen/qwen3-vl-235b-a22b-instruct
```

The backend, model, and `--api-base` are saved in `run.json`, so `--resume`
continues against the same provider without repeating them.

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
--backend ollama    Use Gemma 4 locally (default); openai-compatible for any
                    provider speaking the OpenAI chat API (pair with --api-base);
                    anthropic for the Claude API; apple for Apple's classifier.
                    The two API backends send photos to a third-party service —
                    a different privacy posture than local-first ollama/apple.
                    Validate any of them with `test-backend` first.
--api-base URL      Endpoint for --backend openai-compatible, ending at /v1
                    (default: https://api.openai.com/v1). A full .../chat/
                    completions URL is accepted and trimmed.
--batch-size 25     Whole-library items per verified batch (default: 25).
                    With --resume it overrides the value saved in run.json.
--order descending  Whole-library traversal direction (default: ascending)
--model NAME        Vision model. Defaults to gemma4:e4b-it-qat for the ollama
                    backend; required explicitly for anthropic and
                    openai-compatible (no silent local default is substituted).
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
more useful natural-language tags. The Apple Vision fallback is faster, but its
fixed vocabulary is generic enough to be unusable for search — see
[About the `apple` backend](#about-the-apple-backend).

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
