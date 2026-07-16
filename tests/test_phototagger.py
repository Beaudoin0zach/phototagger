import argparse
import importlib.util
import json
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "phototagger.py"
SPEC = importlib.util.spec_from_file_location("phototagger", MODULE_PATH)
assert SPEC and SPEC.loader
phototagger = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = phototagger
SPEC.loader.exec_module(phototagger)


class KeywordTests(unittest.TestCase):
    def test_descending_library_cursor_math(self):
        self.assertEqual(phototagger.library_batch_end(77780, 25, "descending"), 77756)
        self.assertEqual(phototagger.library_next_index(77780, 25, "descending"), 77755)
        self.assertFalse(
            phototagger.library_cursor_complete(77755, 77780, "descending")
        )
        self.assertTrue(phototagger.library_cursor_complete(0, 77780, "descending"))
        self.assertEqual(
            phototagger.library_directional_progress(77755, 77780, "descending"), 25
        )

    def test_ascending_library_cursor_math(self):
        self.assertEqual(phototagger.library_batch_end(151, 25, "ascending"), 175)
        self.assertEqual(phototagger.library_next_index(151, 25, "ascending"), 176)
        self.assertFalse(phototagger.library_cursor_complete(176, 77780, "ascending"))
        self.assertTrue(phototagger.library_cursor_complete(77781, 77780, "ascending"))

    def test_normalized_tags_filters_and_splits(self):
        values = phototagger.normalized_tags(
            [
                {"label": "plant, flora", "confidence": 0.91},
                {"label": "houseplant", "confidence": 0.73},
                {"label": "leaf", "confidence": 0.2},
            ],
            confidence=0.65,
            maximum=3,
            prefix="AI: ",
        )
        self.assertEqual(values, ["AI: plant", "AI: flora", "AI: houseplant"])

    def test_normalized_tags_turns_model_underscores_into_spaces(self):
        values = phototagger.normalized_tags(
            [{"label": "decorative_plant", "confidence": 0.9}],
            confidence=0.65,
            maximum=3,
            prefix="AI: ",
        )
        self.assertEqual(values, ["AI: decorative plant"])

    def test_normalized_tags_canonicalizes_common_variants(self):
        values = phototagger.normalized_tags(
            [
                {"label": "house plant", "confidence": 1.0},
                {"label": "houseplants", "confidence": 1.0},
                {"label": "cacti", "confidence": 1.0},
                {"label": "windows", "confidence": 1.0},
            ],
            confidence=0.65,
            maximum=5,
            prefix="AI: ",
        )
        self.assertEqual(values, ["AI: houseplant", "AI: cactus", "AI: window"])

    def test_merge_preserves_existing_and_deduplicates_case(self):
        values = phototagger.merge_keywords(
            ["Family", "AI: Plant"],
            ["ai: plant", "AI: leaf"],
        )
        self.assertEqual(values, ["Family", "AI: Plant", "AI: leaf"])

    def test_rename_generated_keywords_removes_only_generated_prefixes(self):
        values = phototagger.rename_generated_keywords(
            ["Family", "AI: screenshot", "AI: unrelated", "map screenshot"],
            ["AI: screenshot", "AI: map screenshot"],
            from_prefix="AI: ",
            to_prefix="",
        )
        self.assertEqual(
            values,
            ["Family", "screenshot", "AI: unrelated", "map screenshot"],
        )

    def test_live_photo_export_selects_still_component(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            root = Path(temp)
            still = root / "IMG_1682.HEIC"
            video = root / "IMG_1682.MOV"
            still.touch()
            video.touch()
            selected = phototagger.choose_exported_image([still, video], "IMG_1682.HEIC")
            self.assertEqual(selected, still)

    def test_raw_photo_is_accepted_as_still_image(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            raw = Path(temp) / "O84A5517.CR2"
            raw.touch()
            selected = phototagger.choose_exported_image([raw], "O84A5517.CR2")
            self.assertEqual(selected, raw)

    @mock.patch.object(phototagger, "image_bytes_for_ollama", return_value=b"fake-image")
    @mock.patch("urllib.request.urlopen")
    def test_ollama_classifier_sends_image_and_reads_structured_tags(
        self, urlopen, _image_bytes
    ):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        response = Response()
        response.read = lambda: json.dumps(
            {
                "message": {
                    "content": json.dumps(
                        {
                            "tags": ["houseplant", "green leaf"],
                            "determinations": {
                                "focus": "not blurry",
                                "exposure": "acceptable",
                                "orientation": "normal",
                                "media_type": "camera photo",
                                "text_content": "no text",
                                "screenshot_subtype": "none",
                                "document_type": "none",
                                "special_content": "none",
                            },
                        }
                    )
                }
            }
        ).encode()
        urlopen.return_value = response
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            image = Path(temp) / "photo.jpg"
            image.write_bytes(b"source-image")
            values = phototagger.classify_with_ollama(
                image, model="gemma4:test", album="Plants", maximum=5
            )
        self.assertEqual(
            values["classifications"],
            [
                {"label": "houseplant", "confidence": 1.0},
                {"label": "green leaf", "confidence": 1.0},
            ],
        )
        self.assertEqual(values["determinations"]["focus"], "not blurry")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gemma4:test")
        self.assertEqual(payload["messages"][0]["images"], ["ZmFrZS1pbWFnZQ=="])

    @mock.patch.object(phototagger, "image_bytes_for_ollama", return_value=b"fake-image")
    @mock.patch("urllib.request.urlopen")
    def test_anthropic_classifier_sends_image_and_reads_tool_call(
        self, urlopen, _image_bytes
    ):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        response = Response()
        response.read = lambda: json.dumps(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "record_photo_tags",
                        "input": {
                            "tags": ["houseplant", "green leaf"],
                            "determinations": {
                                "focus": "not blurry",
                                "exposure": "acceptable",
                                "orientation": "normal",
                                "media_type": "camera photo",
                                "text_content": "no text",
                                "screenshot_subtype": "none",
                                "document_type": "none",
                                "special_content": "none",
                            },
                        },
                    }
                ]
            }
        ).encode()
        urlopen.return_value = response
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with TemporaryDirectory() as temp:
                image = Path(temp) / "photo.jpg"
                image.write_bytes(b"source-image")
                values = phototagger.classify_with_anthropic(
                    image, model="claude-sonnet-5", album="Plants", maximum=5
                )
        self.assertEqual(
            values["classifications"],
            [
                {"label": "houseplant", "confidence": 1.0},
                {"label": "green leaf", "confidence": 1.0},
            ],
        )
        self.assertEqual(values["determinations"]["focus"], "not blurry")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "claude-sonnet-5")
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "record_photo_tags"})
        self.assertEqual(
            payload["messages"][0]["content"][1]["source"]["data"], "ZmFrZS1pbWFnZQ=="
        )
        self.assertEqual(request.get_header("X-api-key"), "test-key")

    def test_anthropic_classifier_requires_api_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with TemporaryDirectory() as temp:
                image = Path(temp) / "photo.jpg"
                image.write_bytes(b"source-image")
                with self.assertRaises(RuntimeError):
                    phototagger.classify_with_anthropic(
                        image, model="claude-sonnet-5", album="Plants", maximum=5
                    )

    def test_determination_keywords_omit_routine_negative_values(self):
        values = phototagger.determination_keywords(
            {
                "focus": "not blurry",
                "exposure": "acceptable",
                "orientation": "normal",
                "media_type": "camera photo",
                "text_content": "no text",
                "screenshot_subtype": "none",
                "document_type": "none",
                "special_content": "none",
            },
            prefix="AI: ",
        )
        self.assertEqual(values, [])

    def test_determination_keywords_include_positive_flags(self):
        values = phototagger.determination_keywords(
            {
                "focus": "blurry",
                "exposure": "underexposed",
                "orientation": "sideways",
                "media_type": "screenshot",
                "text_content": "readable text",
                "screenshot_subtype": "message",
                "document_type": "identification card",
                "special_content": "social media post",
            },
            prefix="AI: ",
        )
        self.assertEqual(
            values,
            [
                "AI: blurry",
                "AI: underexposed",
                "AI: screenshot",
                "AI: message screenshot",
                "AI: identification card",
                "AI: social media post",
            ],
        )

    def test_determination_keywords_include_new_screenshot_subtype(self):
        values = phototagger.determination_keywords(
            {
                "focus": "not blurry",
                "exposure": "acceptable",
                "orientation": "normal",
                "media_type": "screenshot",
                "text_content": "readable text",
                "screenshot_subtype": "search results",
                "document_type": "none",
                "special_content": "none",
            },
            prefix="AI: ",
        )
        self.assertEqual(values, ["AI: screenshot", "AI: search results screenshot"])

    def test_refine_determinations_uses_explicit_map_tag(self):
        values = phototagger.refine_determinations(
            [{"label": "map"}, {"label": "navigation app"}],
            {
                "media_type": "screenshot",
                "screenshot_subtype": "other",
                "document_type": "none",
            },
        )
        self.assertEqual(values["screenshot_subtype"], "map")

    def test_refine_determinations_recognizes_membership_card(self):
        values = phototagger.refine_determinations(
            [{"label": "membership card"}],
            {
                "media_type": "screenshot",
                "screenshot_subtype": "other",
                "document_type": "none",
            },
        )
        self.assertEqual(values["document_type"], "identification card")


