#!/usr/bin/env python3
"""Safely classify Photos albums or the whole library and merge optional keywords."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
BUILD_SCRIPT = ROOT / "scripts" / "build.sh"
CLASSIFIER = ROOT / ".build" / "phototagger-classify"
APPLEScript_DIR = ROOT / "applescript"
FIELD_SEPARATOR = "\x1e"
ROW_SEPARATOR = "\x1d"
KEYWORD_SEPARATOR = "\x1f"
RAW_EXTENSIONS = {
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".nef",
    ".orf",
    ".raf",
    ".rw2",
}
IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
} | RAW_EXTENSIONS
# Statuses that must be retried on resume; anything else counts as completed.
RETRY_STATUSES = {"error", "verify-failed"}
TAG_ALIASES = {
    "cacti": "cactus",
    "house plant": "houseplant",
    "house plants": "houseplant",
    "houseplants": "houseplant",
    "indoor plants": "houseplant",
    "potted plants": "potted plant",
    "succulents": "succulent",
    "windows": "window",
}


@dataclass(frozen=True)
class PhotoItem:
    identifier: str
    filename: str
    keywords: list[str]
    source_index: int | None = None
    capture_date: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(command: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def run_applescript(
    name: str,
    *arguments: str,
    timeout: int = 600,
    retries: int = 3,
) -> str:
    script = APPLEScript_DIR / name
    for attempt in range(retries + 1):
        try:
            result = run_command(["osascript", str(script), *arguments], timeout=timeout)
            return result.stdout.rstrip("\n")
        except subprocess.TimeoutExpired as error:
            # osascript never returned at all (Photos hung or was slow to wake
            # up) — distinct from the -1712 case below, where Photos itself
            # reports the AppleEvent timeout and returns promptly.
            if attempt >= retries:
                raise RuntimeError(
                    f"Photos automation timed out after {timeout}s running {name}"
                ) from error
            delay = (5, 15, 30)[attempt]
            print(
                f"Photos did not respond within {timeout}s; retrying {name} in {delay}s "
                f"({attempt + 1}/{retries})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or str(error)).strip()
            # (-1712): AppleEvent timed out. (-609): Photos' connection was
            # invalidated (e.g. it was busy or momentarily unresponsive).
            # Both are transient automation hiccups worth a short backoff retry;
            # anything else (wrong id, missing album, etc.) is a real error.
            retryable = "(-1712)" in detail or "(-609)" in detail
            if not retryable or attempt >= retries:
                raise RuntimeError(f"Photos automation failed: {detail}") from error
            delay = (5, 15, 30)[attempt]
            print(
                f"Photos automation hiccup; retrying {name} in {delay}s "
                f"({attempt + 1}/{retries})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("Photos automation retry loop ended unexpectedly")


def list_albums() -> list[str]:
    output = run_applescript("list_albums.applescript")
    return sorted({line.strip() for line in output.splitlines() if line.strip()}, key=str.casefold)


def inventory_album(album: str) -> list[PhotoItem]:
    output = run_applescript("inventory_album.applescript", album)
    if not output:
        return []
    items: list[PhotoItem] = []
    for row in output.split(ROW_SEPARATOR):
        fields = row.split(FIELD_SEPARATOR)
        if len(fields) != 3:
            raise RuntimeError("Photos returned an inventory row in an unexpected format")
        identifier, filename, keyword_text = fields
        keywords = keyword_text.split(KEYWORD_SEPARATOR) if keyword_text else []
        items.append(PhotoItem(identifier=identifier, filename=filename, keywords=keywords))
    return items


def library_count() -> int:
    return int(run_applescript("library_count.applescript"))


def inventory_library_batch(
    start: int,
    count: int,
    order: str = "ascending",
    timeout: int = 1800,
) -> list[PhotoItem]:
    if order not in {"ascending", "descending"}:
        raise RuntimeError("library order must be ascending or descending")
    output = run_applescript(
        "inventory_library_batch.applescript",
        str(start),
        str(count),
        order,
        timeout=timeout,
    )
    if not output:
        return []
    items: list[PhotoItem] = []
    for row in output.split(ROW_SEPARATOR):
        fields = row.split(FIELD_SEPARATOR)
        if len(fields) != 5:
            raise RuntimeError("Photos returned a library inventory row in an unexpected format")
        identifier, filename, keyword_text, source_index, capture_date = fields
        keywords = keyword_text.split(KEYWORD_SEPARATOR) if keyword_text else []
        items.append(
            PhotoItem(
                identifier=identifier,
                filename=filename,
                keywords=keywords,
                source_index=int(source_index),
                capture_date=capture_date,
            )
        )
    return items


def inventory_library_batch_chunked(
    start: int,
    count: int,
    order: str = "ascending",
    chunk_size: int = 100,
) -> list[PhotoItem]:
    """Read a batch as small AppleEvents so one -1712 timeout only retries a chunk."""
    if order not in {"ascending", "descending"}:
        raise RuntimeError("library order must be ascending or descending")
    if chunk_size < 1:
        raise RuntimeError("chunk size must be at least 1")
    items: list[PhotoItem] = []
    remaining = count
    chunk_start = start
    while remaining > 0:
        if order == "descending":
            if chunk_start < 1:
                break
            chunk_count = min(chunk_size, remaining, chunk_start)
        else:
            chunk_count = min(chunk_size, remaining)
        chunk = inventory_library_batch(chunk_start, chunk_count, order, timeout=600)
        items.extend(chunk)
        if len(chunk) < chunk_count:
            break  # reached the library boundary
        remaining -= chunk_count
        chunk_start = library_next_index(chunk_start, chunk_count, order)
    return items


def library_batch_end(start: int, count: int, order: str) -> int:
    if order == "descending":
        return start - count + 1
    return start + count - 1


def library_next_index(start: int, count: int, order: str) -> int:
    if order == "descending":
        return start - count
    return start + count


def library_cursor_complete(next_index: int, total_count: int, order: str) -> bool:
    if order == "descending":
        return next_index < 1
    return next_index > total_count


def library_directional_progress(next_index: int, total_count: int, order: str) -> int:
    if order == "descending":
        return total_count - max(next_index, 0)
    return min(next_index - 1, total_count)


def library_batch_review_path(
    run_dir: Path,
    start: int,
    end: int,
    order: str,
) -> Path:
    if order == "descending":
        return run_dir / "batches" / f"descending-{end:06d}-{start:06d}.csv"
    return run_dir / "batches" / f"{start:06d}-{end:06d}.csv"


def ensure_classifier() -> None:
    source = ROOT / "Sources" / "PhotoClassifier.swift"
    if CLASSIFIER.exists() and CLASSIFIER.stat().st_mtime >= source.stat().st_mtime:
        return
    try:
        run_command([str(BUILD_SCRIPT)], timeout=300)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise RuntimeError(f"Could not build the local Vision classifier: {detail}") from error


def classify_with_apple_vision(image: Path, maximum: int = 30) -> list[dict[str, object]]:
    ensure_classifier()
    result = run_command([str(CLASSIFIER), str(image), str(maximum)], timeout=300)
    payload = json.loads(result.stdout)
    return payload["classifications"]


def build_vision_prompt(maximum: int, album: str) -> str:
    return f"""Create searchable tags for this personal photo.

