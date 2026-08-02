# Session failure log

Searchable history of what went wrong, so patterns across sessions become visible.

## Session: 2026-07-28

**Project:** phototagger (bring-your-own-API backend; restarted the library run)

### Failures

**Read a diagnostic number as the wrong thing (repeat of a known shape)**
- Reported the check-run skill's progress script as the run's completion figure: **20%**, while the
  runner's own `APPLY BATCH:` line said **90%**. The script counts only photos *this* machine has a
  record for; the tool also treats a photo as complete when it already carries generated keywords,
  including the iMac's work arriving by iCloud. Both numbers were right. Reporting the 20% alone
  would have read as a stalled run that had lost days. → Fixed in the skill itself: the script's
  label now says "this machine processed", with a table naming `APPLY BATCH:` as the completion
  figure. Same family as last session's "0 keyword search results ≠ tagging failure": a query that
  measures something narrower than the question asked.

**Suspected my own action before checking the timestamp**
- Found the runner process gone with an Ollama `Remote end closed connection` error in the log, and
  first suspected the test request I had sent to Ollama. It was ~20 hours earlier and the runner ran
  ~16 more hours and many batches afterward; the real cause was a system reboot at 08:06:08, twelve
  seconds after the log's last write. → `sysctl -n kern.boottime` settles "did the machine restart"
  in one command and should be the first check when a long-running background job vanishes.

**Self-caught code defects (found by exercising the CLI, not by tests)**
- Put the `tag` credential pre-check *after* `new_run_directory()`, so a missing-key failure left an
  orphaned run directory behind → moved it ahead of directory creation; verified no directory is
  created on failure.
- Built the missing-key message from a provider-derived env var plus a generic fallback, which
  collapse to the same name for OpenAI: "export OPENAI_API_KEY or OPENAI_API_KEY" → deduped with
  `dict.fromkeys`. Both defects were invisible to the unit tests and surfaced only from running the
  actual command and reading its output.

**Environment / tooling**
- `sleep 45 && <checks>` was blocked by the harness → used a backgrounded `until` loop instead.
- CLAUDE.md documents `python3 -m pytest tests/`, but pytest is not installed for the active
  Python 3.14; only `python3 tests/test_phototagger.py` works. Not fixed here — the correction
  belongs on `main`, not on this feature branch.

---

## Session: 2026-07-27

**Project:** phototagger (whole-library tagging run, depot migration, app design)

### Failures

**Wrong diagnosis held for days (the expensive one)**
- Photos hangs: attributed to "sustained AppleEvent load" and built restart-between-batches
  machinery around that theory → actual cause was disk exhaustion blocking iCloud original
  downloads. Measured: 544 empty-export failures/day at 12 GB free vs 0 at 64 GB, same
  throughput. Corrected in CLAUDE.md and the check-run skill; the restart machinery remains but
  is documented as treating a symptom.
- `export_library_photo_with_warmup_retry`: a 20s "Photos is cold" retry built on that same wrong
  theory. Fired 563 times (~3 hours of pure sleeping) while the real cause went unaddressed →
  removed; a failed export already becomes a retryable record.

**Guards that did not guard**
- 20 GB disk guard implemented as a bash loop in the agent session, and documented in CLAUDE.md as
  a property of the tool → died silently with the session; run ground back down to 18 GB free with
  nothing watching. Moved into `run_library_batches.py` (`MIN_FREE_GB`).
- Hang circuit-breaker counted *all* consecutive per-item errors → 8 transient cold-start export
  failures tripped it, the runner "recovered" by restarting Photos, which recreated the cold-start,
  looping. Made the breaker timeout-signature-only.
- No-progress guard had no backoff → a batch where everything fails completes in minutes, so a
  transient iCloud throttle burned all 3 strikes almost instantly and stopped the run. All 5 sampled
  photos exported fine 3s later. Added escalating 10/20-minute backoff.

**Reported success that wasn't**
- rsync monitor polled `pgrep` for a process that had already died on an unsupported flag → printed
  "RSYNC FINISHED" for a sync that copied zero bytes. The evidence was in my own output (Seagate
  still 102 G, free space unchanged) and I did not read it. Monitors now confirm liveness first.