def make_item(identifier, keywords=(), source_index=None, filename=None):
    return phototagger.PhotoItem(
        identifier=identifier,
        filename=filename or f"{identifier}.jpg",
        keywords=list(keywords),
        source_index=source_index,
    )


class ResumeSafetyTests(unittest.TestCase):
    def test_pending_items_retries_error_and_verify_failed(self):
        items = [make_item("a"), make_item("b"), make_item("c"), make_item("d")]
        latest = {
            "a": {"photo_id": "a", "status": "applied"},
            "b": {"photo_id": "b", "status": "error"},
            "c": {"photo_id": "c", "status": "verify-failed"},
        }
        pending = phototagger.pending_items(items, latest)
        self.assertEqual([item.identifier for item in pending], ["b", "c", "d"])

    def test_verify_applied_batch_logs_mismatch_as_retryable(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            records = {
                "a": {
                    "photo_id": "a",
                    "filename": "a.jpg",
                    "status": "applied",
                    "keywords_before": ["Family"],
                    "keywords_after": ["Family", "plant"],
                },
                "b": {
                    "photo_id": "b",
                    "filename": "b.jpg",
                    "status": "applied",
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                },
            }
            current = [
                make_item("a", keywords=["Family", "plant"]),  # matches
                make_item("b", keywords=[]),  # write did not persist
            ]
            failures = phototagger.verify_applied_batch(run_dir, current, records)
            self.assertEqual(failures, 1)
            appended = phototagger.read_jsonl(run_dir / "results.jsonl")
            self.assertEqual(len(appended), 1)
            self.assertEqual(appended[0]["photo_id"], "b")
            self.assertEqual(appended[0]["status"], "verify-failed")
            self.assertEqual(appended[0]["keywords_expected"], ["plant"])
            self.assertEqual(appended[0]["keywords_found"], [])

    def test_verify_applied_batch_counts_missing_photo(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            records = {
                "gone": {
                    "photo_id": "gone",
                    "filename": "gone.jpg",
                    "status": "applied",
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                }
            }
            failures = phototagger.verify_applied_batch(run_dir, [], records)
            self.assertEqual(failures, 1)

    def test_verify_applied_batch_passes_clean_batch(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            records = {
                "a": {
                    "photo_id": "a",
                    "status": "applied",
                    "keywords_after": ["plant"],
                },
                "b": {"photo_id": "b", "status": "review"},
            }
            current = [make_item("a", keywords=["plant"]), make_item("b")]
            failures = phototagger.verify_applied_batch(run_dir, current, records)
            self.assertEqual(failures, 0)
            self.assertFalse((run_dir / "results.jsonl").exists())

    def _library_run_dir(self, temp, *, next_index=1, total_count=1, batch_size=25):
        run_dir = Path(temp) / "run"
        run_dir.mkdir()
        metadata = {
            "album": "Photos Library",
            "apply": True,
            "backend": "ollama",
            "batch_size": batch_size,
            "confidence": 0.65,
            "keep_exports": False,
            "limit": 0,
            "max_tags": 5,
            "model": "gemma4:test",
            "next_index": next_index,
            "order": "ascending",
            "prefix": "",
            "source": "library",
            "status": "batch_errors",
            "total_count": total_count,
            "version": 4,
        }
        phototagger.save_run(run_dir, metadata)
        return run_dir

    def _resume_args(self, run_dir, batch_size=None):
        return argparse.Namespace(resume=str(run_dir), batch_size=batch_size)

    def test_resume_with_empty_pending_does_not_advance_past_failed_verify(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            # Photo recorded as applied, but Photos never persisted the write.
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                    "source": "library",
                    "source_index": 1,
                },
            )
            inventory = [make_item("p1", keywords=[], source_index=1)]
            with mock.patch.object(phototagger, "library_count", return_value=1), \
                    mock.patch.object(
                        phototagger,
                        "inventory_library_batch_chunked",
                        return_value=inventory,
                    ):
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 1)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "batch_errors")
            self.assertEqual(metadata["next_index"], 1)  # cursor did NOT advance
            latest = phototagger.latest_records_by_photo(
                phototagger.read_jsonl(run_dir / "results.jsonl")
            )
            self.assertEqual(latest["p1"]["status"], "verify-failed")

    def test_resume_with_empty_pending_advances_after_clean_verify(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                    "source": "library",
                    "source_index": 1,
                },
            )
            inventory = [make_item("p1", keywords=["plant"], source_index=1)]
            with mock.patch.object(phototagger, "library_count", return_value=1), \
                    mock.patch.object(
                        phototagger,
                        "inventory_library_batch_chunked",
                        return_value=inventory,
                    ):
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 0)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(metadata["next_index"], 2)
            self.assertTrue(metadata["last_batch_verified"])

    def test_resume_batch_size_override_is_persisted(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp, batch_size=2000)
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                    "source": "library",
                    "source_index": 1,
                },
            )
            inventory = [make_item("p1", keywords=["plant"], source_index=1)]
            with mock.patch.object(phototagger, "library_count", return_value=1), \
                    mock.patch.object(
                        phototagger,
                        "inventory_library_batch_chunked",
                        return_value=inventory,
                    ) as chunked:
                phototagger.tag_command(self._resume_args(run_dir, batch_size=200))
            self.assertEqual(chunked.call_args_list[0].args[1], 200)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["batch_size"], 200)

    def test_verify_phase_infrastructure_failure_saves_batch_errors_instead_of_crashing(
        self,
    ):
        # Regression test: a real production run hit "Connection is invalid
        # (-609)" from the post-write verification re-inventory call. That
        # call wasn't wrapped in error handling, so the whole invocation
        # crashed before save_run — losing this attempt's bookkeeping even
        # though every per-item result was already durable in results.jsonl.
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp, next_index=5, total_count=5)
            pending_item = make_item("p1", keywords=[], source_index=5)
            with mock.patch.object(
                phototagger, "library_count", return_value=5
            ), mock.patch.object(
                phototagger,
                "inventory_library_batch_chunked",
                side_effect=[
                    [pending_item],
                    RuntimeError("Photos automation failed: Connection is invalid. (-609)"),
                ],
            ), mock.patch.object(
                phototagger, "export_library_photo", return_value=Path("/tmp/fake.jpg")
            ), mock.patch.object(
                phototagger,
                "classify",
                return_value={
                    "classifications": [{"label": "plant", "confidence": 1.0}],
                    "determinations": {},
                },
            ), mock.patch.object(phototagger, "set_library_keywords"):
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 1)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "batch_errors")
            self.assertEqual(metadata["next_index"], 5)  # cursor did NOT advance
            # The write itself is durable even though verification couldn't confirm it.
            latest = phototagger.latest_records_by_photo(
                phototagger.read_jsonl(run_dir / "results.jsonl")
            )
            self.assertEqual(latest["p1"]["status"], "applied")


