# Trying PhotoTagger — a step-by-step guide

PhotoTagger looks at the photos in your Apple Photos library, works out what's
in them, and adds those words as **keywords** so you can search for "beach" or
"dog" later. Everything runs on your own Mac. No photos are uploaded anywhere.

This guide takes about 15 minutes. You'll use the Terminal app, but you can
copy and paste every command — you don't need to know how any of it works.

**The most important thing to know:** PhotoTagger does *nothing* to your
library unless you add the word `--apply`. Every command below is safe to run
and only shows you what it *would* do, until Step 6. And even then, Step 8
undoes it.

---

## Step 1 — Open Terminal

Press `Cmd + Space`, type `Terminal`, press Return. A window with a text prompt
opens. That's where every command below goes: paste it, press Return, wait for
the prompt to come back.

## Step 2 — Install Apple's developer tools

PhotoTagger includes a small piece of Apple code it needs to compile once.
Paste this:

```bash
xcode-select --install
```

A dialog appears — click **Install** and wait for it to finish (a few minutes).

If you see `command line tools are already installed`, you're set. Move on.

## Step 3 — Check your Python version

macOS comes with Python. PhotoTagger needs version 3.10 or newer.

```bash
python3 --version
```

If that prints `Python 3.10` or higher, continue to Step 4.

If it prints something older, or "command not found", install a current Python
from [python.org/downloads](https://www.python.org/downloads/) — download the
macOS installer, run it, then close and reopen Terminal before continuing.

## Step 4 — Download PhotoTagger and build it

```bash
git clone https://github.com/Beaudoin0zach/phototagger.git
cd phototagger
./scripts/build.sh
```

You should see `Built .../phototagger-classify`. That's the image recognizer,
compiled and ready.

> If `git clone` says "repository not found" or asks for a password, the repo is
> private — ask Zach to add you as a collaborator on GitHub.

## Step 5 — See your albums (and grant permission)

```bash
./phototagger.py albums
```

**The first time you run this, macOS will ask whether Terminal can control
Photos. Click OK.** Without that, nothing else works.

You'll get a list of your album names. Pick a small one to test with — ideally
one with 10–20 photos. You'll use its exact name in the next step.

> Didn't get a permission prompt, and it failed? Open **System Settings →
> Privacy & Security → Automation**, find **Terminal**, and switch on **Photos**.

## Step 6 — A safe test run (changes nothing)

Replace `Vacation` with your album's real name, keeping the quotes:

```bash
./phototagger.py tag --album "Vacation" --limit 5 --backend apple
```

This looks at 5 photos and tells you what keywords it *would* add. Your library
is untouched.

Photos stored in iCloud have to download first, so the first run can be slow.
That's normal.

When it finishes it prints a path ending in `review.csv`. Open it:

```bash
open runs/*/review.csv
```

That's the proposed keywords, one row per photo. Have a look and see whether
they seem reasonable.

## Step 7 — Actually add the keywords

Happy with what you saw? Run the same command with `--apply` on the end:

```bash
./phototagger.py tag --album "Vacation" --limit 5 --backend apple --apply
```

Now open Photos and search for one of the keywords. Your photos should come up.

PhotoTagger only ever *adds* keywords. Anything you'd already tagged by hand
stays exactly as it was.

Once you trust it, drop `--limit 5` to do the whole album:

```bash
./phototagger.py tag --album "Vacation" --backend apple --apply
```

## Step 8 — Changing your mind (undo)

Every applied run can be undone. Find the run's folder name:

```bash
ls runs/
```

Then, using the one you want to undo:

```bash
./phototagger.py rollback --run runs/PASTE-THE-NAME-HERE
```

This removes only the keywords *that run* added. Keywords you added yourself
are never touched.

---

## Optional: better keywords

The steps above use Apple's built-in image recognition (`--backend apple`).
It's instant and needs no setup, but its vocabulary is genuinely limited and
often abstract. A real example — this is Apple Vision describing a photo of a
houseplant on a windowsill:

> structure | wood processed | conveyance | portal | window

Technically true, not very useful. Expect that, and treat `--backend apple` as
a way to confirm the plumbing works rather than as the finished product.

The local AI model describes the same kind of photo as something closer to
`potted plant | windowsill | terracotta pot | sunlight`. If you want keywords
you'd actually search for, it's worth the bigger install.

This is a bigger install (about 6 GB) and everything still stays on your Mac —
no photos leave your machine either way.

```bash
# Install Homebrew (skip if you already have it)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install the pieces
brew install ollama imagemagick ffmpeg

# Start the AI service and download the model (~6 GB, one time)
brew services start ollama
ollama pull gemma4:e4b-it-qat
```

Then just leave off `--backend apple` — the better model is the default:

```bash
./phototagger.py tag --album "Vacation" --limit 5
```

What each piece is for:

| Tool | Why |
|---|---|
| `ollama` | runs the smarter image model on your Mac |
| `imagemagick` | better HEIC handling, and detects which camera or phone took each photo |
| `ffmpeg` | lets it tag **videos** as well as photos |

---

## Good habits

- **Start small.** One album, `--limit 5`, no `--apply`. Confirm you like it
  before doing anything larger.
- **Review before applying.** The `review.csv` exists precisely so you never
  apply blind.
- **Keep your `runs/` folder private.** It records your photo filenames. Don't
  post it anywhere or attach it to a bug report.
- **You can stop anytime.** Press `Ctrl + C`. Nothing is left half-written —
  each photo is finished completely or not started.

## If something goes wrong

| What you see | What to do |
|---|---|
| `Photos automation` errors, or empty album lists | System Settings → Privacy & Security → Automation → Terminal → enable Photos |
| `command not found: ./phototagger.py` | You're in the wrong folder. Run `cd phototagger` first. |
| `0 candidate still images` | Usually low disk space — the tool must download originals from iCloud. Free up some room and retry. |
| Very slow first run | iCloud downloads. It speeds up once photos are local. |
| Anything else | Send Zach the error text and the last few lines of output — but not your `runs/` folder. |
