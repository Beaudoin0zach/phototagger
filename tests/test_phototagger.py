import argparse
import importlib.util
import json
import os
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

    def test_device_keyword_from_exif_tags_only_iphones(self):
        # Real iPhone: Apple make + iPhone model, any format (HEIC or old JPG).
        self.assertEqual(
            phototagger.device_keyword_from_exif("Apple", "iPhone 6s", prefix=""),
            "iPhone",
        )
        self.assertEqual(
            phototagger.device_keyword_from_exif(" apple ", "iPhone 13 Pro", prefix=""),
            "iPhone",
        )
        # iPad, Mac, and non-Apple cameras must not be tagged as iPhone.
        self.assertIsNone(
            phototagger.device_keyword_from_exif("Apple", "iPad Pro", prefix="")
        )
        self.assertIsNone(
            phototagger.device_keyword_from_exif("Canon", "Canon EOS 5D", prefix="")
        )
        # Missing EXIF (a converted or stripped file) yields no tag.
        self.assertIsNone(phototagger.device_keyword_from_exif("", "", prefix=""))
        # The keyword prefix is honored, like descriptive/determination tags.
        self.assertEqual(
            phototagger.device_keyword_from_exif("Apple", "iPhone SE", prefix="AI: "),
            "AI: iPhone",
        )


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

    def test_pending_items_retries_write_pending_and_skips_not_found(self):
        items = [make_item("a"), make_item("b")]
        latest = {
            "a": {"photo_id": "a", "status": "write-pending"},
            "b": {"photo_id": "b", "status": "not-found"},
        }
        pending = phototagger.pending_items(items, latest)
        self.assertEqual([item.identifier for item in pending], ["a"])

    def test_verify_applied_batch_logs_missing_generated_tags_as_retryable(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            records = {
                "a": {
                    "photo_id": "a",
                    "filename": "a.jpg",
                    "status": "applied",
                    "generated_keywords": ["plant"],
                    "keywords_after": ["Family", "plant"],
                },
                "b": {
                    "photo_id": "b",
                    "filename": "b.jpg",
                    "status": "applied",
                    "generated_keywords": ["plant"],
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

    def test_verify_applied_batch_tolerates_concurrent_manual_keywords(self):
        # A user adding/removing their own keywords in Photos.app mid-run must
        # never count as a verification failure — only OUR generated tags
        # going missing does.
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            records = {
                "a": {
                    "photo_id": "a",
                    "status": "applied",
                    "generated_keywords": ["plant"],
                    "keywords_after": ["old-manual", "plant"],
                }
            }
            current = [
                # user removed "old-manual" and added "vacation" since the write
                make_item("a", keywords=["plant", "vacation"])
            ]
            failures = phototagger.verify_applied_batch(run_dir, current, records)
            self.assertEqual(failures, 0)
            self.assertFalse((run_dir / "results.jsonl").exists())

    def test_verify_applied_batch_counts_missing_photo(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            records = {
                "gone": {
                    "photo_id": "gone",
                    "filename": "gone.jpg",
                    "status": "applied",
                    "generated_keywords": ["plant"],
                    "keywords_after": ["plant"],
                }
            }
            failures = phototagger.verify_applied_batch(run_dir, [], records)
            self.assertEqual(failures, 1)

    def _library_run_dir(
        self,
        temp,
        *,
        batch_size=25,
        status="batch_complete",
        manifest_ids=("p1",),
        extra_metadata=None,
    ):
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
            "order": "ascending",
            "prefix": "",
            "source": "library",
            "status": status,
            "version": 5,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        phototagger.save_run(run_dir, metadata)
        for photo_id in manifest_ids:
            phototagger.append_jsonl(
                run_dir / "manifest.jsonl",
                {
                    "capture_date": "",
                    "filename": f"{photo_id}.jpg",
                    "photo_id": photo_id,
                    "swept_index": 1,
                },
            )
        return run_dir

    def _resume_args(self, run_dir, batch_size=None):
        return argparse.Namespace(resume=str(run_dir), batch_size=batch_size)

    def _classify_result(self):
        return {
            "classifications": [{"label": "plant", "confidence": 1.0}],
            "determinations": {},
        }

    def test_write_ahead_journal_precedes_the_photos_write(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            with mock.patch.object(
                phototagger, "library_count", return_value=1
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                side_effect=[
                    make_item("p1", keywords=["Family"]),  # batch read
                    make_item("p1", keywords=["Family", "plant"]),  # verify read
                ],
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                return_value=Path("/tmp/fake.jpg"),
            ), mock.patch.object(
                phototagger, "classify", return_value=self._classify_result()
            ), mock.patch.object(
                phototagger,
                "sync_library_keywords",
                return_value=(["Family"], ["Family", "plant"]),
            ) as sync:
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 0)
            sync.assert_called_once_with("p1", ["plant"])
            records = phototagger.read_jsonl(run_dir / "results.jsonl")
            statuses = [record["status"] for record in records]
            # The journal entry lands durably BEFORE the mutation's record.
            self.assertEqual(statuses, ["write-pending", "applied"])
            # keywords_before comes from the sync's atomic read, not the
            # inventory-time snapshot.
            self.assertEqual(records[1]["keywords_before"], ["Family"])
            self.assertEqual(records[1]["keywords_after"], ["Family", "plant"])
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "complete")
            self.assertTrue(metadata["last_batch_verified"])

    def test_interrupted_write_pending_record_is_retried_on_resume(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            # Crash happened between the journal entry and the applied record;
            # the (idempotent) write must be retried on resume.
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "generated_keywords": ["plant"],
                    "status": "write-pending",
                    "source": "library",
                },
            )
            with mock.patch.object(
                phototagger, "library_count", return_value=1
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                side_effect=[
                    make_item("p1", keywords=["Family", "plant"]),  # batch read
                    make_item("p1", keywords=["Family", "plant"]),  # verify read
                ],
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                return_value=Path("/tmp/fake.jpg"),
            ), mock.patch.object(
                phototagger, "classify", return_value=self._classify_result()
            ), mock.patch.object(
                phototagger,
                "sync_library_keywords",
                return_value=(["Family", "plant"], ["Family", "plant"]),
            ) as sync:
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 0)
            sync.assert_called_once()  # retried, and safely idempotent
            latest = phototagger.latest_records_by_photo(
                phototagger.read_jsonl(run_dir / "results.jsonl")
            )
            self.assertEqual(latest["p1"]["status"], "applied")

    def test_errored_batch_is_reverified_on_resume(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(
                temp,
                status="batch_errors",
                extra_metadata={"last_batch_photo_ids": ["p1"]},
            )
            # Recorded applied, but Photos does not actually hold the tag.
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "generated_keywords": ["plant"],
                    "keywords_after": ["plant"],
                    "status": "applied",
                    "source": "library",
                },
            )
            with mock.patch.object(
                phototagger, "library_count", return_value=1
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                side_effect=[
                    make_item("p1", keywords=[]),  # re-verify read: tag missing
                    make_item("p1", keywords=[]),  # batch read for reprocess
                    make_item("p1", keywords=["plant"]),  # verify read after re-apply
                ],
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                return_value=Path("/tmp/fake.jpg"),
            ), mock.patch.object(
                phototagger, "classify", return_value=self._classify_result()
            ), mock.patch.object(
                phototagger,
                "sync_library_keywords",
                return_value=([], ["plant"]),
            ) as sync:
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 0)
            sync.assert_called_once()  # the unconfirmed write was redone
            statuses = [
                record["status"]
                for record in phototagger.read_jsonl(run_dir / "results.jsonl")
            ]
            self.assertEqual(
                statuses, ["applied", "verify-failed", "write-pending", "applied"]
            )
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "complete")

    def test_resume_batch_size_override_limits_batch_and_persists(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(
                temp, batch_size=2000, manifest_ids=("p1", "p2", "p3")
            )
            seen: set = set()

            def read_by_id(pid):
                # First read (batch) has no tag yet; verify read sees it applied.
                if pid in seen:
                    return make_item(pid, keywords=["plant"])
                seen.add(pid)
                return make_item(pid, keywords=[])

            with mock.patch.object(
                phototagger, "library_count", return_value=3
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                side_effect=read_by_id,
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                return_value=Path("/tmp/fake.jpg"),
            ), mock.patch.object(
                phototagger, "classify", return_value=self._classify_result()
            ), mock.patch.object(
                phototagger,
                "sync_library_keywords",
                return_value=([], ["plant"]),
            ) as sync:
                result = phototagger.tag_command(self._resume_args(run_dir, batch_size=2))
            self.assertEqual(result, 0)
            self.assertEqual(sync.call_count, 2)  # only the batch, not all 3
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["batch_size"], 2)
            self.assertEqual(metadata["status"], "batch_complete")  # p3 remains

    def test_old_positional_run_requires_migration(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            phototagger.save_run(
                run_dir,
                {
                    "album": "Photos Library",
                    "apply": True,
                    "batch_size": 25,
                    "confidence": 0.65,
                    "keep_exports": False,
                    "limit": 0,
                    "max_tags": 5,
                    "model": "gemma4:test",
                    "next_index": 77759,
                    "order": "descending",
                    "prefix": "",
                    "source": "library",
                    "status": "batch_complete",
                    "total_count": 77784,
                    "version": 4,
                },
            )
            with self.assertRaisesRegex(RuntimeError, "migrate_run_to_manifest"):
                phototagger.tag_command(self._resume_args(run_dir))

    def test_deleted_photo_is_recorded_as_terminal_not_found(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            with mock.patch.object(
                phototagger, "library_count", return_value=0
            ), mock.patch.object(
                phototagger, "library_item_by_id", return_value=None
            ):
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 0)  # a vanished photo is not an error
            latest = phototagger.latest_records_by_photo(
                phototagger.read_jsonl(run_dir / "results.jsonl")
            )
            self.assertEqual(latest["p1"]["status"], "not-found")
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "complete")

    def test_non_timeout_errors_do_not_trip_the_circuit_breaker(self):
        # Regression: 8 adjacent photos failing export with "0 candidate still
        # images" (a transient cold-start condition, not a hang) tripped the
        # breaker, which made the runner force-restart Photos in a loop —
        # guaranteeing the next batch also started cold on the same photos.
        with TemporaryDirectory() as temp:
            manifest_ids = tuple(f"p{i}" for i in range(1, 13))
            run_dir = self._library_run_dir(temp, manifest_ids=manifest_ids)
            failing = {f"p{i}" for i in range(1, 11)}  # 10 consecutive failures

            def fake_export(item, destination):
                if item.identifier in failing:
                    raise RuntimeError(
                        f"Photos export produced 0 candidate still images for {item.filename}"
                    )
                return Path("/tmp/fake.jpg")

            reads: dict = {}

            def read_by_id(pid):
                # batch read first, verify read second
                if pid in reads:
                    return make_item(pid, keywords=["plant"])
                reads[pid] = True
                return make_item(pid, keywords=[])

            with mock.patch.object(
                phototagger, "library_count", return_value=12
            ), mock.patch.object(
                phototagger, "library_item_by_id", side_effect=read_by_id
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                side_effect=fake_export,
            ), mock.patch.object(
                phototagger, "classify", return_value=self._classify_result()
            ), mock.patch.object(
                phototagger, "sync_library_keywords", return_value=([], ["plant"])
            ) as sync:
                result = phototagger.tag_command(self._resume_args(run_dir))
            # Plain errors, not a hang: exit 1, and the batch continued PAST
            # the failing cluster to apply the photos behind it.
            self.assertEqual(result, 1)
            self.assertEqual(sync.call_count, 2)  # p11, p12 still processed
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "batch_errors")
            self.assertEqual(metadata["applied_this_invocation"], 2)
            self.assertEqual(metadata["errors_this_invocation"], 10)

    def test_failed_export_becomes_a_retryable_record(self):
        # No inline retry: a failed export is recorded as a retryable "error"
        # and re-enters pending on the next batch. This is what makes the
        # removed 20s warm-up retry redundant.
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            with mock.patch.object(
                phototagger, "library_count", return_value=1
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                return_value=make_item("p1", keywords=[]),
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                side_effect=RuntimeError(
                    "Photos export produced 0 candidate still images for p1.jpg"
                ),
            ) as export:
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 1)
            self.assertEqual(export.call_count, 1)  # attempted once, no inline retry
            latest = phototagger.latest_records_by_photo(
                phototagger.read_jsonl(run_dir / "results.jsonl")
            )
            self.assertEqual(latest["p1"]["status"], "error")
            # "error" is in RETRY_STATUSES, so the photo is still pending.
            still_pending = phototagger.pending_items(
                [make_item("p1")], latest
            )
            self.assertEqual([i.identifier for i in still_pending], ["p1"])

    def test_consecutive_errors_trip_the_circuit_breaker(self):
        # A hung Photos fails every AppleEvent; the batch must stop early with
        # durable error records and saved metadata instead of grinding through
        # hundreds of doomed items (or crashing on an unguarded read).
        with TemporaryDirectory() as temp:
            manifest_ids = tuple(f"p{i}" for i in range(1, 21))
            run_dir = self._library_run_dir(temp, manifest_ids=manifest_ids)
            with mock.patch.object(
                phototagger, "library_count", return_value=20
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                side_effect=RuntimeError("Photos automation timed out after 120s"),
            ) as read_by_id:
                result = phototagger.tag_command(self._resume_args(run_dir))
            # Distinct exit code tells the runner to restart Photos and resume.
            self.assertEqual(result, phototagger.EXIT_PHOTOS_HUNG)
            limit = phototagger.CONSECUTIVE_ERROR_LIMIT
            self.assertEqual(read_by_id.call_count, limit)  # stopped early
            records = phototagger.read_jsonl(run_dir / "results.jsonl")
            self.assertEqual(len(records), limit)
            self.assertTrue(all(r["status"] == "error" for r in records))
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "batch_errors")

    def test_verify_phase_infrastructure_failure_saves_batch_errors_instead_of_crashing(
        self,
    ):
        # Regression: a real production run died on "Connection is invalid
        # (-609)" raised by the post-write verification pass, crashing before
        # save_run. Verification infrastructure failures must degrade to
        # batch_errors with metadata still persisted.
        with TemporaryDirectory() as temp:
            run_dir = self._library_run_dir(temp)
            with mock.patch.object(
                phototagger, "library_count", return_value=1
            ), mock.patch.object(
                phototagger,
                "library_item_by_id",
                side_effect=[
                    make_item("p1", keywords=[]),  # batch read
                    RuntimeError(
                        "Photos automation failed: Connection is invalid. (-609)"
                    ),  # verify read
                ],
            ), mock.patch.object(
                phototagger,
                "export_library_photo_by_id",
                return_value=Path("/tmp/fake.jpg"),
            ), mock.patch.object(
                phototagger, "classify", return_value=self._classify_result()
            ), mock.patch.object(
                phototagger,
                "sync_library_keywords",
                return_value=([], ["plant"]),
            ):
                result = phototagger.tag_command(self._resume_args(run_dir))
            self.assertEqual(result, 1)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "batch_errors")
            self.assertEqual(metadata["last_batch_photo_ids"], ["p1"])
            # The write itself is durable even though verification couldn't confirm it.
            latest = phototagger.latest_records_by_photo(
                phototagger.read_jsonl(run_dir / "results.jsonl")
            )
            self.assertEqual(latest["p1"]["status"], "applied")


