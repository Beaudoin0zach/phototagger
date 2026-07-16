import importlib.util
import json
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
