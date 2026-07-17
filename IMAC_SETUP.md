# iMac setup — second tagging front (ascending)

Context for a fresh Claude Code session on the iMac (M1, 16GB): this machine
runs a whole-library tagging run **oldest-to-newest** while the MacBook Pro
runs **newest-to-oldest**; they meet in the middle. Concurrent tagging is safe
by design — every write is an atomic add-if-missing sync, so a photo already
tagged by the other machine (keywords arrive via iCloud Photos sync) becomes a
harmless no-op. Read `CLAUDE.md` first; all its safety rules apply here.

## Prerequisites to verify before anything else
1. Signed into the same iCloud account, iCloud Photos enabled, library synced
   (Photos › Settings › iCloud). Prefer "Download Originals to this Mac" —
   otherwise each export pulls from iCloud on demand (works, slower).
2. Xcode command-line tools: `xcode-select --install` if `swiftc` is missing.
3. ImageMagick: `brew install imagemagick` (needs `magick` on PATH).
4. Ollama: `brew install ollama` (or the app from ollama.com), then
   `ollama pull gemma4:e4b-it-qat` (~5GB download) and confirm
   `ollama run gemma4:e4b-it-qat "hi"` answers.

## Setup
```bash
git clone git@github.com:Beaudoin0zach/phototagger.git ~/projects/phototagger
cd ~/projects/phototagger
./scripts/build.sh
./phototagger.py albums        # triggers the Photos automation permission
```
The `albums` command makes macOS ask "Terminal would like to control Photos" —
**click Allow on this Mac's screen**. If it was ever denied, fix it under
System Settings › Privacy & Security › Automation.

## Validate before applying (dry runs never write to Photos)
```bash
./phototagger.py tag --library --order ascending --limit 5   # review-only
```
Check the run's `review.csv` looks sane, then start the real run:

## Start the run
```bash
./phototagger.py tag --library --order ascending --apply --batch-size 300
# after that first batch completes cleanly, hand it to the guarded runner:
./scripts/start_library_runner.sh runs/<the-new-run-id>
```
The runner restarts Photos between every batch (sustained AppleEvent load
hangs Photos after ~350-450 photos) and force-restarts + resumes on a hang.
Pause anytime: `touch runs/<run-id>/STOP`.

## Coordination with the MacBook
- The MacBook's run is descending from the newest photos; this run ascends
  from the oldest. Progress meets in the middle; overlap is harmless.
- Do NOT run rollback on this machine for the MacBook's run or vice versa —
  each machine's run directory only knows its own device-local photo ids.
- When combined progress approaches the full library, either machine's
  `./phototagger.py coverage --run runs/<run-id>` reports what its own
  manifest still lacks.