- macOS ships `openrsync`, not GNU rsync: `--info=progress2`, `--no-perms`, `--human-readable` all
  abort before copying. Relaunched with supported flags only.

**Environment self-inflicted**
- Exported a minimal `PATH` (`/usr/bin:/bin:…`) to work around missing tools, then launched the
  background runner from that shell → ImageMagick not found, batch failed immediately. Relaunched
  with `/opt/homebrew/bin` present.
- Same restricted PATH made `xxd`/`head`/`tail` unavailable mid-command, producing false "BROKEN"
  verdicts on a parquet integrity check. Redid the analysis in Python; the file was fine.
- An unquoted glob aborted a zsh command before it ran, leaving a variable empty, and I reported
  "NONE FOUND" as a result rather than a failed search.

**Claims made before verifying**
- Pitched a shared-library product feature ("never delete someone else's tag") that no macOS API
  can support: neither AppleScript nor PhotoKit exposes library membership, contributor, or
  keyword provenance. Retracted rather than planned around, after probing `Photos.sdef` and the
  PhotoKit headers.
- Claimed PhotoKit's local-only sized image request would solve the disk wall → measured only
  5% of photos have local pixels available, so the thesis is unproven and its plan section needs
  rewriting before anyone builds on it.
- Called the Seagate "NTFS, read-only" from a `diskutil` partition-type ID → it is exFAT and
  writable. One command (`mount`) would have settled it.
- Compared `du` sizes across APFS and exFAT and treated the difference as meaningful → exFAT's
  128 KB clusters inflate small files (102 M → 218 M). Only checksums compare across filesystems.
- Reported a keyword search returning 0 results as though it were a tagging failure → the
  AppleScript `whose keywords contains` form silently returns nothing; direct per-id reads
  confirmed the tags were present all along.

**External / credential**
- Anthropic API returned 401 repeatedly: the stored key contained an embedded space (a copy-paste
  line-wrap artifact). Then 400: `temperature` is deprecated for the model. Both would have been
  caught in seconds by a single-photo test call — the reason `test-backend` is a requirement in the
  BYO-API task.
- API key was exposed in a screenshot during debugging; advised immediate revocation. Key material
  was never handled directly, but a plaintext key file contributed to the exposure.

**Data integrity (caught, not caused)**
- A 283 MB parquet file on the Seagate had matching size *and* mtime but a different SHA-256 — its
  last ~62 KB zeroed, including the parquet footer. Corrupt since April, invisible to every
  size-based comparison. Found by `rsync --checksum`, repaired by forced re-copy. Had the local
  copy been deleted on the strength of that backup, the file would have been lost.

---
## Session: 2026-07-31 (RAW completion segment)

**Project:** phototagger

### Failures
- [runner/filter-exhaust]: exhausted --only-extensions exited 0 with status batch_complete,
  which the runner read as "keep going" — an infinite loop of no-op batches, each
  force-restarting Photos (~every 30s). Caught live at the CR2 finish line → exhaustion now
  places STOP so the runner exits; regression-tested.
- [diagnosis]: declared ~260 repeatedly-failing UUID-named CR2s "genuinely unavailable —
  will never succeed" after 8 attempts each → 258 of them subsequently succeeded on further
  blind retries (very slow iCloud materialization). The attempt-cap stays as a safety valve,
  but the confident unrecoverability claim was wrong; only 2 of 9,567 truly failed.
- [watchers]: three overlapping completion watchers accumulated across hibernation
  recoveries (each re-armed without confirming the prior one died), producing duplicate
  finish-line notifications → drained; arm-once discipline noted.
- [tooling]: ran build_index.py with /usr/bin/python3 (3.9) → SyntaxError on modern
  f-strings; the script was fine, the interpreter was wrong → use PATH python3 (homebrew).
- [environment]: two battery-death hibernations froze the entire process tree mid-run;
  the stack thawed intact both times (runner, batch, caffeinate) — logged as a success of
  the crash design, but the workload out-draining a 100% power bank is a real operational
  hazard worth remembering.

---