class ExtensionFilterTests(unittest.TestCase):
    def test_filter_selects_only_named_types(self):
        items = [
            make_item("a", filename="a.CR2"),
            make_item("b", filename="b.jpg"),
            make_item("c", filename="c.cr2"),   # case-insensitive
            make_item("d", filename="d.HEIC"),
        ]
        got = phototagger.pending_items(items, {}, {"cr2"})
        self.assertEqual([i.identifier for i in got], ["a", "c"])

    def test_no_filter_returns_everything_pending(self):
        items = [make_item("a", filename="a.CR2"), make_item("b", filename="b.jpg")]
        self.assertEqual(len(phototagger.pending_items(items, {}, None)), 2)
        self.assertEqual(len(phototagger.pending_items(items, {}, set())), 2)

    def test_filter_does_not_mark_excluded_photos_done(self):
        # Excluded photos must stay pending so dropping the filter resumes them.
        items = [make_item("a", filename="a.CR2"), make_item("b", filename="b.jpg")]
        latest = {"a": {"photo_id": "a", "status": "applied"}}
        # with the filter, only CR2 considered — and 'a' is already done
        self.assertEqual(phototagger.pending_items(items, latest, {"cr2"}), [])
        # without it, the jpg is still waiting
        self.assertEqual(
            [i.identifier for i in phototagger.pending_items(items, latest, None)], ["b"]
        )

    def test_photos_exceeding_error_attempts_leave_pending(self):
        # Observed live: 260 photos with unavailable originals each failed 8
        # times and would have retried forever, starving the tail of the run.
        items = [make_item("a"), make_item("b")]
        latest = {
            "a": {"photo_id": "a", "status": "error"},
            "b": {"photo_id": "b", "status": "error"},
        }
        attempts = {"a": phototagger.MAX_ERROR_ATTEMPTS, "b": 1}
        got = phototagger.pending_items(items, latest, None, attempts)
        self.assertEqual([i.identifier for i in got], ["b"])

    def test_matches_extensions_handles_dots_and_case(self):
        self.assertTrue(phototagger.matches_extensions("x.CR2", {"cr2"}))
        self.assertTrue(phototagger.matches_extensions("x.cr2", {"cr2"}))
        self.assertFalse(phototagger.matches_extensions("x.jpg", {"cr2"}))
        self.assertTrue(phototagger.matches_extensions("noext", None))


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
    def _library_run(self, temp):
        run_dir = Path(temp) / "run"
        run_dir.mkdir()
        phototagger.save_run(
            run_dir, {"album": "Photos Library", "apply": True, "source": "library"}
        )
        return run_dir

    def test_generated_tags_union_covers_applied_and_write_pending(self):
        records = [
            {"photo_id": "p1", "status": "applied", "generated_keywords": ["plant"],
             "keywords_before": [], "keywords_after": ["plant"]},
            # A crashed write attempt still counts — the write may have landed.
            {"photo_id": "p1", "status": "write-pending", "generated_keywords": ["Leaf"],
             "keywords_before": ["plant"]},
            {"photo_id": "p1", "status": "applied", "generated_keywords": ["leaf", "pot"],
             "keywords_before": ["plant", "leaf"], "keywords_after": ["plant", "leaf", "pot"]},
            {"photo_id": "p2", "status": "review", "generated_keywords": ["ignored"]},
            {"photo_id": "p3", "status": "applied", "generated_keywords": [],
             "keywords_before": [], "keywords_after": []},
        ]
        union = phototagger.generated_tags_by_photo(records)
        self.assertEqual(set(union), {"p1"})
        self.assertEqual(union["p1"]["generated_keywords"], ["plant", "Leaf", "pot"])

    def test_rollback_never_removes_pre_existing_user_keywords(self):
        # P1 regression: the model generated "bird" on a photo the user had
        # ALREADY hand-tagged "bird". The write was a no-op for that tag, so
        # rollback must not remove it — only "goose", which this run added.
        records = [
            {"photo_id": "p1", "status": "applied",
             "generated_keywords": ["bird", "goose"],
             "keywords_before": ["bird", "Vacation"],
             "keywords_after": ["bird", "Vacation", "goose"]},
        ]
        union = phototagger.generated_tags_by_photo(records)
        self.assertEqual(union["p1"]["generated_keywords"], ["goose"])

    def test_rollback_retry_chain_keeps_our_tag_removable(self):
        # Attempt 1 added "goose"; the retry's before-list already contains it
        # (we put it there). The retry contributes nothing, but attempt 1's
        # record keeps the tag removable — it must not be shielded.
        records = [
            {"photo_id": "p1", "status": "applied",
             "generated_keywords": ["goose"],
             "keywords_before": [], "keywords_after": ["goose"]},
            {"photo_id": "p1", "status": "applied",
             "generated_keywords": ["goose"],
             "keywords_before": ["goose"], "keywords_after": ["goose"]},
        ]
        union = phototagger.generated_tags_by_photo(records)
        self.assertEqual(union["p1"]["generated_keywords"], ["goose"])

    def test_rollback_write_pending_excludes_pre_existing(self):
        records = [
            {"photo_id": "p1", "status": "write-pending",
             "generated_keywords": ["bird", "goose"],
             "keywords_before": ["bird"]},
        ]
        union = phototagger.generated_tags_by_photo(records)
        self.assertEqual(union["p1"]["generated_keywords"], ["goose"])

    def test_rollback_surgically_removes_only_generated_tags(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run(temp)
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "generated_keywords": ["plant", "leaf"],
                    "keywords_before": ["Family", "vacation"],
                    "keywords_after": ["Family", "vacation", "plant", "leaf"],
                },
            )
            removed_calls = []

            def fake_remove(photo_id, targets):
                removed_calls.append((photo_id, targets))
                # Photos had a user-added keyword too; it survives untouched.
                return (["Family", "plant", "leaf", "vacation"], ["Family", "vacation"])

            with mock.patch.object(
                phototagger, "remove_library_keywords", side_effect=fake_remove
            ):
                result = phototagger.rollback_command(argparse.Namespace(run=str(run_dir)))
            self.assertEqual(result, 0)
            self.assertEqual(removed_calls, [("p1", ["plant", "leaf"])])
            audits = list(run_dir.glob("rollback-*.jsonl"))
            self.assertEqual(len(audits), 1)
            audit_records = phototagger.read_jsonl(audits[0])
            self.assertEqual(audit_records[-1]["status"], "removed")
            self.assertEqual(audit_records[-1]["keywords_after"], ["Family", "vacation"])

    def test_rollback_flags_surviving_generated_tag(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run(temp)
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "generated_keywords": ["plant"],
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                },
            )
            with mock.patch.object(
                phototagger,
                "remove_library_keywords",
                return_value=(["plant"], ["plant"]),  # removal did not take
            ):
                result = phototagger.rollback_command(argparse.Namespace(run=str(run_dir)))
            self.assertEqual(result, 1)
            audits = list(run_dir.glob("rollback-*.jsonl"))
            audit_records = phototagger.read_jsonl(audits[0])
            self.assertEqual(audit_records[-1]["status"], "error")

    def test_rollback_records_vanished_photo_without_failing(self):
        with TemporaryDirectory() as temp:
            run_dir = self._library_run(temp)
            phototagger.append_jsonl(
                run_dir / "results.jsonl",
                {
                    "photo_id": "p1",
                    "filename": "p1.jpg",
                    "status": "applied",
                    "generated_keywords": ["plant"],
                    "keywords_before": [],
                    "keywords_after": ["plant"],
                },
            )
            with mock.patch.object(
                phototagger,
                "remove_library_keywords",
                side_effect=phototagger.PhotoNotFoundError("gone"),
            ):
                result = phototagger.rollback_command(argparse.Namespace(run=str(run_dir)))
            self.assertEqual(result, 0)
            audits = list(run_dir.glob("rollback-*.jsonl"))
            audit_records = phototagger.read_jsonl(audits[0])
            self.assertEqual(audit_records[-1]["status"], "not-found")