class ChunkedInventoryTests(unittest.TestCase):
    def _fake_batch(self, calls):
        def fake(start, count, order="ascending", timeout=1800):
            calls.append((start, count, order))
            return [make_item(f"{order[:3]}-{start}-{offset}") for offset in range(count)]

        return fake

    def test_ascending_chunk_boundaries(self):
        calls = []
        with mock.patch.object(
            phototagger, "inventory_library_batch", side_effect=self._fake_batch(calls)
        ):
            items = phototagger.inventory_library_batch_chunked(1, 250, "ascending")
        self.assertEqual(calls, [(1, 100, "ascending"), (101, 100, "ascending"), (201, 50, "ascending")])
        self.assertEqual(len(items), 250)

    def test_descending_chunk_boundaries_clamp_at_one(self):
        calls = []
        with mock.patch.object(
            phototagger, "inventory_library_batch", side_effect=self._fake_batch(calls)
        ):
            items = phototagger.inventory_library_batch_chunked(250, 300, "descending")
        self.assertEqual(
            calls,
            [(250, 100, "descending"), (150, 100, "descending"), (50, 50, "descending")],
        )
        self.assertEqual(len(items), 250)

    def test_short_chunk_stops_at_library_boundary(self):
        def fake(start, count, order="ascending", timeout=1800):
            return [make_item(f"i{start + offset}") for offset in range(min(count, 30))]

        with mock.patch.object(phototagger, "inventory_library_batch", side_effect=fake):
            items = phototagger.inventory_library_batch_chunked(1, 250, "ascending")
        self.assertEqual(len(items), 30)