Return {maximum} or fewer short, concrete tags. Prefer visible objects, scene,
setting, activity, colors, and broad categories. Use lower-case singular nouns or
short noun phrases. Do not identify people, infer sensitive traits, guess an exact
location, or add subjective/emotional judgments. The Photos album is named
{album!r}; treat that only as weak context and never force a tag unsupported by
the image. Do not include an AI prefix.

Also make controlled determinations about quality, media type, text, and special
content.
For focus, choose "not blurry" when the primary subject is reasonably clear and
"blurry" when motion or missed focus materially obscures it. For exposure, choose
"acceptable" when the main subject retains useful light and shadow detail,
"underexposed" when important detail is lost to darkness, and "overexposed" when
important detail is lost to brightness. For media type, choose "camera photo" for
an ordinary photographed scene, "screenshot" when the image is a screen capture,
"scan" for a digitized paper item, and "graphic" for an illustration or designed
graphic. Use "uncertain" only for genuine ambiguity, not as a default. Mark text
as readable only when actual words are clearly visible at this resolution; choose
"no text" for tiny incidental markings or when no letterforms are confidently
visible. Use "unreadable text" only when text is clearly a central subject but is
illegible. Never transcribe text. When media type is "screenshot", always choose
the closest screenshot subtype and use "other" only when none of the named types
fit; never choose "none" for a screenshot. When media type is not "screenshot",
choose screenshot subtype "none". Only choose a document type when a document is
the main subject; otherwise choose "none". An
identification card means a government, school, workplace, or membership identity
document; never read or return its personal information. A social media post means
the image visibly contains a social-platform post or its interface, not merely a
photo that could be posted.
Return only the requested JSON."""


def build_tag_schema(maximum: int) -> dict[str, object]:
    """The tags+determinations JSON schema shared by every structured-output backend."""
    return {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": maximum,
            },
            "determinations": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "enum": ["blurry", "not blurry", "uncertain"],
                    },
                    "exposure": {
                        "type": "string",
                        "enum": ["underexposed", "acceptable", "overexposed", "uncertain"],
                    },
                    "orientation": {
                        "type": "string",
                        "enum": ["normal", "sideways", "upside down", "uncertain"],
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["camera photo", "screenshot", "scan", "graphic", "uncertain"],
                    },
                    "text_content": {
                        "type": "string",
                        "enum": ["no text", "readable text", "unreadable text", "uncertain"],
                    },
                    "screenshot_subtype": {
                        "type": "string",
                        "enum": [
                            "message",
                            "web page",
                            "map",
                            "email",
                            "meme",
                            "social media",
                            "search results",
                            "app screen",
                            "data table",
                            "error message",
                            "other",
                            "none",
                            "uncertain",
                        ],
                    },
                    "document_type": {
                        "type": "string",
                        "enum": [
                            "identification card",
                            "receipt",
                            "business card",
                            "ticket",
                            "form",
                            "handwritten note",
                            "payment card",
                            "other document",
                            "none",
                            "uncertain",
                        ],
                    },
                    "special_content": {
                        "type": "string",
                        "enum": [
                            "social media post",
                            "identification card",
                            "receipt",
                            "other document",
                            "none",
                            "uncertain",
                        ],
                    },
                },
                "required": [
                    "focus",
                    "exposure",
                    "orientation",
                    "media_type",
                    "text_content",
                    "screenshot_subtype",
                    "document_type",
                    "special_content",
                ],
                "additionalProperties": False,
            },
        },
        "required": ["tags", "determinations"],
        "additionalProperties": False,
    }


def shape_vision_result(decoded: dict[str, object], maximum: int, *, source: str) -> dict[str, object]:
    """Turn a decoded {tags, determinations} payload into our standard analysis shape."""
    tags = decoded.get("tags", [])
    if not isinstance(tags, list):
        raise RuntimeError(f"{source} returned an invalid tags payload")
    determinations = decoded.get("determinations", {})
    if not isinstance(determinations, dict):
        raise RuntimeError(f"{source} returned an invalid determinations payload")
    return {
        "classifications": [
            {"label": str(tag), "confidence": 1.0}
            for tag in tags[:maximum]
            if str(tag).strip()
        ],
        "determinations": {
            "focus": str(determinations.get("focus", "uncertain")),
            "exposure": str(determinations.get("exposure", "uncertain")),
            "orientation": str(determinations.get("orientation", "uncertain")),
            "media_type": str(determinations.get("media_type", "uncertain")),
            "text_content": str(determinations.get("text_content", "uncertain")),
            "screenshot_subtype": str(
                determinations.get("screenshot_subtype", "uncertain")
            ),
            "document_type": str(determinations.get("document_type", "uncertain")),
            "special_content": str(determinations.get("special_content", "uncertain")),
        },
    }


def classify_with_ollama(
    image: Path,
    *,
    model: str,
    album: str,
    maximum: int,
    endpoint: str = "http://127.0.0.1:11434/api/chat",
) -> dict[str, object]:
    prompt = build_vision_prompt(maximum, album)
    schema = build_tag_schema(maximum)
    image_bytes = image_bytes_for_ollama(image)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image_bytes).decode("ascii")],
            }
        ],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Ollama request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        detail = getattr(error, "reason", error)
        raise RuntimeError(f"Ollama request failed: {detail}") from error
    content = result.get("message", {}).get("content", "")
    decoded = json.loads(content)
    return shape_vision_result(decoded, maximum, source="Ollama")


ANTHROPIC_TOOL_NAME = "record_photo_tags"


def classify_with_anthropic(
    image: Path,
    *,
    model: str,
    album: str,
    maximum: int,
    endpoint: str = "https://api.anthropic.com/v1/messages",
) -> dict[str, object]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set; export it before using --backend anthropic"
        )
    prompt = build_vision_prompt(maximum, album)
    schema = build_tag_schema(maximum)
    image_bytes = image_bytes_for_ollama(image)
    payload = {
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                ],
            }
        ],
        "tools": [
            {
                "name": ANTHROPIC_TOOL_NAME,
                "description": (
                    "Record searchable tags and controlled quality/content "
                    "determinations for a personal photo."
                ),
                "input_schema": schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": ANTHROPIC_TOOL_NAME},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Anthropic request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        detail = getattr(error, "reason", error)
        raise RuntimeError(f"Anthropic request failed: {detail}") from error
    tool_use = next(
        (
            block
            for block in result.get("content", [])
            if block.get("type") == "tool_use" and block.get("name") == ANTHROPIC_TOOL_NAME
        ),
        None,
    )
    if tool_use is None:
        raise RuntimeError("Anthropic response did not include the expected tool call")
    decoded = tool_use.get("input", {})
    if not isinstance(decoded, dict):
        raise RuntimeError("Anthropic returned an invalid tool input payload")
    return shape_vision_result(decoded, maximum, source="Anthropic")


def image_bytes_for_ollama(image: Path) -> bytes:
    """Decode, auto-orient, and resize through ImageMagick for consistent pixels."""
    with tempfile.TemporaryDirectory(prefix="phototagger-ollama-") as temp:
        source_image = image
        if image.suffix.casefold() in RAW_EXTENSIONS:
            try:
                run_command(
                    [
                        "qlmanage",
                        "-t",
                        "-s",
                        "1536",
                        "-o",
                        temp,
                        str(image),
                    ],
                    timeout=300,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as error:
                detail = getattr(error, "stderr", "") or str(error)
                raise RuntimeError(
                    "Could not create a macOS RAW preview for Ollama: " + detail
                ) from error
            raw_preview = Path(temp) / f"{image.name}.png"
            if not raw_preview.exists():
                raise RuntimeError("macOS did not produce the expected RAW preview")
            source_image = raw_preview
        compatible_image = Path(temp) / "image.jpg"
        try:
            run_command(
                [
                    "magick",
                    str(source_image),
                    "-auto-orient",
                    "-resize",
                    "1536x1536>",
                    "-quality",
                    "90",
                    str(compatible_image),
                ],
                timeout=300,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            detail = getattr(error, "stderr", "") or str(error)
            raise RuntimeError(
                "Could not decode image for Ollama with ImageMagick: " + detail
            ) from error
        if not compatible_image.exists() or compatible_image.stat().st_size == 0:
            raise RuntimeError("ImageMagick produced an empty Ollama input image")
        return compatible_image.read_bytes()


def classify(
    image: Path,
    *,
    backend: str,
    model: str,
    album: str,
    maximum: int,
) -> dict[str, object]:
    if backend == "ollama":
        return classify_with_ollama(image, model=model, album=album, maximum=maximum)
    if backend == "anthropic":
        return classify_with_anthropic(image, model=model, album=album, maximum=maximum)
    return {
        "classifications": classify_with_apple_vision(image),
        "determinations": {
            "focus": "uncertain",
            "exposure": "uncertain",
            "orientation": "uncertain",
            "media_type": "uncertain",
            "text_content": "uncertain",
            "screenshot_subtype": "uncertain",
            "document_type": "uncertain",
            "special_content": "uncertain",
        },
    }


def normalized_tags(
    classifications: Iterable[dict[str, object]],
    *,
    confidence: float,
    maximum: int,
    prefix: str,
) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for observation in classifications:
        score = float(observation["confidence"])
        if score < confidence:
            continue
        for raw_label in str(observation["label"]).split(","):
            # Strip the record/field/keyword separator control characters so a
            # hostile or malformed model label can never corrupt AppleScript I/O.
            raw_label = re.sub(r"[\x1d-\x1f]", " ", raw_label)
            label = re.sub(r"\s+", " ", raw_label.replace("_", " ")).strip(" ._-")
            if len(label) < 2:
                continue
            label = TAG_ALIASES.get(label.casefold(), label)
            value = f"{prefix}{label}"
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            tags.append(value)
            if len(tags) >= maximum:
                return tags
    return tags


def merge_keywords(existing: Iterable[str], generated: Iterable[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for keyword in [*existing, *generated]:
        # Defensively remove the AppleScript I/O separator control characters;
        # every keyword-write path funnels through this merge.
        value = re.sub(r"[\x1d-\x1f]", " ", str(keyword)).strip()
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        merged.append(value)
    return merged


def rename_generated_keywords(
    current: Iterable[str],
    generated: Iterable[str],
    *,
    from_prefix: str,
    to_prefix: str,
) -> list[str]:
    generated_keys = {str(value).casefold() for value in generated}
    prefix_key = from_prefix.casefold()
    renamed: list[str] = []
    for keyword in current:
        value = str(keyword)
        if value.casefold() in generated_keys and value.casefold().startswith(prefix_key):
            value = f"{to_prefix}{value[len(from_prefix):]}"
        renamed.append(value)
    return merge_keywords([], renamed)


def determination_keywords(determinations: dict[str, object], *, prefix: str) -> list[str]:
    """Return only useful positive flags; routine negatives remain review metadata."""
    values: list[str] = []
    focus = str(determinations.get("focus", "uncertain"))
    exposure = str(determinations.get("exposure", "uncertain"))
    orientation = str(determinations.get("orientation", "uncertain"))
    media_type = str(determinations.get("media_type", "uncertain"))
    text_content = str(determinations.get("text_content", "uncertain"))
    screenshot_subtype = str(determinations.get("screenshot_subtype", "uncertain"))
    document_type = str(determinations.get("document_type", "uncertain"))
    special_content = str(determinations.get("special_content", "uncertain"))
    if focus == "blurry":
        values.append(f"{prefix}blurry")
    if exposure in {"underexposed", "overexposed"}:
        values.append(f"{prefix}{exposure}")
    media_keywords = {
        "screenshot": "screenshot",
        "scan": "scanned document",
        "graphic": "graphic",
    }
    if media_type in media_keywords:
        values.append(f"{prefix}{media_keywords[media_type]}")
    if text_content == "unreadable text":
        values.append(f"{prefix}{text_content}")
    screenshot_keywords = {
        "message": "message screenshot",
        "web page": "web page screenshot",
        "map": "map screenshot",
        "email": "email screenshot",
        "meme": "meme",
        "social media": "social media post",
        "search results": "search results screenshot",
        "app screen": "app screenshot",
        "data table": "data table screenshot",
        "error message": "error message screenshot",
    }
    if screenshot_subtype in screenshot_keywords:
        values.append(f"{prefix}{screenshot_keywords[screenshot_subtype]}")
    if document_type not in {"none", "uncertain"}:
        values.append(f"{prefix}{document_type}")
    if special_content not in {"none", "uncertain"}:
        values.append(f"{prefix}{special_content}")
    return merge_keywords([], values)


def refine_determinations(
    classifications: Iterable[dict[str, object]],
    determinations: dict[str, object],
) -> dict[str, object]:
    """Use explicit descriptive labels to resolve overly broad structured choices."""
    refined = dict(determinations)
    labels = " | ".join(str(item.get("label", "")) for item in classifications).casefold()
    if str(refined.get("media_type")) == "screenshot" and str(
        refined.get("screenshot_subtype")
    ) in {"other", "none", "uncertain"}:
        subtype_rules = [
            (("error message", "error screen"), "error message"),
            (("spreadsheet", "data table"), "data table"),
            (("search results", "search result"), "search results"),
            (("map", "navigation"), "map"),
            (("chat", "text message", "message thread"), "message"),
            (("email",), "email"),
            (("social media", "social-media"), "social media"),
            (("news article", "web page", "website"), "web page"),
            (("app screen", "mobile app", "mobile interface"), "app screen"),
        ]
        for needles, subtype in subtype_rules:
            if any(needle in labels for needle in needles):
                refined["screenshot_subtype"] = subtype
                break
        else:
            refined["screenshot_subtype"] = "other"
    if str(refined.get("document_type")) in {"none", "uncertain"} and any(
        needle in labels
        for needle in ("identification card", "identity document", "membership card")
    ):
        refined["document_type"] = "identification card"
    return refined


def choose_exported_image(paths: Iterable[Path], expected_filename: str) -> Path:
    images = [path for path in paths if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS]
    if len(images) == 1:
        return images[0]
    exact = [path for path in images if path.name.casefold() == expected_filename.casefold()]
    if len(exact) == 1:
        return exact[0]
    expected_stem = Path(expected_filename).stem.casefold()
    matching_stem = [path for path in images if path.stem.casefold() == expected_stem]
    if len(matching_stem) == 1:
        return matching_stem[0]
    raise RuntimeError(
        f"Photos export produced {len(images)} candidate still images for {expected_filename}"
    )


def export_photo(album: str, item: PhotoItem, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in destination.iterdir()}
    run_applescript("export_item.applescript", album, item.identifier, str(destination), timeout=1800)
    created = [path for path in destination.iterdir() if path.name not in before and path.is_file()]
    try:
        return choose_exported_image(created, item.filename)
    except RuntimeError:
        pass
    if not created:
        candidates = [path for path in destination.iterdir() if path.is_file()]
        return choose_exported_image(candidates, item.filename)
    raise RuntimeError(f"Photos export did not yield an unambiguous still image for {item.filename}")


def export_library_photo(item: PhotoItem, destination: Path) -> Path:
    if item.source_index is None:
        raise RuntimeError("Library photo is missing its source index")
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in destination.iterdir()}
    run_applescript(
        "export_library_item.applescript",
        str(item.source_index),
        item.identifier,
        str(destination),
        timeout=1800,
    )
    created = [path for path in destination.iterdir() if path.name not in before and path.is_file()]
    try:
        return choose_exported_image(created, item.filename)
    except RuntimeError:
        pass
    if not created:
        candidates = [path for path in destination.iterdir() if path.is_file()]
        return choose_exported_image(candidates, item.filename)
    raise RuntimeError(f"Photos export did not yield an unambiguous still image for {item.filename}")


def set_keywords(album: str, identifier: str, keywords: list[str]) -> None:
    run_applescript(
        "set_keywords.applescript",
        album,
        identifier,
        KEYWORD_SEPARATOR.join(keywords),
    )


def set_library_keywords(item: PhotoItem, keywords: list[str]) -> None:
    if item.source_index is None:
        raise RuntimeError("Library photo is missing its source index")
    run_applescript(
        "set_library_keywords.applescript",
        str(item.source_index),
        item.identifier,
        KEYWORD_SEPARATOR.join(keywords),
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_review_file(path: Path, records: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    records = list(records)
    fields = [
        "status",
        "source_index",
        "capture_date",
        "filename",
        "photo_id",
        "generated_keywords",
        "keywords_before",
        "keywords_after",
        "top_predictions",
        "focus",
        "exposure",
        "orientation",
        "media_type",
        "text_content",
        "screenshot_subtype",
        "document_type",
        "special_content",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "status": record.get("status", ""),
                    "source_index": record.get("source_index", ""),
                    "capture_date": record.get("capture_date", ""),
                    "filename": record.get("filename", ""),
                    "photo_id": record.get("photo_id", ""),
                    "generated_keywords": " | ".join(record.get("generated_keywords", [])),
                    "keywords_before": " | ".join(record.get("keywords_before", [])),
                    "keywords_after": " | ".join(record.get("keywords_after", [])),
                    "top_predictions": " | ".join(
                        f"{item['label']} ({float(item['confidence']):.3f})"
                        for item in record.get("classifications", [])[:10]
                    ),
                    "focus": record.get("determinations", {}).get("focus", ""),
                    "exposure": record.get("determinations", {}).get("exposure", ""),
                    "orientation": record.get("determinations", {}).get("orientation", ""),
                    "media_type": record.get("determinations", {}).get("media_type", ""),
                    "text_content": record.get("determinations", {}).get("text_content", ""),
                    "screenshot_subtype": record.get("determinations", {}).get(
                        "screenshot_subtype", ""
                    ),
                    "document_type": record.get("determinations", {}).get(
                        "document_type", ""
                    ),
                    "special_content": record.get("determinations", {}).get(
                        "special_content", ""
                    ),
                    "error": record.get("error", ""),
                }
            )


def latest_records_by_photo(
    records: Iterable[dict[str, object]],
) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for record in records:
        latest[str(record.get("photo_id", ""))] = record
    return latest


def pending_items(
    items: Iterable[PhotoItem],
    latest_by_photo: dict[str, dict[str, object]],
) -> list[PhotoItem]:
    completed = {
        photo_id
        for photo_id, record in latest_by_photo.items()
        if str(record.get("status", "")) not in RETRY_STATUSES
    }
    return [item for item in items if item.identifier not in completed]


def verify_applied_batch(
    run_dir: Path,
    current_items: Iterable[PhotoItem],
    records_by_photo: dict[str, dict[str, object]],
) -> int:
    """Confirm Photos holds the applied keywords; log each failure durably as retryable."""
    current = {item.identifier: item for item in current_items}
    failures = 0
    for photo_id, record in records_by_photo.items():
        if str(record.get("status", "")) != "applied":
            continue
        item = current.get(photo_id)
        if item is not None and set(item.keywords) == set(record.get("keywords_after", [])):
            continue
        failures += 1
        append_jsonl(
            run_dir / "results.jsonl",
            {
                "album": record.get("album", ""),
                "capture_date": record.get("capture_date", ""),
                "error": (
                    "verification could not find the photo in the batch"
                    if item is None
                    else "verification found different keywords in Photos than were applied"
                ),
                "filename": record.get("filename", ""),
                "keywords_before": record.get("keywords_before", []),
                "keywords_expected": record.get("keywords_after", []),
                "keywords_found": item.keywords if item is not None else [],
                "photo_id": photo_id,
                "source": record.get("source", "library"),
                "source_index": record.get("source_index"),
                "status": "verify-failed",
                "timestamp": utc_now(),
            },
        )
        print(
            f"  VERIFY ERROR: {record.get('filename', photo_id)}",
            file=sys.stderr,
        )
    return failures


def advance_library_cursor(
    metadata: dict[str, object],
    batch_start: int,
    count: int,
    order: str,
) -> None:
    """Move the cursor past a batch. Callers must verify the batch first."""
    metadata["next_index"] = library_next_index(batch_start, count, order)
    metadata["status"] = (
        "complete"
        if library_cursor_complete(
            int(metadata["next_index"]),
            int(metadata["total_count"]),
            order,
        )
        else "batch_complete"
    )


def write_review(run_dir: Path) -> None:
    attempts = read_jsonl(run_dir / "results.jsonl")
    latest_by_photo = latest_records_by_photo(attempts)
    write_review_file(run_dir / "review.csv", latest_by_photo.values())


def load_run(run_dir: Path) -> dict[str, object]:
    path = run_dir / "run.json"
    if not path.exists():
        raise RuntimeError(f"run metadata not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_run(run_dir: Path, metadata: dict[str, object]) -> None:
    (run_dir / "run.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def new_run_directory(base: Path, album: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", album).strip("-") or "album"
    run_dir = base / f"{stamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def tag_command(args: argparse.Namespace) -> int:
    if args.resume:
        run_dir = Path(args.resume).resolve()
        metadata = load_run(run_dir)
        source_type = str(metadata.get("source", "album"))
        album = str(metadata.get("album", "Photos Library"))
        apply_changes = bool(metadata["apply"])
        confidence = float(metadata["confidence"])
        max_tags = int(metadata["max_tags"])
        prefix = str(metadata["prefix"])
        keep_exports = bool(metadata["keep_exports"])
        limit = int(metadata["limit"])
        backend = str(metadata.get("backend", "apple"))
        model = str(metadata.get("model", ""))
        batch_size = (
            int(metadata.get("batch_size", 25))
            if args.batch_size is None
            else args.batch_size
        )
        metadata["batch_size"] = batch_size
        library_order = str(metadata.get("order", "ascending"))
    else:
        source_type = "library" if args.library else "album"
        if source_type == "album" and not args.album:
            raise RuntimeError("--album or --library is required unless --resume is used")
        album = args.album if source_type == "album" else "Photos Library"
        apply_changes = args.apply
        confidence = args.confidence
        max_tags = args.max_tags
        prefix = args.prefix
        keep_exports = args.keep_exports
        limit = args.limit
        backend = args.backend
        model = args.model
        batch_size = args.batch_size if args.batch_size is not None else 25
        library_order = args.order
        run_dir = new_run_directory(Path(args.runs_dir).resolve(), album)
        metadata = {
            "album": album,
            "apply": apply_changes,
            "backend": backend,
            "batch_size": batch_size,
            "confidence": confidence,
            "created_at": utc_now(),
            "keep_exports": keep_exports,
            "limit": limit,
            "max_tags": max_tags,
            "model": model,
            "prefix": prefix,
            "source": source_type,
            "status": "running",
            "version": 4,
        }
        if source_type == "library":
            metadata["order"] = library_order
            metadata["total_count"] = library_count()
            metadata["next_index"] = (
                metadata["total_count"] if library_order == "descending" else 1
            )
        save_run(run_dir, metadata)

    batch_start = 0
    if source_type == "library":
        current_total = library_count()
        expected_total = int(metadata["total_count"])
        if current_total != expected_total:
            raise RuntimeError(
                f"Photos library count changed from {expected_total} to {current_total}; "
                "the index cursor was stopped to prevent skipped or duplicate items"
            )
        library_order = str(metadata.get("order", "ascending"))
        if library_order not in {"ascending", "descending"}:
            raise RuntimeError(f"unknown saved library order: {library_order!r}")
        default_index = expected_total if library_order == "descending" else 1
        batch_start = int(metadata.get("next_index", default_index))
        if library_cursor_complete(batch_start, expected_total, library_order):
            metadata["status"] = "complete"
            metadata["completed_at"] = utc_now()
            save_run(run_dir, metadata)
            print(f"Library run complete: {expected_total} items covered")
            return 0
        items = inventory_library_batch_chunked(batch_start, batch_size, library_order)
        if not items:
            raise RuntimeError(
                f"Photos returned no items at library index {batch_start}; "
                "stopping instead of advancing the cursor"
            )
    else:
        items = inventory_album(album)
        if limit > 0:
            items = items[:limit]

    latest_by_photo = latest_records_by_photo(read_jsonl(run_dir / "results.jsonl"))
    pending = pending_items(items, latest_by_photo)

    mode = "APPLY" if apply_changes else "DRY RUN"
    if source_type == "library":
        batch_end = library_batch_end(batch_start, len(items), library_order)
        print(
            f"{mode} BATCH: {len(pending)} pending of {len(items)} items "
            f"at library indexes {batch_start} to {batch_end} ({library_order})"
        )
    else:
        print(f"{mode}: {len(pending)} pending of {len(items)} selected photos in {album!r}")
    print(f"Run directory: {run_dir}")
    # Album runs with nothing pending are simply complete. Library runs fall
    # through even with an empty pending list so the batch is (re-)verified
    # before the cursor may advance — a verify-failed or verify-crashed batch
    # must never be skipped past on resume.
    if not pending and source_type == "album":
        metadata["status"] = "complete"
        metadata["completed_at"] = utc_now()
        save_run(run_dir, metadata)
        write_review(run_dir)
        return 0

    persistent_exports = run_dir / "exports"
    error_count = 0
    batch_records: list[dict[str, object]] = []
    for index, item in enumerate(pending, start=1):
        print(f"[{index}/{len(pending)}] {item.filename}", flush=True)
        record: dict[str, object] = {
            "album": album,
            "filename": item.filename,
            "keywords_before": item.keywords,
            "photo_id": item.identifier,
            "source": source_type,
            "source_index": item.source_index,
            "capture_date": item.capture_date,
            "timestamp": utc_now(),
        }
        try:
            if Path(item.filename).suffix.casefold() not in IMAGE_EXTENSIONS:
                record.update(
                    {
                        "classifications": [],
                        "determinations": {},
                        "generated_keywords": [],
                        "keywords_after": item.keywords,
                        "status": "skipped-unsupported",
                    }
                )
            else:
                if keep_exports:
                    item_export_dir = persistent_exports / re.sub(
                        r"[^A-Za-z0-9._-]+", "_", item.identifier
                    )
                    # Clear stale files from an earlier attempt so the exported
                    # still image can be identified unambiguously on retry.
                    if item_export_dir.exists():
                        for stale in item_export_dir.iterdir():
                            if stale.is_file():
                                stale.unlink()
                    image_path = (
                        export_library_photo(item, item_export_dir)
                        if source_type == "library"
                        else export_photo(album, item, item_export_dir)
                    )
                    analysis = classify(
                        image_path,
                        backend=backend,
                        model=model,
                        album=album,
                        maximum=max_tags,
                    )
                else:
                    with tempfile.TemporaryDirectory(prefix="phototagger-") as temp:
                        image_path = (
                            export_library_photo(item, Path(temp))
                            if source_type == "library"
                            else export_photo(album, item, Path(temp))
                        )
                        analysis = classify(
                            image_path,
                            backend=backend,
                            model=model,
                            album=album,
                            maximum=max_tags,
                        )
                classifications = list(analysis["classifications"])
                determinations = refine_determinations(
                    classifications,
                    dict(analysis["determinations"]),
                )
                descriptive = normalized_tags(
                    classifications,
                    confidence=confidence,
                    maximum=max_tags,
                    prefix=prefix,
                )
                generated = merge_keywords(
                    descriptive,
                    determination_keywords(determinations, prefix=prefix),
                )
                merged = merge_keywords(item.keywords, generated)
                if apply_changes and generated:
                    if source_type == "library":
                        set_library_keywords(item, merged)
                    else:
                        set_keywords(album, item.identifier, merged)
                    status = "applied"
                elif apply_changes:
                    status = "no-tags"
                else:
                    status = "review"
                record.update(
                    {
                        "classifications": classifications,
                        "determinations": determinations,
                        "generated_keywords": generated,
                        "keywords_after": merged,
                        "status": status,
                    }
                )
        except Exception as error:  # continue so one iCloud/export failure does not lose the run
            error_count += 1
            record.update({"error": str(error), "status": "error"})
            print(f"  ERROR: {error}", file=sys.stderr)
        append_jsonl(run_dir / "results.jsonl", record)
        batch_records.append(record)
        if source_type == "album":
            write_review(run_dir)

    if source_type == "library":
        batch_end = library_batch_end(batch_start, len(items), library_order)
        batch_review_path = library_batch_review_path(
            run_dir, batch_start, batch_end, library_order
        )
        if batch_records:
            write_review_file(batch_review_path, batch_records)
        if apply_changes:
            # Verify every applied record for this batch's photos — including
            # records from earlier invocations — not just this invocation's.
            batch_ids = {item.identifier for item in items}
            batch_records_by_photo = {
                photo_id: record
                for photo_id, record in latest_records_by_photo(
                    read_jsonl(run_dir / "results.jsonl")
                ).items()
                if photo_id in batch_ids
            }
            try:
                # With no new writes this invocation, the batch inventory read
                # at the top already reflects the current Photos state.
                current_items = (
                    inventory_library_batch_chunked(batch_start, len(items), library_order)
                    if batch_records
                    else items
                )
                error_count += verify_applied_batch(
                    run_dir, current_items, batch_records_by_photo
                )
            except RuntimeError as error:
                # Verification infrastructure itself failed (e.g. Photos
                # dropped its automation connection). Treat as inconclusive
                # rather than crashing before metadata is saved — every
                # per-item result is already durable in results.jsonl, so
                # nothing is lost, but this batch must not be trusted as
                # verified and the cursor must not advance.
                error_count += 1
                print(f"  VERIFICATION ERROR: {error}", file=sys.stderr)
        if error_count:
            metadata["status"] = "batch_errors"
        else:
            advance_library_cursor(metadata, batch_start, len(items), library_order)
        metadata["last_batch_start"] = batch_start
        metadata["last_batch_end"] = batch_end
        metadata["last_batch_verified"] = apply_changes and not error_count
        metadata["last_batch_completed_at"] = utc_now()
    else:
        metadata["status"] = "complete_with_errors" if error_count else "complete"
        metadata["completed_at"] = utc_now()
    metadata["errors_this_invocation"] = error_count
    save_run(run_dir, metadata)
    if source_type == "album":
        write_review(run_dir)
        review_path = run_dir / "review.csv"
    else:
        review_path = batch_review_path
    print(f"Finished with {error_count} error(s). Review {review_path}")
    if source_type == "library" and not error_count:
        print(
            "Library directional progress: "
            f"{library_directional_progress(int(metadata['next_index']), int(metadata['total_count']), library_order)} "
            f"of {metadata['total_count']} items ({library_order})"
        )
    return 1 if error_count else 0


def earliest_applied_by_photo(
    records: Iterable[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """The first applied record per photo holds the true pre-run keyword snapshot."""
    earliest: dict[str, dict[str, object]] = {}
    for record in records:
        if str(record.get("status", "")) != "applied":
            continue
        earliest.setdefault(str(record.get("photo_id", "")), record)
    return earliest


def rollback_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    metadata = load_run(run_dir)
    if not metadata.get("apply"):
        raise RuntimeError("This was a dry run; it did not change Photos")
    source_type = str(metadata.get("source", "album"))
    album = str(metadata["album"])
    if source_type == "library":
        current_total = library_count()
        expected_total = int(metadata["total_count"])
        if current_total != expected_total:
            raise RuntimeError(
                f"Photos library count changed from {expected_total} to {current_total}; "
                "rollback was stopped because saved library indexes may no longer match"
            )
    records = read_jsonl(run_dir / "results.jsonl")
    applied = list(earliest_applied_by_photo(records).values())
    audit_path = run_dir / f"rollback-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    print(f"Restoring keywords for {len(applied)} photos in {album!r}")
    errors = 0
    restored: list[dict[str, object]] = []
    for index, record in enumerate(applied, start=1):
        try:
            if source_type == "library":
                item = PhotoItem(
                    identifier=str(record["photo_id"]),
                    filename=str(record["filename"]),
                    keywords=list(record.get("keywords_after", [])),
                    source_index=int(record["source_index"]),
                )
                set_library_keywords(item, list(record["keywords_before"]))
            else:
                set_keywords(album, str(record["photo_id"]), list(record["keywords_before"]))
            restored.append(record)
            print(f"[{index}/{len(applied)}] restored {record['filename']}")
        except Exception as error:
            errors += 1
            append_jsonl(
                audit_path,
                {
                    "error": str(error),
                    "filename": record.get("filename", ""),
                    "photo_id": record.get("photo_id", ""),
                    "status": "error",
                    "timestamp": utc_now(),
                },
            )
            print(f"ERROR restoring {record['filename']}: {error}", file=sys.stderr)
    if restored:
        print(f"Verifying {len(restored)} restored photos")
    album_inventory: dict[str, PhotoItem] = {}
    if restored and source_type == "album":
        album_inventory = {item.identifier: item for item in inventory_album(album)}
    for record in restored:
        photo_id = str(record.get("photo_id", ""))
        audit: dict[str, object] = {
            "filename": record.get("filename", ""),
            "keywords_restored": record.get("keywords_before", []),
            "photo_id": photo_id,
            "timestamp": utc_now(),
        }
        try:
            if source_type == "library":
                readback = inventory_library_batch(
                    int(record["source_index"]), 1, "ascending", timeout=300
                )
                current = readback[0] if readback else None
                if current is not None and current.identifier != photo_id:
                    current = None
            else:
                current = album_inventory.get(photo_id)
            if current is None:
                raise RuntimeError("photo was not found during read-back")
            if set(current.keywords) != set(record.get("keywords_before", [])):
                audit["keywords_found"] = current.keywords
                raise RuntimeError("Photos read back different keywords than were restored")
            audit["status"] = "restored"
        except Exception as error:
            errors += 1
            audit.update({"error": str(error), "status": "verify-failed"})
            print(
                f"VERIFY ERROR restoring {record.get('filename', photo_id)}: {error}",
                file=sys.stderr,
            )
        append_jsonl(audit_path, audit)
    print(f"Restored {len(restored)} of {len(applied)} photos; {errors} error(s)")
    if applied:
        print(f"Audit: {audit_path}")
    return 1 if errors else 0


def coverage_command(args: argparse.Namespace) -> int:
    """Read-only sweep: report library photos that have no record in the run."""
    run_dir = Path(args.run).resolve()
    metadata = load_run(run_dir)
    if metadata.get("source") != "library":
        raise RuntimeError("coverage is only defined for whole-library runs")
    latest_by_photo = latest_records_by_photo(read_jsonl(run_dir / "results.jsonl"))
    processed = set(latest_by_photo)
    unresolved = sorted(
        photo_id
        for photo_id, record in latest_by_photo.items()
        if str(record.get("status", "")) in RETRY_STATUSES
    )
    total = library_count()
    expected_total = int(metadata.get("total_count", 0))
    if total != expected_total:
        print(
            f"note: Photos library count is {total} but the run recorded "
            f"{expected_total}; differences may reflect adds or deletes since the run"
        )
    range_start = args.start if args.start is not None else 1
    range_end = args.end if args.end is not None else total
    if range_start < 1 or range_end > total or range_start > range_end:
        raise RuntimeError(
            f"--start/--end must satisfy 1 <= start <= end <= {total} (current library count)"
        )
    if range_start > 1 or range_end < total:
        print(f"scoping sweep to indexes {range_start}-{range_end} of {total}")
    missing: list[PhotoItem] = []
    chunk_size = 100
    start = range_start
    scanned = 0
    scan_total = range_end - range_start + 1
    while start <= range_end:
        count = min(chunk_size, range_end - start + 1)
        for item in inventory_library_batch(start, count, "ascending", timeout=600):
            if item.identifier not in processed:
                missing.append(item)
        scanned += count
        start += count
        if scanned % 1000 == 0:
            print(
                f"scanned {scanned} of {scan_total} items; {len(missing)} unprocessed so far",
                flush=True,
            )
    report_path = run_dir / f"coverage-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_index", "capture_date", "filename", "photo_id"])
        for item in missing:
            writer.writerow(
                [item.source_index, item.capture_date, item.filename, item.identifier]
            )
    print(f"{len(missing)} of {scan_total} scanned items (indexes {range_start}-{range_end}) have no record in this run")
    if unresolved:
        print(
            f"{len(unresolved)} recorded items ended in a retryable state "
            "(error or verify-failed); resume the run to retry them"
        )
    print(f"Report: {report_path}")
    return 1 if missing or unresolved else 0


def set_library_order_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    metadata = load_run(run_dir)
    if metadata.get("source") != "library":
        raise RuntimeError("order can only be changed for a whole-library run")
    previous_total_count = int(metadata["total_count"])
    total_count = library_count()
    previous_order = str(metadata.get("order", "ascending"))
    previous_index = int(metadata.get("next_index", 1))
    history = list(metadata.get("order_history", []))
    history.append(
        {
            "changed_at": utc_now(),
            "from": previous_order,
            "previous_next_index": previous_index,
            "previous_total_count": previous_total_count,
            "to": args.order,
            "total_count": total_count,
        }
    )
    metadata["order_history"] = history
    metadata["order"] = args.order
    metadata["total_count"] = total_count
    metadata["next_index"] = total_count if args.order == "descending" else 1
    metadata["status"] = "batch_complete" if total_count else "complete"
    metadata["order_changed_at"] = utc_now()
    save_run(run_dir, metadata)
    print(
        f"Library order set to {args.order}; next physical Photos index is "
        f"{metadata['next_index']}. Existing results were preserved."
    )
    return 0


def rename_prefix_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    metadata = load_run(run_dir)
    if not metadata.get("apply"):
        raise RuntimeError("Prefixes can only be renamed from an applied run")
    album = str(metadata["album"])
    latest: dict[str, dict[str, object]] = {}
    for record in read_jsonl(run_dir / "results.jsonl"):
        if record.get("status") == "applied":
            latest[str(record["photo_id"])] = record
    inventory = {item.identifier: item for item in inventory_album(album)}
    audit_path = run_dir / f"prefix-change-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    changed = 0
    errors = 0
    for photo_id, record in latest.items():
        item = inventory.get(photo_id)
        audit: dict[str, object] = {
            "filename": record.get("filename", ""),
            "photo_id": photo_id,
            "timestamp": utc_now(),
        }
        try:
            if item is None:
                raise RuntimeError("Photo is no longer present in the source album")
            after = rename_generated_keywords(
                item.keywords,
                record.get("generated_keywords", []),
                from_prefix=args.from_prefix,
                to_prefix=args.to_prefix,
            )
            if after != item.keywords:
                set_keywords(album, photo_id, after)
                changed += 1
            audit.update(
                {
                    "keywords_before": item.keywords,
                    "keywords_after": after,
                    "status": "changed" if after != item.keywords else "unchanged",
                }
            )
        except Exception as error:
            errors += 1
            audit.update({"error": str(error), "status": "error"})
            print(f"ERROR renaming {record.get('filename', photo_id)}: {error}", file=sys.stderr)
        append_jsonl(audit_path, audit)
    print(f"Renamed prefixes on {changed} of {len(latest)} photos; {errors} error(s)")
    print(f"Audit: {audit_path}")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("albums", help="list Photos albums")

    tag = subparsers.add_parser("tag", help="classify a Photos album")
    source = tag.add_mutually_exclusive_group()
    source.add_argument("--album", help="exact Photos album name")
    source.add_argument("--library", action="store_true", help="process the whole library")
    tag.add_argument("--apply", action="store_true", help="merge generated keywords into Photos")
    tag.add_argument(
        "--backend",
        choices=("ollama", "anthropic", "apple"),
        default="ollama",
        help="ollama (default, local Gemma) | anthropic (Claude API, "
        "needs ANTHROPIC_API_KEY; --model e.g. claude-sonnet-5) | apple (local Vision)",
    )
    tag.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="whole-library batch size (default: 25; with --resume, overrides the saved value)",
    )
    tag.add_argument(
        "--confidence",
        type=float,
        default=0.65,
        help="minimum confidence for the apple backend (ollama tags are always 1.0)",
    )
    tag.add_argument("--keep-exports", action="store_true")
    tag.add_argument("--limit", type=int, default=25, help="0 processes the full album")
    tag.add_argument(
        "--max-tags",
        type=int,
        default=5,
        help="maximum descriptive tags; determination flags such as "
        "'screenshot' or 'blurry' are additive",
    )
    tag.add_argument("--model", default="gemma4:e4b-it-qat", help="Ollama vision model")
    tag.add_argument(
        "--order",
        choices=("ascending", "descending"),
        default="ascending",
        help="whole-library traversal order (default: ascending)",
    )
    tag.add_argument("--prefix", default="")
    tag.add_argument("--resume", help="existing run directory")
    tag.add_argument("--runs-dir", default=str(ROOT / "runs"))

    rollback = subparsers.add_parser("rollback", help="restore keywords from an applied run")
    rollback.add_argument("--run", required=True)

    coverage = subparsers.add_parser(
        "coverage",
        help="report library photos with no record in a whole-library run (read-only)",
    )
    coverage.add_argument("--run", required=True)
    coverage.add_argument(
        "--start", type=int, default=None, help="scope the sweep to this index (default: 1)"
    )
    coverage.add_argument(
        "--end",
        type=int,
        default=None,
        help="scope the sweep to this index (default: current library count)",
    )

    set_order = subparsers.add_parser(
        "set-library-order", help="restart a whole-library cursor in a chosen order"
    )
    set_order.add_argument("--run", required=True)
    set_order.add_argument("--order", choices=("ascending", "descending"), required=True)

    rename_prefix = subparsers.add_parser(
        "rename-prefix", help="rename generated keywords from an applied run"
    )
    rename_prefix.add_argument("--run", required=True)
    rename_prefix.add_argument("--from-prefix", default="AI: ")
    rename_prefix.add_argument("--to-prefix", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "albums":
            for album in list_albums():
                print(album)
            return 0
        if args.command == "tag":
            if not 0.0 <= args.confidence <= 1.0:
                raise RuntimeError("--confidence must be between 0 and 1")
            if (
                args.max_tags < 1
                or args.limit < 0
                or (args.batch_size is not None and args.batch_size < 1)
            ):
                raise RuntimeError(
                    "--max-tags and --batch-size must be positive; --limit cannot be negative"
                )
            return tag_command(args)
        if args.command == "rollback":
            return rollback_command(args)
        if args.command == "coverage":
            return coverage_command(args)
        if args.command == "set-library-order":
            return set_library_order_command(args)
        if args.command == "rename-prefix":
            return rename_prefix_command(args)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(f"phototagger: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