class TornTailTests(unittest.TestCase):
    def test_torn_final_record_is_dropped_with_warning(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "results.jsonl"
            phototagger.append_jsonl(path, {"photo_id": "p1", "status": "applied"})
            phototagger.append_jsonl(path, {"photo_id": "p2", "status": "applied"})
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"photo_id": "p3", "sta')  # crash mid-append
            records = phototagger.read_jsonl(path)
            self.assertEqual([r["photo_id"] for r in records], ["p1", "p2"])

    def test_torn_middle_record_still_raises(self):
        # Mid-file corruption is not a crash signature; silently skipping it
        # would hide real damage.
        with TemporaryDirectory() as temp:
            path = Path(temp) / "results.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                handle.write('{"photo_id": "p1"\n')  # torn, but NOT last
                handle.write('{"photo_id": "p2", "status": "applied"}\n')
            with self.assertRaises(ValueError):
                phototagger.read_jsonl(path)


class FilteredCompletionTests(unittest.TestCase):
    # P1 regression: exhausting an extension filter must never mark the whole
    # manifest complete while unfiltered photos still lack records.
    def _run_dir(self, temp, manifest):
        run_dir = Path(temp) / "run"
        run_dir.mkdir()
        phototagger.save_run(run_dir, {
            "album": "Photos Library", "apply": True, "backend": "ollama",
            "batch_size": 25, "confidence": 0.65, "keep_exports": False,
            "limit": 0, "max_tags": 5, "model": "gemma4:test",
            "order": "ascending", "prefix": "", "source": "library",
            "status": "batch_complete", "version": 5,
        })
        for photo_id, filename in manifest:
            phototagger.append_jsonl(run_dir / "manifest.jsonl",
                {"photo_id": photo_id, "filename": filename, "capture_date": ""})
        return run_dir

    def test_exhausted_filter_does_not_complete_the_run(self):
        with TemporaryDirectory() as temp:
            run_dir = self._run_dir(temp, [("a", "a.CR2"), ("b", "b.jpg")])
            # the only CR2 is already done; b.jpg has no record at all
            phototagger.append_jsonl(run_dir / "results.jsonl", {
                "photo_id": "a", "filename": "a.CR2", "status": "applied",
                "generated_keywords": ["x"], "keywords_before": [],
                "keywords_after": ["x"]})
            with mock.patch.object(phototagger, "library_count", return_value=2):
                result = phototagger.tag_command(argparse.Namespace(
                    resume=str(run_dir), batch_size=None, only_extensions="CR2"))
            self.assertEqual(result, 0)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "batch_complete")  # NOT complete
            # and it parks itself so a guarded runner exits instead of looping
            self.assertTrue((run_dir / "STOP").exists())

    def test_truly_finished_run_still_completes(self):
        with TemporaryDirectory() as temp:
            run_dir = self._run_dir(temp, [("a", "a.CR2")])
            phototagger.append_jsonl(run_dir / "results.jsonl", {
                "photo_id": "a", "filename": "a.CR2", "status": "applied",
                "generated_keywords": ["x"], "keywords_before": [],
                "keywords_after": ["x"]})
            with mock.patch.object(phototagger, "library_count", return_value=1):
                result = phototagger.tag_command(argparse.Namespace(
                    resume=str(run_dir), batch_size=None, only_extensions="CR2"))
            self.assertEqual(result, 0)
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["status"], "complete")