class AppleScriptRetryTests(unittest.TestCase):
    def _timeout_error(self):
        return subprocess.CalledProcessError(
            1, ["osascript"], output="", stderr="Photos got an error: AppleEvent timed out. (-1712)"
        )

    def test_retries_1712_timeouts_then_succeeds(self):
        success = subprocess.CompletedProcess(["osascript"], 0, stdout="ok\n", stderr="")
        with mock.patch.object(
            phototagger,
            "run_command",
            side_effect=[self._timeout_error(), self._timeout_error(), success],
        ) as run_command, mock.patch.object(phototagger.time, "sleep") as sleep:
            result = phototagger.run_applescript("library_count.applescript")
        self.assertEqual(result, "ok")
        self.assertEqual(run_command.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])

    def test_non_timeout_error_raises_immediately(self):
        error = subprocess.CalledProcessError(
            1, ["osascript"], output="", stderr="Expected one media item with id: X"
        )
        with mock.patch.object(phototagger, "run_command", side_effect=[error]) as run_command:
            with self.assertRaises(RuntimeError):
                phototagger.run_applescript("set_keywords.applescript")
        self.assertEqual(run_command.call_count, 1)

    def test_retries_subprocess_hang_then_succeeds(self):
        # osascript itself never returns (Photos hung or was slow to wake up) —
        # a real subprocess.TimeoutExpired, distinct from the -1712 case above
        # where Photos reports the AppleEvent timeout and osascript returns promptly.
        success = subprocess.CompletedProcess(["osascript"], 0, stdout="ok\n", stderr="")
        hang = subprocess.TimeoutExpired(cmd=["osascript"], timeout=600)
        with mock.patch.object(
            phototagger, "run_command", side_effect=[hang, hang, success]
        ) as run_command, mock.patch.object(phototagger.time, "sleep") as sleep:
            result = phototagger.run_applescript("inventory_library_batch.applescript")
        self.assertEqual(result, "ok")
        self.assertEqual(run_command.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])

    def test_subprocess_hang_raises_after_exhausting_retries(self):
        hang = subprocess.TimeoutExpired(cmd=["osascript"], timeout=600)
        with mock.patch.object(
            phototagger, "run_command", side_effect=[hang, hang, hang, hang]
        ) as run_command, mock.patch.object(phototagger.time, "sleep"):
            with self.assertRaises(RuntimeError):
                phototagger.run_applescript("inventory_library_batch.applescript", retries=3)
        self.assertEqual(run_command.call_count, 4)


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_earliest_snapshot_and_verifies(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            phototagger.save_run(
                run_dir, {"album": "Plants", "apply": True, "source": "album"}
            )
            # Two applied records for one photo: retry after a verify failure.
            # Only the FIRST snapshot holds the true pre-run keywords.
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "keywords_before": ["Family"],
                    "keywords_after": ["Family", "plant"],
                },
            )
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "keywords_before": ["Family", "plant"],
                    "keywords_after": ["Family", "plant"],
                },
            )
            restored_calls = []

            def fake_set_keywords(album, photo_id, keywords):
                restored_calls.append((album, photo_id, keywords))

            with mock.patch.object(
                phototagger, "set_keywords", side_effect=fake_set_keywords
            ), mock.patch.object(
                phototagger,
                "inventory_album",
                return_value=[make_item("p1", keywords=["Family"])],
            ):
                result = phototagger.rollback_command(
                    argparse.Namespace(run=str(run_dir))
                )
            self.assertEqual(result, 0)
            self.assertEqual(restored_calls, [("Plants", "p1", ["Family"])])
            audits = list(run_dir.glob("rollback-*.jsonl"))
            self.assertEqual(len(audits), 1)
            audit_records = phototagger.read_jsonl(audits[0])
            self.assertEqual(audit_records[-1]["status"], "restored")

    def test_rollback_flags_unverified_restore(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            phototagger.save_run(
                run_dir, {"album": "Plants", "apply": True, "source": "album"}
            )
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "keywords_before": ["Family"],
                    "keywords_after": ["Family", "plant"],
                },
            )
            with mock.patch.object(phototagger, "set_keywords"), mock.patch.object(
                phototagger,
                "inventory_album",
                return_value=[make_item("p1", keywords=["Family", "plant"])],
            ):
                result = phototagger.rollback_command(
                    argparse.Namespace(run=str(run_dir))
                )
            self.assertEqual(result, 1)
            audits = list(run_dir.glob("rollback-*.jsonl"))
            audit_records = phototagger.read_jsonl(audits[0])
            self.assertEqual(audit_records[-1]["status"], "verify-failed")


class SanitizationTests(unittest.TestCase):
    def test_merge_keywords_strips_separator_control_characters(self):
        values = phototagger.merge_keywords([], ["plant\x1fpot", "a\x1db", "c\x1ed"])
        self.assertEqual(values, ["plant pot", "a b", "c d"])

    def test_normalized_tags_strip_separator_control_characters(self):
        values = phototagger.normalized_tags(
            [{"label": "green\x1eleaf", "confidence": 0.9}],
            confidence=0.65,
            maximum=3,
            prefix="",
        )
        self.assertEqual(values, ["green leaf"])


if __name__ == "__main__":
    unittest.main()
