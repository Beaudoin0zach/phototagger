# Recovery notes

## What was found

The old home-directory inventory at `projects/workbench/list.txt` recorded these
items together on 2025-12-18:

- `PhotoTagger/`
- `photos_auto_tagger.py`
- `mobilenetv2.mlmodel`
- `photos_env/`
- `photo_tagger_House_plants.txt`

The Seagate snapshot at
`/Volumes/Seagate/pre-wipe-snapshot-2026-04-20/` contains selected Desktop,
Documents, Downloads, dotfile, and `projects/` trees. It does not include the old
home-directory root where the first four items lived. A complete filename search
of the mounted Seagate found no second copy.

The run log survived under the archived Desktop tree and contains:

```text
Album: House plants
Started: 2025-12-16 16:29:37
======================================================================
```

This confirms the target album and approximate architecture, but the original
source is not recoverable from the mounted backup.

## Reconstruction verification — 2026-07-15

- Apple Photos scripting definition confirms `media item.keywords` is writable.
- Photos album enumeration succeeded and found `House plants`.
- The local Apple Vision classifier compiled and classified a test image.
- A one-photo dry run against `House plants` completed without modifying Photos.
- A five-photo dry run reproduced a likely historical issue: Live Photos export
  both a still image and a companion video.
- The exporter was fixed to select the still component.
- Resuming the same run retried all four failed Live Photos successfully.
- Unit tests cover label normalization, keyword merging, and Live Photo selection.

No Photos keywords were changed during reconstruction or verification.

