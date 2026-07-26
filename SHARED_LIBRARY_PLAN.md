# Shared-library PhotoTagger — feasibility and development plan

Status: **proposal**. Nothing here is built. Written 2026-07-26 after probing the
actual macOS APIs, because the original pitch turned out to be partly impossible.

## What I checked first, and what it killed

The pitch was: *"in a shared library, 'never delete a keyword someone else added'
stops being a safety rule and becomes the core feature."* That framing assumed
the app can tell **whose** keyword is whose. It can't. Verified against the
macOS 26.5 SDK and `Photos.sdef`:

| Question | AppleScript | PhotoKit |
|---|---|---|
| Read/write keywords | **yes** (only path) | **no API at all** |
| Is this photo in a shared library? | no | no |
| Who contributed this photo? | no | no |
| Who added this keyword? | no | no |
| Stable ID across devices | no (`localIdentifier` only) | **yes** (`PHCloudIdentifier`) |
| Fetch a *sized* image without downloading the original | no | **yes** |

`Photos.sdef` exposes six classes (album, application, container, folder, media
item, moment); `media item` has ten properties, none about provenance. The string
"shared" appears zero times. PhotoKit has zero keyword mentions — it can write
`title`, `favorite`, `hidden`, `creationDate`, `location`, and nothing else.
PhotoKit knows about *Shared Albums* (`PHAssetSourceTypeCloudShared`, the 2012
feature) but has no API for *iCloud Shared Photo Library* (the 2022 feature).

**Conclusion: per-person attribution is not buildable on current APIs.** Any claim
that the app protects Bob's keywords *specifically* would be a lie — it can only
protect *all existing keywords*, which the additive write already does today.

## What survives, and is arguably the better product

Strip attribution away and the useful half remains: **many machines and many
people tagging one logical library without duplicating work or fighting.** We are
already doing a two-machine version of this by hand (MacBook descending, iMac
ascending) and its real weakness is that neither machine knows what the other has
done — they only converge because iCloud syncs the keywords afterward.

Two API findings make the good version buildable:

1. **`PHCloudIdentifier`** (macOS 12+) maps local identifiers to IDs stable across
   devices. This is the missing primitive — a shared ledger can be keyed by cloud
   ID, so "photo X is done" means the same thing on every machine.
2. **`PHImageManager` sized requests with `networkAccessAllowed = NO`** return
   whatever is already local at the size you ask for. We currently force a full
   original download per photo (~30 MB per RAW) and then downscale to 1536 px and
   throw the rest away. This is the fix for the disk wall that just capped this
   run at 30k photos.

Finding 2 is worth doing **whether or not shared libraries ever happen**, which is
why it's stage 1.

## Plan

Each stage ships something usable alone and gates on a verification step. Stop
after any stage and the tool is still better than it is today.

### Stage 1 — Swift Photos bridge for reading (fixes the disk wall)

Grow `Sources/PhotoClassifier.swift` from "classify a file" into a small Photos
bridge, built by the existing `scripts/build.sh` with `-framework Photos` added.
Give it one new subcommand: fetch a 1536 px JPEG for a given local identifier via
`PHImageManager.requestImage(for:targetSize:)`, `networkAccessAllowed = NO` by
default, `--allow-download` to opt in per photo.

Python calls this instead of `export_library_item_by_id.applescript`. Keyword
writes stay on AppleScript — unchanged, still the only path.

- **Why first:** removes the full-original download that made the whole library
  unreachable. Local-only fetch means tagging costs ~0 disk.
- **Gate:** on 200 real photos, measure disk delta and per-photo time against the
  current export path, and diff the generated tags to confirm the smaller image
  doesn't change classifications. If tags shift materially, stop and reconsider.
- **Risk:** photos with no local preview at all return nothing — those need
  `--allow-download` or get recorded as skipped. Unknown what fraction that is;
  the gate measures it.
- **Risk:** a CLI binary touching the photo library needs TCC authorization
  (`NSPhotoLibraryUsageDescription`, user approves once). Unproven for an
  unbundled `swiftc` binary — **spike this before anything else**; it may force
  shipping a minimal `.app` bundle.

### Stage 2 — Cloud identifiers in the manifest

Extend the bridge to return `PHCloudIdentifier` alongside the local ID, and record
both in `manifest.jsonl`. Existing runs keep working on local IDs; new runs carry
cloud IDs too.

- **Gate:** build a manifest on both machines and confirm the cloud IDs for the
  same photos actually match. This is the load-bearing assumption of everything
  after — if it fails, stages 3–4 are dead and stage 1 still stands.

### Stage 3 — Shared ledger

A single append-only JSONL, keyed by cloud identifier, in a location both
machines already sync (iCloud Drive, or a git repo). Each machine appends
`{cloud_id, tagged_at, by, tags}` after a verified write, and skips photos already
present before classifying.

Keep it append-only with last-writer-wins per cloud ID — no locking across
machines, because two machines tagging the same photo is *harmless* (the write is
additive and idempotent); it's only wasteful. The ledger is an optimization, not
a correctness mechanism, and should be designed so corruption degrades to
duplicate work rather than lost work.

- **Gate:** run both machines against the same ledger and confirm the second one
  skips what the first finished, with zero duplicate classification.

### Stage 4 — Authorship by convention, not by API

Since the API can't attribute, use the existing `--prefix` as a per-author
namespace (`--prefix "AI/zach: "`). Rollback already removes only the union of
tags a run generated; a per-author prefix makes that boundary legible to humans
and makes one person's undo obviously incapable of touching another's tags.

Be honest in the UI that this is a convention: it protects against *this tool's*
mistakes, not against someone hand-editing a prefixed tag.

### Stage 5 — Consent and scope

The part that matters most and is pure product design, not API work. Tagging a
shared library means running AI over **other people's photos**. Minimum bar:
per-album or per-date scoping rather than whole-library-by-default, a visible
record of what was tagged by whom, and an explicit opt-in before a first run
touches photos the operator didn't contribute. The document-detection features
(identification card, receipt, medical) make this sharper — those labels on
someone else's photos are a real privacy event, not a nice-to-have setting.

## What I'd cut

The original "shared library" framing, as a *marketing* claim about protecting
other people's tags. It isn't enforceable and shouldn't be promised. The
defensible claim is narrower and still good: **this tool only ever adds keywords,
and can remove exactly what it added — on any library, shared or not.**
