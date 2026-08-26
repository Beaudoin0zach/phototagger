"""Tests for the unattended supervisor.

This script runs from launchd every five minutes with the power to kill
processes and delete STOP files, and it had no tests at all — every fix in it
was verified by hand-run scripts that would not catch a regression. The cases
here are the ones that actually went wrong in production or were found by
audit, not a general sweep.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "health_check.py"
SPEC = importlib.util.spec_from_file_location("health_check", MODULE_PATH)
assert SPEC and SPEC.loader
health_check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = health_check
SPEC.loader.exec_module(health_check)


DISK_PARK = "2026-08-26T09:00:00 only 18.9 GB free (< 20.0 GB); STOP placed to protect Photos.\n"


class RunDir:
    """A throwaway run directory with the files the supervisor reads."""

    def __init__(self, tmp: str, log_lines: str = DISK_PARK, stop: bool = True):
        self.path = Path(tmp)
        (self.path / "run.json").write_text(json.dumps({"status": "batch_complete"}))
        (self.path / "results.jsonl").write_text('{"photo_id":"a","status":"applied"}\n')
        (self.path / "runner.log").write_text(log_lines)
        if stop:
            (self.path / "STOP").touch()
            self.stop_older_than_log()

    def stop_older_than_log(self):
        """The runner touches STOP and then logs, so its STOP is the older file."""
        log_stat = (self.path / "runner.log").stat()
        os.utime(self.path / "STOP", (log_stat.st_atime, log_stat.st_mtime - 60))


class DiskParkTests(unittest.TestCase):
    def test_clears_the_runners_own_park_once_space_returns(self):
        with TemporaryDirectory() as tmp:
            run = RunDir(tmp)
            with mock.patch.object(health_check, "free_gb", return_value=60.0):
                self.assertTrue(health_check.disk_park_cleared(run.path))

    def test_holds_the_park_while_space_is_still_short(self):
        with TemporaryDirectory() as tmp:
            run = RunDir(tmp)
            with mock.patch.object(health_check, "free_gb", return_value=21.0):
                # 21 GB clears the floor but not the resume margin.
                self.assertFalse(health_check.disk_park_cleared(run.path))

    def test_never_clears_a_stop_touched_after_the_runner_logged(self):
        """A STOP newer than runner.log belongs to a human, not the disk guard."""
        with TemporaryDirectory() as tmp:
            run = RunDir(tmp)
            time.sleep(0.01)
            (run.path / "STOP").touch()
            with mock.patch.object(health_check, "free_gb", return_value=60.0):
                self.assertFalse(health_check.disk_park_cleared(run.path))

    def test_ignores_a_stale_disk_park_that_is_not_the_last_event(self):
        """Filter exhaustion after a disk park must not inherit its authority."""
        with TemporaryDirectory() as tmp:
            run = RunDir(tmp, DISK_PARK + "2026-08-26T09:05:00 Filter exhausted; STOP placed\n")
            with mock.patch.object(health_check, "free_gb", return_value=60.0):
                self.assertFalse(health_check.disk_park_cleared(run.path))

    def test_ignores_a_target_stop(self):
        with TemporaryDirectory() as tmp:
            run = RunDir(tmp, DISK_PARK + "2026-08-26T09:05:00 reached target: 500 photos done\n")
            with mock.patch.object(health_check, "free_gb", return_value=60.0):
                self.assertFalse(health_check.disk_park_cleared(run.path))

    def test_no_stop_file_means_nothing_to_clear(self):
        with TemporaryDirectory() as tmp:
            run = RunDir(tmp, stop=False)
            with mock.patch.object(health_check, "free_gb", return_value=60.0):
                self.assertFalse(health_check.disk_park_cleared(run.path))


class WorkInProgressTests(unittest.TestCase):
    def _pgrep(self, stdout, returncode=0):
        return mock.patch.object(
            health_check.subprocess, "run",
            return_value=mock.Mock(stdout=stdout, returncode=returncode),
        )

    def test_a_tagger_child_counts_as_busy(self):
        """Killing a runner orphans its tagger; the orphan is still doing the work."""
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            line = f"123 python phototagger.py tag --resume {run.resolve()} --batch-size 150\n"
            with self._pgrep(line):
                self.assertTrue(health_check.work_in_progress(run))

    def test_another_runs_processes_do_not_count(self):
        with TemporaryDirectory() as tmp:
            run = Path(tmp)
            with self._pgrep("123 python run_library_batches.py /some/other/run\n"):
                self.assertFalse(health_check.work_in_progress(run))

    def test_pgrep_failure_is_treated_as_busy(self):
        """Ambiguity must never cause a second runner to be started."""
        with TemporaryDirectory() as tmp:
            with self._pgrep("", returncode=2):
                self.assertTrue(health_check.work_in_progress(Path(tmp)))

    def test_no_match_means_idle(self):
        with TemporaryDirectory() as tmp:
            with self._pgrep("", returncode=1):
                self.assertFalse(health_check.work_in_progress(Path(tmp)))


class StallThresholdTests(unittest.TestCase):
    def test_threshold_clears_the_longest_legal_export_silence(self):
        """Exports allow timeout=1800 with up to three retries (~121 minutes)."""
        longest_legal_silence = (1800 * 4 + 50) / 60
        self.assertGreater(health_check.STALL_MINUTES, longest_legal_silence)


if __name__ == "__main__":
    unittest.main()