class PrivacyAndLockTests(unittest.TestCase):
    def test_new_run_directory_is_private(self):
        with TemporaryDirectory() as temp:
            run_dir = phototagger.new_run_directory(Path(temp), "My Album")
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)

    def test_append_jsonl_creates_private_files(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "results.jsonl"
            phototagger.append_jsonl(path, {"photo_id": "p1"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_run_creates_private_run_json(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            phototagger.save_run(run_dir, {"status": "running"})
            self.assertEqual((run_dir / "run.json").stat().st_mode & 0o777, 0o600)

    def test_run_lock_retrofits_dir_privacy(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "runs" / "20990101-test"
            run_dir.mkdir(parents=True)
            os.chmod(run_dir, 0o755)  # pre-fix world-readable dir
            with phototagger.run_lock(run_dir):
                pass
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)

    def test_runner_photos_lifecycle_lock_is_exclusive(self):
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "rlb_test", MODULE_PATH.parent / "scripts" / "run_library_batches.py"
        )
        rlb = ilu.module_from_spec(spec)
        sys.modules["rlb_test"] = rlb
        spec.loader.exec_module(rlb)
        import fcntl as f
        with TemporaryDirectory() as temp:
            lock_path = Path(temp) / ".photos-lifecycle.lock"
            h1 = lock_path.open("w")
            f.flock(h1, f.LOCK_EX | f.LOCK_NB)
            h2 = lock_path.open("w")
            with self.assertRaises(BlockingIOError):
                f.flock(h2, f.LOCK_EX | f.LOCK_NB)
            h1.close(); h2.close()


class InfrastructureTests(unittest.TestCase):
    def test_parse_before_after(self):
        before, after = phototagger.parse_before_after("a\x1fb\x1ea\x1fb\x1fc")
        self.assertEqual(before, ["a", "b"])
        self.assertEqual(after, ["a", "b", "c"])
        before, after = phototagger.parse_before_after("\x1ec")
        self.assertEqual(before, [])
        self.assertEqual(after, ["c"])

    def test_sync_library_keywords_maps_missing_id_to_not_found(self):
        with mock.patch.object(
            phototagger,
            "run_applescript",
            side_effect=RuntimeError(
                "Photos automation failed: Can’t get media item id \"X\"."
            ),
        ):
            with self.assertRaises(phototagger.PhotoNotFoundError):
                phototagger.sync_library_keywords("X", ["plant"])

    def test_run_lock_contention_fails_fast(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            with phototagger.run_lock(run_dir):
                with self.assertRaisesRegex(RuntimeError, "already operating"):
                    with phototagger.run_lock(run_dir):
                        pass

    def test_save_run_is_atomic_and_leaves_no_temp_file(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            phototagger.save_run(run_dir, {"status": "running"})
            phototagger.save_run(run_dir, {"status": "complete"})
            self.assertEqual(phototagger.load_run(run_dir)["status"], "complete")
            self.assertFalse((run_dir / "run.json.tmp").exists())

    def test_load_manifest_deduplicates_preserving_order(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            for photo_id in ("a", "b", "a", "c"):
                phototagger.append_jsonl(
                    run_dir / "manifest.jsonl",
                    {"photo_id": photo_id, "filename": f"{photo_id}.jpg"},
                )
            entries = phototagger.load_manifest(run_dir)
            self.assertEqual([entry["photo_id"] for entry in entries], ["a", "b", "c"])

    def test_build_manifest_bulk_orders_and_replaces_stale_partial(self):
        FS, RS = "\x1e", "\x1d"
        bulk = RS.join(
            [
                f"id1{FS}a.jpg{FS}Jan 1",
                f"id2{FS}b.jpg{FS}Jan 2",
                f"id3{FS}c.jpg{FS}Jan 3",
            ]
        )
        with TemporaryDirectory() as temp:
            run_dir = Path(temp)
            # A stale partial manifest from an interrupted build must be
            # replaced wholesale, not appended to.
            phototagger.append_jsonl(run_dir / "manifest.jsonl", {"photo_id": "stale"})
            with mock.patch.object(
                phototagger, "run_applescript", return_value=bulk
            ):
                total = phototagger.build_manifest(run_dir, "descending")
            self.assertEqual(total, 3)
            entries = phototagger.load_manifest(run_dir)
            self.assertEqual(
                [entry["photo_id"] for entry in entries], ["id3", "id2", "id1"]
            )
            self.assertEqual(entries[0]["filename"], "c.jpg")
            self.assertFalse((run_dir / "manifest.jsonl.tmp").exists())
            with mock.patch.object(
                phototagger, "run_applescript", return_value=bulk
            ):
                phototagger.build_manifest(run_dir, "ascending")
            entries = phototagger.load_manifest(run_dir)
            self.assertEqual(
                [entry["photo_id"] for entry in entries], ["id1", "id2", "id3"]
            )

    def test_resolve_model_defaults_and_validation(self):
        self.assertEqual(phototagger.resolve_model("ollama", None), "gemma4:e4b-it-qat")
        self.assertEqual(phototagger.resolve_model("ollama", "custom"), "custom")
        self.assertEqual(
            phototagger.resolve_model("anthropic", "claude-sonnet-5"), "claude-sonnet-5"
        )
        with self.assertRaisesRegex(RuntimeError, "requires an explicit --model"):
            phototagger.resolve_model("anthropic", None)

    def test_rename_prefix_rejects_library_runs(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            phototagger.save_run(
                run_dir,
                {"album": "Photos Library", "apply": True, "source": "library"},
            )
            with self.assertRaisesRegex(RuntimeError, "only supports album runs"):
                phototagger.rename_prefix_command(
                    argparse.Namespace(run=str(run_dir), from_prefix="AI: ", to_prefix="")
                )

    def test_set_library_order_reverses_manifest(self):
        with TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"
            run_dir.mkdir()
            phototagger.save_run(
                run_dir,
                {
                    "album": "Photos Library",
                    "apply": True,
                    "order": "ascending",
                    "source": "library",
                },
            )
            for photo_id in ("a", "b", "c"):
                phototagger.append_jsonl(
                    run_dir / "manifest.jsonl", {"photo_id": photo_id}
                )
            result = phototagger.set_library_order_command(
                argparse.Namespace(run=str(run_dir), order="descending")
            )
            self.assertEqual(result, 0)
            entries = phototagger.load_manifest(run_dir)
            self.assertEqual([entry["photo_id"] for entry in entries], ["c", "b", "a"])
            metadata = phototagger.load_run(run_dir)
            self.assertEqual(metadata["order"], "descending")


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
