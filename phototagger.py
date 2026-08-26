#!/usr/bin/env python3
"""Safely classify Photos albums or the whole library and merge optional keywords."""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import csv
import fcntl
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
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
# Videos are classified from a single extracted frame (see extract_video_frame);
# every other stage — keyword merge, journal, verify, rollback — is identical to
# a still, because Photos treats keywords the same for both.
VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mov",
    ".mp4",
}
# How far into a clip to grab the representative frame. Drone and phone footage
# routinely opens on a black fade-in, a lens cap, or motion blur from takeoff,
# all of which classify as nothing useful; a tenth of the way in is past that
# without being deep enough to miss the subject of a short clip.
VIDEO_FRAME_POSITION = 0.1
# Statuses that must be retried on resume; anything else counts as completed.
# "write-pending" is the write-ahead journal entry appended immediately before
# a Photos mutation: if it is the latest record for a photo, the process died
# mid-write and the (idempotent) write must be retried.
RETRY_STATUSES = {"error", "verify-failed", "write-pending"}
# A hung Photos fails every AppleEvent; stop the batch after this many
# consecutive per-item errors instead of grinding through the rest.
CONSECUTIVE_ERROR_LIMIT = 8
# A photo that has failed this many separate attempts is not transient —
# observed live: 260 photos (mostly UUID-named, originals unavailable in
# iCloud) each failed 8 times with "empty export" and would have retried
# forever. Such photos leave pending; coverage reports them as unresolved.
MAX_ERROR_ATTEMPTS = 5
# Exit code signalling "Photos appears hung; restart it and resume" — chosen
# to match EX_TEMPFAIL so the guarded runner can distinguish a recoverable
# hang from a real error that needs human eyes.
EXIT_PHOTOS_HUNG = 75
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


class PhotosTimeoutError(RuntimeError):
    """Photos hung or its AppleEvent timed out — the hang-circuit signature.

    A distinct type, not a message substring: backend stalls (Ollama, hosted
    APIs) also stringify to "timed out", and matching on the text once made an
    API stall force-restart Photos for a problem Photos didn't have.
    """


# Transient HTTP statuses worth an in-process retry before burning the photo's
# error budget: rate limits (429), server-side blips (5xx), and Anthropic's
# overloaded signal (529). Auth and client errors fail fast — retrying a bad
# key or an unknown model just delays a clear message.
TRANSIENT_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 529}


def urlopen_json_with_retry(
    request: urllib.request.Request,
    *,
    timeout: int,
    retries: int = 2,
) -> dict[str, object]:
    """urlopen + JSON parse, retrying transient failures with backoff.

    Retries only what a wait can fix (TRANSIENT_HTTP_STATUSES and URLError —
    connection resets, DNS blips, socket timeouts); everything else raises
    immediately for the caller's per-backend error translation. Without this,
    every 429 in a hosted-API run cost one of the photo's five lifetime error
    attempts — an environmental burst could permanently exclude photos.
    """
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in TRANSIENT_HTTP_STATUSES or attempt >= retries:
                raise
            error.read()  # drain so the connection can be reused
        except urllib.error.URLError:
            if attempt >= retries:
                raise
        delay = (5, 20, 60)[min(attempt, 2)]
        print(
            f"transient API failure; retrying in {delay}s ({attempt + 1}/{retries})",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
    raise RuntimeError("retry loop ended unexpectedly")


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
                raise PhotosTimeoutError(
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
                # -1712 exhausted is Photos reporting its own AppleEvent
                # timeout — the same wedged-Photos signature as a hard hang,
                # so it carries the same type for the circuit breaker.
                if "(-1712)" in detail:
                    raise PhotosTimeoutError(
                        f"Photos automation failed: {detail}"
                    ) from error
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
setting, activity, and colors. Name things as SPECIFICALLY as the image actually
supports: if you can tell a goose from a duck, or a maple from a tree, use the
specific name. Only fall back to a general word when the image genuinely does not
support more precision. Never guess a species or model you cannot clearly see.
Use lower-case singular nouns or
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
        result = urlopen_json_with_retry(request, timeout=900)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Ollama request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        detail = getattr(error, "reason", error)
        raise RuntimeError(f"Ollama request failed: {detail}") from error
    content = result.get("message", {}).get("content", "")
    decoded = json.loads(content)
    return shape_vision_result(decoded, maximum, source="Ollama")


BACKEND_CHOICES = ("ollama", "openai-compatible", "anthropic", "apple")

DEFAULT_OPENAI_API_BASE = "https://api.openai.com/v1"

# Hosts we can name confidently. Anything else falls back to the first label of
# the hostname, so a self-hosted endpoint still gets a stable keychain service.
KNOWN_PROVIDER_HOSTS = {
    "api.openai.com": "openai",
    "openrouter.ai": "openrouter",
    "api.groq.com": "groq",
    "api.together.xyz": "together",
    "api.deepinfra.com": "deepinfra",
    "api.mistral.ai": "mistral",
    "api.x.ai": "xai",
    "generativelanguage.googleapis.com": "gemini",
}

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def normalize_api_base(api_base: str) -> str:
    """Accept either a bare base URL or a full chat-completions URL."""
    base = api_base.strip().rstrip("/")
    if not base:
        raise RuntimeError("--api-base cannot be empty")
    for suffix in ("/chat/completions", "/completions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    if not urllib.parse.urlsplit(base).scheme:
        raise RuntimeError(f"--api-base must include a scheme, e.g. https://: {api_base!r}")
    return base


def provider_slug(api_base: str) -> str:
    """Short, stable name for an endpoint — used for the keychain service name."""
    host = (urllib.parse.urlsplit(normalize_api_base(api_base)).hostname or "").casefold()
    if not host:
        raise RuntimeError(f"--api-base has no host: {api_base!r}")
    if host in LOCAL_HOSTS:
        return "local"
    if host in KNOWN_PROVIDER_HOSTS:
        return KNOWN_PROVIDER_HOSTS[host]
    labels = [label for label in host.split(".") if label and label != "api"]
    return labels[0] if labels else host


def is_local_endpoint(api_base: str) -> bool:
    """Local servers (LM Studio, vLLM, Ollama) usually accept any or no key."""
    host = (urllib.parse.urlsplit(normalize_api_base(api_base)).hostname or "").casefold()
    return host in LOCAL_HOSTS


@functools.lru_cache(maxsize=None)
def keychain_secret(service: str) -> str | None:
    """Read a generic password from the macOS Keychain, or None if absent.

    Deliberately forgiving: a locked keychain, a missing `security` binary, or a
    denied prompt must not abort a long run, it just means "fall back to the
    environment". Cached because this is called once per photo — which also
    means a transient failure pins None for the whole process, so any failure
    other than plain item-not-found warns once on stderr instead of silently
    switching credential sources for the rest of the run.
    """
    try:
        completed = run_command(
            ["security", "find-generic-password", "-s", service, "-w"],
            timeout=15,
        )
    except subprocess.CalledProcessError as error:
        if error.returncode != 44:  # 44 = errSecItemNotFound: a plain absence
            print(
                f"warning: keychain lookup for {service!r} failed "
                f"(security exited {error.returncode}, e.g. locked keychain or "
                "denied prompt); falling back to the environment for the rest "
                "of this run",
                file=sys.stderr,
                flush=True,
            )
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as error:
        print(
            f"warning: keychain lookup for {service!r} failed ({error!r}); "
            "falling back to the environment for the rest of this run",
            file=sys.stderr,
            flush=True,
        )
        return None
    secret = completed.stdout.strip()
    return secret or None


def resolve_api_key(
    *,
    service: str,
    env_vars: Iterable[str],
    required: bool = True,
    what: str = "this backend",
) -> str:
    """Keychain first, then environment. Never reads or writes a plaintext key file.

    The returned key is always stripped: a trailing newline or a space pasted
    into an env var is invisible and produces a 401 that reads like a bad key.
    """
    names = list(dict.fromkeys(env_vars))
    secret = keychain_secret(service)
    if not secret:
        for name in names:
            candidate = os.environ.get(name, "").strip()
            if candidate:
                secret = candidate
                break
    secret = (secret or "").strip()
    if not secret:
        if not required:
            return ""
        exported = " or ".join(names) if names else "an API key environment variable"
        raise RuntimeError(
            f"no API key found for {what}. Add one to the macOS Keychain:\n"
            f"  security add-generic-password -s {service} -a phototagger -w\n"
            f"(that prompts for the key instead of putting it in your shell history)\n"
            f"or export {exported}."
        )
    if any(character.isspace() for character in secret):
        raise RuntimeError(
            f"the API key for {what} contains whitespace, which servers reject as a "
            "malformed credential (usually a stray space from copy/paste). "
            f"Re-enter it in the keychain item {service!r} or the environment."
        )
    return secret


ANTHROPIC_TOOL_NAME = "record_photo_tags"


def classify_with_anthropic(
    image: Path,
    *,
    model: str,
    album: str,
    maximum: int,
    endpoint: str = "https://api.anthropic.com/v1/messages",
) -> dict[str, object]:
    api_key = resolve_api_key(
        service="phototagger-anthropic",
        env_vars=("ANTHROPIC_API_KEY",),
        what="--backend anthropic",
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
        result = urlopen_json_with_retry(request, timeout=180)
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


OPENAI_SCHEMA_NAME = "record_photo_tags"

# Keywords OpenAI's strict structured-output mode rejects outright. Dropping
# them is safe here: shape_vision_result() already truncates to `maximum`, and
# the prompt states the limit in words.
OPENAI_UNSUPPORTED_SCHEMA_KEYS = ("maxItems", "minItems", "maxLength", "minLength")


def openai_strict_schema(schema: dict[str, object]) -> dict[str, object]:
    """Copy of the shared schema with keywords strict mode refuses to compile."""

    def strip(node: object) -> object:
        if isinstance(node, dict):
            return {
                key: strip(value)
                for key, value in node.items()
                if key not in OPENAI_UNSUPPORTED_SCHEMA_KEYS
            }
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(copy.deepcopy(schema))  # type: ignore[return-value]


def openai_api_key(api_base: str) -> str:
    """Credential for an OpenAI-compatible endpoint, keyed by provider."""
    slug = provider_slug(api_base)
    env_name = re.sub(r"[^A-Z0-9]+", "_", slug.upper()) + "_API_KEY"
    return resolve_api_key(
        service=f"phototagger-{slug}",
        env_vars=(env_name, "OPENAI_API_KEY"),
        # Local servers (LM Studio, vLLM, Ollama's /v1) ship without auth.
        required=not is_local_endpoint(api_base),
        what=f"--backend openai-compatible --api-base {normalize_api_base(api_base)}",
    )


def openai_compatible_request(
    url: str, payload: dict[str, object] | None, *, api_key: str, timeout: int
) -> dict[str, object]:
    """One JSON round-trip, with errors that name the likely cause."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    try:
        return urlopen_json_with_retry(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        hint = ""
        if error.code in (401, 403):
            hint = (
                " — the endpoint rejected the credential. Check the key itself, "
                "and that it belongs to this provider."
            )
        elif error.code == 404:
            hint = (
                " — check --api-base. It should end at /v1, not at "
                "/v1/chat/completions, and the model must exist on this provider."
            )
        elif error.code == 400:
            hint = (
                " — the provider rejected the request body. Usually an unknown "
                "model name, or a parameter this model does not accept."
            )
        raise RuntimeError(
            f"OpenAI-compatible request failed ({error.code}): {detail}{hint}"
        ) from error
    except urllib.error.URLError as error:
        detail = getattr(error, "reason", error)
        raise RuntimeError(
            f"OpenAI-compatible request failed: {detail} (is {url} reachable?)"
        ) from error


def list_openai_compatible_models(api_base: str = DEFAULT_OPENAI_API_BASE) -> list[str]:
    """GET /v1/models, so users can read model names instead of guessing them."""
    base = normalize_api_base(api_base)
    result = openai_compatible_request(
        f"{base}/models", None, api_key=openai_api_key(api_base), timeout=60
    )
    entries = result.get("data", result)
    if not isinstance(entries, list):
        raise RuntimeError("the provider returned an unexpected /models payload")
    names = [
        str(entry.get("id", "")).strip()
        for entry in entries
        if isinstance(entry, dict) and str(entry.get("id", "")).strip()
    ]
    return sorted(names, key=str.casefold)


def classify_with_openai_compatible(
    image: Path,
    *,
    model: str,
    album: str,
    maximum: int,
    api_base: str = DEFAULT_OPENAI_API_BASE,
    endpoint: str | None = None,
) -> dict[str, object]:
    """OpenAI chat-completions shape: covers OpenAI, OpenRouter, Groq, Together,
    DeepInfra, LM Studio, vLLM, and Ollama's own /v1 endpoint."""
    base = normalize_api_base(api_base)
    url = endpoint or f"{base}/chat/completions"
    api_key = openai_api_key(base)
    prompt = build_vision_prompt(maximum, album)
    schema = openai_strict_schema(build_tag_schema(maximum))
    image_bytes = image_bytes_for_ollama(image)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                    },
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": OPENAI_SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            },
        },
    }
    # Deliberately no `temperature` and no `max_tokens`: several current models
    # reject one or both outright (400), and the structured-output schema
    # already pins the response shape. Omitting them is the portable choice.
    result = openai_compatible_request(url, payload, api_key=api_key, timeout=180)
    choices = result.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("the provider returned no choices")
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    if not isinstance(message, dict):
        raise RuntimeError("the provider returned an invalid message payload")
    refusal = message.get("refusal")
    if refusal:
        raise RuntimeError(f"the model refused to classify this image: {refusal}")
    content = message.get("content", "")
    if isinstance(content, list):
        # Some providers return content parts rather than a bare string.
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text", "output_text")
        )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("the provider returned an empty response body")
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "the provider did not return JSON matching the requested schema; "
            "this model may not support structured output: "
            f"{content[:200]}"
        ) from error
    if not isinstance(decoded, dict):
        raise RuntimeError("the provider returned a non-object JSON payload")
    return shape_vision_result(decoded, maximum, source="OpenAI-compatible")


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


def parse_color_histogram(text: str) -> list[dict[str, object]]:
    """Parse `magick ... histogram:info:` output into weighted hex swatches."""
    entries: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+):.*?#([0-9A-Fa-f]{6})", line)
        if match:
            entries.append((int(match.group(1)), "#" + match.group(2).upper()))
    total = sum(count for count, _ in entries)
    if not total:
        return []
    entries.sort(key=lambda entry: (-entry[0], entry[1]))
    return [
        {"hex": hex_value, "weight": round(count / total, 4)}
        for count, hex_value in entries
    ]


def image_palette(image: Path, *, swatches: int = 5) -> dict[str, object] | None:
    """Dominant-color swatches via ImageMagick quantization, or None.

    Color is enrichment riding an export we already paid for; a palette
    failure must never fail the photo's tagging, so every error maps to None.
    """
    try:
        result = run_command(
            [
                "magick",
                f"{image}[0]",
                "-auto-orient",
                "-resize",
                "64x64!",
                "-alpha",
                "remove",
                "-colors",
                str(swatches),
                "-depth",
                "8",
                "-format",
                "%c",
                "histogram:info:",
            ],
            timeout=120,
        )
        parsed = parse_color_histogram(result.stdout)
    except Exception:
        return None
    if not parsed:
        return None
    return {"dominant": parsed[0]["hex"], "swatches": parsed}


def classify(
    image: Path,
    *,
    backend: str,
    model: str,
    album: str,
    maximum: int,
    api_base: str | None = None,
) -> dict[str, object]:
    if backend == "ollama":
        return classify_with_ollama(image, model=model, album=album, maximum=maximum)
    if backend == "anthropic":
        return classify_with_anthropic(image, model=model, album=album, maximum=maximum)
    if backend == "openai-compatible":
        return classify_with_openai_compatible(
            image,
            model=model,
            album=album,
            maximum=maximum,
            api_base=api_base or DEFAULT_OPENAI_API_BASE,
        )
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


def device_keyword_from_exif(make: str, model: str, *, prefix: str) -> str | None:
    """Map EXIF Make/Model to a device keyword, or None.

    Two devices for now, so iPads, Macs, and other cameras stay untagged:
    Apple + an iPhone model, and any DJI-made camera (the drones write
    Make=DJI with a bare sensor code like "FC3411" for the model, so the
    maker alone is the reliable signal). Pure string logic, kept separate
    from the subprocess so it can be unit-tested.
    """
    make_key = make.strip().casefold()
    if make_key == "apple" and model.strip().casefold().startswith("iphone"):
        return f"{prefix}iPhone"
    if make_key == "dji" or make_key.startswith("dji "):
        return f"{prefix}Drone"
    return None


def device_keyword(image_path: Path, *, prefix: str) -> str | None:
    """Read the photo's EXIF Make/Model and return its device keyword, or None.

    Reads the exported original with ImageMagick (already a hard dependency),
    so it works for HEIC and JPG alike — older iPhone photos are JPG, and this
    correctly tags those while skipping iPad/screenshot HEICs. Any read failure
    yields None: a device tag is a bonus, never a reason to fail a photo.
    """
    try:
        result = subprocess.run(
            [
                "magick",
                "identify",
                "-format",
                "%[EXIF:Make]\t%[EXIF:Model]",
                str(image_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    make, _, model = result.stdout.partition("\t")
    return device_keyword_from_exif(make, model, prefix=prefix)


def video_duration_seconds(video_path: Path) -> float | None:
    """Clip length via ffprobe, or None if it cannot be determined."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def extract_video_frame(video_path: Path, destination: Path) -> Path:
    """Write one representative frame from a video and return its path.

    Seeks VIDEO_FRAME_POSITION into the clip rather than taking frame 0, which
    is usually a black fade-in. A clip whose duration ffprobe cannot read falls
    back to one second in — still past a fade, and safe on a very short clip
    because ffmpeg clamps a seek beyond the end to the last frame.
    """
    duration = video_duration_seconds(video_path)
    offset = duration * VIDEO_FRAME_POSITION if duration else 1.0
    frame_path = destination / f"{video_path.stem}-frame.jpg"
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-ss",
                f"{offset:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(frame_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not extract a frame from {video_path.name}") from error
    # ffmpeg exits 0 having written nothing for a truncated file, so trust the
    # artifact on disk rather than the return code.
    if not frame_path.exists() or frame_path.stat().st_size == 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["no frame written"]
        raise RuntimeError(f"could not extract a frame from {video_path.name}: {detail[0]}")
    return frame_path


def device_keyword_from_video(video_path: Path, *, prefix: str) -> str | None:
    """Device keyword for a video, read from its container metadata.

    Videos carry no EXIF, so the maker arrives as a container tag — iPhone
    footage sets "com.apple.quicktime.make". DJI writes no maker at all: the
    only brand marker on its MP4s is the per-stream handler name ("DJI.AVC",
    "DJI.Meta"), so that is treated as the maker when present. Both routes end
    in the same Make/Model mapping the stills use, keeping the two in lockstep.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags:stream_tags",
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        probe = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    tags = probe.get("format", {}).get("tags", {}) or {}
    lowered = {str(key).casefold(): str(value) for key, value in tags.items()}
    make = lowered.get("make") or lowered.get("com.apple.quicktime.make") or ""
    model = lowered.get("model") or lowered.get("com.apple.quicktime.model") or ""
    if make:
        return device_keyword_from_exif(make, model, prefix=prefix)
    # No maker tag: fall back to the stream handler names. DJI prefixes its
    # handlers with a control character, so match on a contained "dji." rather
    # than the start of the string.
    handlers = [
        str(stream_tags.get(key, ""))
        for stream in probe.get("streams", []) or []
        for stream_tags in [stream.get("tags", {}) or {}]
        for key in stream_tags
        if str(key).casefold() == "handler_name"
    ]
    if any("dji." in handler.casefold() for handler in handlers):
        return device_keyword_from_exif("DJI", "", prefix=prefix)
    return None


def prepare_for_classification(media_path: Path, *, prefix: str) -> tuple[Path, str | None]:
    """Return the image to classify and the device keyword for an export.

    A still is its own classification input; a video yields an extracted frame,
    with the device read from the movie's container rather than the frame,
    which ffmpeg writes without any maker metadata.
    """
    if media_path.suffix.casefold() in VIDEO_EXTENSIONS:
        frame_path = extract_video_frame(media_path, media_path.parent)
        return frame_path, device_keyword_from_video(media_path, prefix=prefix)
    return media_path, device_keyword(media_path, prefix=prefix)


def folder_album_keywords(
    path_segments: Iterable[str],
    album_name: str,
    renames: dict[str, str],
) -> list[str]:
    """Location keywords for an album: its name plus every ancestor folder.

    Segments are trimmed (Photos folder names can carry invisible trailing
    spaces), renamed via the OLD=NEW map, case-insensitively deduplicated
    (the "Italy" album inside the "Italy" folder yields one keyword), and
    "Untitled Album" contributes nothing — its photos still receive the
    ancestor folders' keywords.
    """
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in (*path_segments, album_name):
        value = str(raw).strip()
        value = renames.get(value, value)
        key = value.casefold()
        if not value or key in seen or key == "untitled album":
            continue
        seen.add(key)
        keywords.append(value)
    return keywords


def tag_folder_command(args: argparse.Namespace) -> int:
    renames: dict[str, str] = {}
    for pair in args.rename or []:
        old, sep, new = pair.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise RuntimeError(f"--rename expects OLD=NEW, got {pair!r}")
        renames[old.strip()] = new.strip()

    listing = run_applescript(
        "list_folder_albums.applescript", args.folder, timeout=900
    )
    albums: list[tuple[list[str], str, list[str]]] = []
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        path_text, album_name, ids_text = parts
        segments = [s.strip() for s in path_text.split(" / ") if s.strip()]
        ids = [i for i in ids_text.split(",") if i]
        albums.append((segments, album_name, ids))
    if not albums:
        raise RuntimeError(
            f"no albums found under a Photos folder named {args.folder!r} "
            "(names are compared with surrounding whitespace trimmed)"
        )

    keywords_by_photo: dict[str, list[str]] = {}
    for segments, album_name, ids in albums:
        keywords = folder_album_keywords(segments, album_name, renames)
        if not keywords:
            continue
        for photo_id in ids:
            keywords_by_photo[photo_id] = merge_keywords(
                keywords_by_photo.get(photo_id, []), keywords
            )
    print(
        f"{len(albums)} albums under {args.folder!r}; "
        f"{len(keywords_by_photo)} distinct photos to keyword"
    )

    run_dir = new_run_directory(Path(args.runs_dir).resolve(), args.folder)
    metadata = {
        "album": f"folder:{args.folder}",
        "source": "library",
        "apply": args.apply,
        "command": "tag-folder",
        "folder": args.folder,
        "renames": renames,
        "created_at": utc_now(),
    }
    save_run(run_dir, metadata)
    applied = errors = not_found = 0
    with run_lock(run_dir):
        for index, (photo_id, keywords) in enumerate(
            sorted(keywords_by_photo.items()), start=1
        ):
            record: dict[str, object] = {
                "photo_id": photo_id,
                "album": metadata["album"],
                "source": "library",
                "generated_keywords": keywords,
                "timestamp": utc_now(),
            }
            if not args.apply:
                append_jsonl(run_dir / "results.jsonl", {**record, "status": "review"})
                continue
            try:
                # Read the current keywords first so the journal can say which
                # of these are actually new. Without it the entry claims every
                # keyword as its own, and rollback deletes keywords it never
                # added — this run once over-claimed 3,206 of them.
                existing = library_item_by_id(photo_id)
                if existing is None:
                    raise PhotoNotFoundError(photo_id)
                record["keywords_before"] = existing.keywords
                # Write-ahead journal: intent is durable before the Photos
                # mutation, so a crash mid-run retries idempotently.
                append_jsonl(
                    run_dir / "results.jsonl", {**record, "status": "write-pending"}
                )
                before, after = sync_library_keywords(photo_id, keywords)
                record.update(
                    {
                        "keywords_before": before,
                        "keywords_after": after,
                        "status": "applied",
                    }
                )
                applied += 1
            except PhotoNotFoundError:
                record["status"] = "not-found"
                not_found += 1
            except Exception as error:
                record.update({"status": "error", "error": str(error)})
                errors += 1
                print(f"  ERROR {photo_id}: {error}", file=sys.stderr, flush=True)
            append_jsonl(run_dir / "results.jsonl", record)
            if index % 100 == 0 or index == len(keywords_by_photo):
                print(f"[{index}/{len(keywords_by_photo)}]", flush=True)
            time.sleep(0.05)  # stay polite to Photos while the library run tags
        metadata["status"] = (
            "review"
            if not args.apply
            else ("complete_with_errors" if errors else "complete")
        )
        metadata["completed_at"] = utc_now()
        save_run(run_dir, metadata)
    if args.apply:
        print(
            f"Applied {applied}; {not_found} not found; {errors} error(s). "
            f"Rollback: ./phototagger.py rollback --run {run_dir}"
        )
    else:
        print(
            f"Dry run: {len(keywords_by_photo)} photos recorded in "
            f"{run_dir / 'results.jsonl'}; re-run with --apply to write"
        )
    return 1 if errors else 0


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


def media_extensions_for(filename: str) -> set[str]:
    """Which extensions count as *the* export for this item.

    Keyed on the item's own type so a Live Photo — which exports a .heic AND a
    .mov — still resolves to the still, while a video item resolves to the
    movie instead of failing to find any candidate.
    """
    if Path(filename).suffix.casefold() in VIDEO_EXTENSIONS:
        return VIDEO_EXTENSIONS
    return IMAGE_EXTENSIONS


def choose_exported_image(paths: Iterable[Path], expected_filename: str) -> Path:
    wanted = media_extensions_for(expected_filename)
    images = [path for path in paths if path.is_file() and path.suffix.casefold() in wanted]
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


def set_keywords(album: str, identifier: str, keywords: list[str]) -> None:
    """Exact-list replace; used only by rename-prefix, which needs it."""
    run_applescript(
        "set_keywords.applescript",
        album,
        identifier,
        KEYWORD_SEPARATOR.join(keywords),
    )


class PhotoNotFoundError(RuntimeError):
    """The photo id no longer resolves in the library (deleted or merged away)."""


def parse_before_after(output: str) -> tuple[list[str], list[str]]:
    """Parse a sync/remove AppleScript's 'before <FS> after' keyword lists."""
    fields = output.split(FIELD_SEPARATOR)
    if len(fields) != 2:
        raise RuntimeError("Photos returned an unexpected before/after keyword payload")
    before, after = fields
    return (
        before.split(KEYWORD_SEPARATOR) if before else [],
        after.split(KEYWORD_SEPARATOR) if after else [],
    )


def sync_keywords(
    album: str, identifier: str, new_keywords: list[str]
) -> tuple[list[str], list[str]]:
    """Atomically add missing keywords to an album photo; returns (before, after)."""
    output = run_applescript(
        "sync_keywords.applescript",
        album,
        identifier,
        KEYWORD_SEPARATOR.join(new_keywords),
    )
    return parse_before_after(output)


def sync_library_keywords(
    identifier: str, new_keywords: list[str]
) -> tuple[list[str], list[str]]:
    """Atomically add missing keywords to a library photo by id; returns (before, after)."""
    try:
        output = run_applescript(
            "sync_library_keywords_by_id.applescript",
            identifier,
            KEYWORD_SEPARATOR.join(new_keywords),
        )
    except RuntimeError as error:
        if "Can’t get media item id" in str(error) or "Can't get media item id" in str(error):
            raise PhotoNotFoundError(f"photo no longer exists: {identifier}") from error
        raise
    return parse_before_after(output)


def remove_keywords(
    album: str, identifier: str, targets: list[str]
) -> tuple[list[str], list[str]]:
    """Atomically remove matching keywords from an album photo; returns (before, after)."""
    output = run_applescript(
        "remove_keywords.applescript",
        album,
        identifier,
        KEYWORD_SEPARATOR.join(targets),
    )
    return parse_before_after(output)


def remove_library_keywords(
    identifier: str, targets: list[str]
) -> tuple[list[str], list[str]]:
    """Atomically remove matching keywords from a library photo by id; returns (before, after)."""
    try:
        output = run_applescript(
            "remove_library_keywords_by_id.applescript",
            identifier,
            KEYWORD_SEPARATOR.join(targets),
        )
    except RuntimeError as error:
        if "Can’t get media item id" in str(error) or "Can't get media item id" in str(error):
            raise PhotoNotFoundError(f"photo no longer exists: {identifier}") from error
        raise
    return parse_before_after(output)


def library_item_by_id(identifier: str) -> PhotoItem | None:
    """Read one library photo by id; None when the photo no longer exists.

    Fails fast on purpose. A healthy answer takes ~0.2s (measured 0.15-0.21s
    over the whole library), so the old 120s x 3-retry policy spent ~7 minutes
    per unresponsive photo to buy almost nothing: of 837 timeouts, 807 (96.4%)
    failed anyway after all three attempts. Meanwhile the retry that DOES work
    is the pending queue — 2,771 of 2,782 photos that errored at least once
    later succeeded on a subsequent batch.

    So give up quickly and let the batch loop record a retryable error: 30s is
    still 150x a normal answer, and one cheap retry absorbs a genuine blip
    (e.g. Photos mid-restart). This also sharpens the runner's hang detection,
    which trips after 8 consecutive errors — a real wedge is now recognized in
    minutes instead of an hour.
    """
    output = run_applescript(
        "library_item_by_id.applescript", identifier, timeout=30, retries=1
    )
    if output == "NOT_FOUND":
        return None
    fields = output.split(FIELD_SEPARATOR)
    if len(fields) != 4 or fields[0] != "FOUND":
        raise RuntimeError("Photos returned an unexpected item-by-id payload")
    _, filename, keyword_text, capture_date = fields
    keywords = keyword_text.split(KEYWORD_SEPARATOR) if keyword_text else []
    return PhotoItem(
        identifier=identifier,
        filename=filename,
        keywords=keywords,
        capture_date=capture_date,
    )


def export_library_photo_by_id(item: PhotoItem, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in destination.iterdir()}
    try:
        run_applescript(
            "export_library_item_by_id.applescript",
            item.identifier,
            str(destination),
            timeout=1800,
        )
    except RuntimeError as error:
        if "PHOTO_NOT_FOUND" in str(error):
            raise PhotoNotFoundError(f"photo no longer exists: {item.identifier}") from error
        raise
    created = [path for path in destination.iterdir() if path.name not in before and path.is_file()]
    try:
        return choose_exported_image(created, item.filename)
    except RuntimeError:
        pass
    if not created:
        candidates = [path for path in destination.iterdir() if path.is_file()]
        return choose_exported_image(candidates, item.filename)
    raise RuntimeError(f"Photos export did not yield an unambiguous still image for {item.filename}")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read JSONL, tolerating a torn FINAL record.

    append_jsonl flushes+fsyncs per record, so a crash mid-append can tear at
    most the last line. Dropping that torn tail loses only an in-flight
    record whose photo simply re-enters pending; refusing to load (the old
    behavior) made the entire run unresumable. Corruption anywhere else is
    not a crash signature and still raises.
    """
    if not path.exists():
        return []
    records = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            if all(not later.strip() for later in lines[index + 1:]):
                print(
                    f"warning: dropping torn final record in {path.name} "
                    "(crash mid-append); the affected photo re-enters pending",
                    file=sys.stderr,
                )
                break
            raise
    return records


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if is_new:
            os.fchmod(handle.fileno(), 0o600)


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
        "device",
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
                    "device": record.get("device") or "",
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


def matches_extensions(filename: str, extensions: set[str] | None) -> bool:
    """True when no filter is set, or the file's extension is in the set."""
    if not extensions:
        return True
    suffix = Path(filename).suffix.lstrip(".").casefold()
    return suffix in extensions


def matches_filenames(filename: str, needles: set[str] | None) -> bool:
    """True when no filter is set, or the name contains one of the substrings.

    Substring rather than glob: the drone exports this was written for carry
    two unrelated shapes for the same camera (`DJI_0004.JPG` and
    `dji_fly_20250819_...jpg`), and one needle catches both.
    """
    if not needles:
        return True
    name = filename.casefold()
    return any(needle in name for needle in needles)


def error_attempts_by_photo(
    records: Iterable[dict[str, object]],
) -> dict[str, int]:
    """CONSECUTIVE trailing errors per photo — any other status resets to zero.

    Deliberately not a lifetime tally. Environmental failures arrive in
    batch-wide bursts (the disk-full day produced 544 empty-export errors;
    a hosted API can 429 a whole batch), and a cumulative count charged every
    affected photo's permanent exclusion budget for problems that weren't the
    photo's: this library already has 278 photos with >= 5 lifetime errors, of
    which 276 later applied fine. A photo now only exhausts its
    MAX_ERROR_ATTEMPTS budget by failing that many separate attempts in a row
    with no success between them — the signature of a genuinely bad photo
    rather than a bad day.
    """
    counts: dict[str, int] = {}
    for record in records:
        photo_id = str(record.get("photo_id", ""))
        if not photo_id:
            continue
        status = str(record.get("status", ""))
        if status == "error":
            counts[photo_id] = counts.get(photo_id, 0) + 1
        elif status != "write-pending":
            # write-pending is journaled intent BEFORE the Photos write, not an
            # outcome — if it reset the streak, a photo failing at the write
            # step would alternate write-pending/error forever and never
            # exhaust its budget.
            counts[photo_id] = 0
    return counts


def pending_items(
    items: Iterable[PhotoItem],
    latest_by_photo: dict[str, dict[str, object]],
    only_extensions: set[str] | None = None,
    error_attempts: dict[str, int] | None = None,
    only_filenames: set[str] | None = None,
    apply_changes: bool = False,
) -> list[PhotoItem]:
    """Photos still needing work, optionally narrowed by file type and name.

    Either filter narrows *what this run processes*; neither ever marks the
    excluded photos as done, so dropping the filter later resumes them normally.
    The two compose (an AND) when both are given.
    Photos with >= MAX_ERROR_ATTEMPTS consecutive failed attempts are excluded
    as non-transient; coverage reports them as unresolved.

    A "review" record is a completed DRY-RUN, so under apply_changes it counts
    as pending again — otherwise resuming a dry-run directory with --apply
    would write nothing and report the run complete, a silent no-op that looks
    like success.

    A "skipped-unsupported" record is only durable while the type is *still*
    unsupported. Adding video support turned 202 already-skipped movies into
    permanently-untagged photos, because that status is not retryable; keying
    the exclusion on the current supported set re-opens exactly those, and
    does the same automatically for any type added later.
    """
    # Materialized because the supported-type lookup below traverses items a
    # second time; the parameter is an Iterable and may be a generator.
    items = list(items)
    supported = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    by_id = {item.identifier: item for item in items}
    completed = {
        photo_id
        for photo_id, record in latest_by_photo.items()
        if str(record.get("status", "")) not in RETRY_STATUSES
        and not (apply_changes and str(record.get("status", "")) == "review")
        and not (
            str(record.get("status", "")) == "skipped-unsupported"
            and photo_id in by_id
            and Path(by_id[photo_id].filename).suffix.casefold() in supported
        )
    }
    attempts = error_attempts or {}
    return [
        item
        for item in items
        if item.identifier not in completed
        and attempts.get(item.identifier, 0) < MAX_ERROR_ATTEMPTS
        and matches_extensions(item.filename, only_extensions)
        and matches_filenames(item.filename, only_filenames)
    ]


def verify_applied_batch(
    run_dir: Path,
    current_items: Iterable[PhotoItem],
    records_by_photo: dict[str, dict[str, object]],
) -> int:
    """Confirm Photos still holds this run's generated tags; log failures durably.

    Subset semantics: the photo's current keywords must contain every keyword
    this run generated (case-insensitive). Keywords the user added or removed
    concurrently in Photos.app are theirs to manage and never count as
    failures — the old exact-list match would have flagged any concurrent
    manual edit as corruption.
    """
    current = {item.identifier: item for item in current_items}
    failures = 0
    for photo_id, record in records_by_photo.items():
        if str(record.get("status", "")) != "applied":
            continue
        item = current.get(photo_id)
        if item is not None and generated_keywords_present(
            item.keywords, record.get("generated_keywords", [])
        ):
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
                    else "verification found this run's generated keywords missing in Photos"
                ),
                "filename": record.get("filename", ""),
                "generated_keywords": record.get("generated_keywords", []),
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
    """Atomically replace run.json so a crash mid-write cannot corrupt the cursor."""
    target = run_dir / "run.json"
    temp = run_dir / "run.json.tmp"
    temp.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    temp.replace(target)


@contextlib.contextmanager
def run_lock(run_dir: Path):
    """Exclusive per-run lock so mutating commands cannot run concurrently.

    Uses command.lock (not runner.lock) so the background runner — which holds
    runner.lock for its whole loop while invoking `tag --resume` as a child
    process — does not deadlock against its own child. Every mutating command
    (tag --resume, rollback, rename-prefix, set-library-order) takes this lock,
    which serializes actual Photos/run.json mutations regardless of who
    launched them.
    """
    # Best-effort retrofit for run dirs created before private modes existed:
    # a 0700 directory gates every artifact inside it regardless of file modes.
    try:
        os.chmod(run_dir, 0o700)
        if run_dir.parent.name == "runs":
            os.chmod(run_dir.parent, 0o700)
    except OSError:
        pass
    handle = (run_dir / "command.lock").open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"another PhotoTagger command is already operating on {run_dir.name}; "
                "wait for it to finish (or stop the background runner) and retry"
            ) from None
        yield
    finally:
        handle.close()


def load_manifest(run_dir: Path) -> list[dict[str, object]]:
    """Load the run's fixed photo-id manifest, deduplicated, in traversal order."""
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in read_jsonl(run_dir / "manifest.jsonl"):
        photo_id = str(record.get("photo_id", ""))
        if not photo_id or photo_id in seen:
            continue
        seen.add(photo_id)
        entries.append(record)
    return entries


def build_manifest(run_dir: Path, order: str) -> int:
    """Capture every photo id into manifest.jsonl via one bulk fetch.

    Positional per-item reads cost O(index) each, making a full positional
    sweep O(n^2) — measured at ~78 hours for a 78k library. The bulk
    AppleScript fetches the whole id/filename/date lists as three Apple
    events and returns in minutes. Everything after this build is addressed
    purely by photo id, so library growth/shrinkage between batches can no
    longer skew which photos get processed. Written atomically (temp+rename),
    replacing any stale partial manifest.
    """
    if order not in {"ascending", "descending"}:
        raise RuntimeError("library order must be ascending or descending")
    output = run_applescript("library_manifest_bulk.applescript", timeout=1800)
    entries: list[dict[str, object]] = []
    for row in output.split(ROW_SEPARATOR) if output else []:
        fields = row.split(FIELD_SEPARATOR)
        if len(fields) != 3:
            raise RuntimeError("Photos returned an unexpected manifest row")
        photo_id, filename, capture_date = fields
        entries.append(
            {
                "capture_date": capture_date,
                "filename": filename,
                "photo_id": photo_id,
            }
        )
    if order == "descending":
        entries.reverse()
    temp = run_dir / "manifest.jsonl.tmp"
    with temp.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(run_dir / "manifest.jsonl")
    return len(load_manifest(run_dir))


def generated_keywords_present(
    current: Iterable[str], generated: Iterable[str]
) -> bool:
    """Subset check: this run's tags must be present; extra keywords are fine."""
    have = {str(value).casefold() for value in current}
    return all(str(value).casefold() in have for value in generated)


def new_run_directory(base: Path, album: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", album).strip("-") or "album"
    run_dir = base / f"{stamp}-{slug}"
    run_dir.mkdir(parents=True, exist_ok=False)
    # Run artifacts hold private photo filenames and library identifiers;
    # keep them out of reach of other local users. chmod (not mkdir mode)
    # so the result does not depend on the caller's umask.
    os.chmod(run_dir, 0o700)
    return run_dir


def resolve_model(backend: str, model: str | None) -> str:
    """Backend-aware model default; never send an Ollama model name to a remote API."""
    if model:
        return model
    if backend == "ollama":
        return "gemma4:e4b-it-qat"
    if backend == "anthropic":
        raise RuntimeError(
            "--backend anthropic requires an explicit --model (e.g. claude-sonnet-5)"
        )
    if backend == "openai-compatible":
        raise RuntimeError(
            "--backend openai-compatible requires an explicit --model; run "
            "'phototagger test-backend --backend openai-compatible --list-models' "
            "to see what this provider offers"
        )
    return ""


def require_backend_credential(backend: str, api_base: str) -> None:
    """Resolve (and validate) an API backend's key without making a request."""
    if backend == "openai-compatible":
        openai_api_key(api_base)
    elif backend == "anthropic":
        resolve_api_key(
            service="phototagger-anthropic",
            env_vars=("ANTHROPIC_API_KEY",),
            what="--backend anthropic",
        )


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
        api_base = str(metadata.get("api_base", "") or DEFAULT_OPENAI_API_BASE)
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
        model = resolve_model(args.backend, args.model)
        api_base = normalize_api_base(args.api_base)
        batch_size = args.batch_size if args.batch_size is not None else 25
        library_order = args.order
        # Before the run directory exists: a credential failure should leave no
        # half-born run behind for the user to clean up.
        require_backend_credential(backend, api_base)
        run_dir = new_run_directory(Path(args.runs_dir).resolve(), album)
        metadata = {
            "album": album,
            "api_base": api_base if backend == "openai-compatible" else "",
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
            "version": 5,
        }
        if source_type == "library":
            metadata["order"] = library_order
        save_run(run_dir, metadata)

    # Resolve the credential once, before any export or iCloud download. A run
    # must never get underway on a key that was never going to work. (New runs
    # were already checked above, ahead of creating the run directory.)
    require_backend_credential(backend, api_base)

    only_extensions = None
    if getattr(args, "only_extensions", None):
        only_extensions = {
            e.strip().lstrip(".").casefold()
            for e in args.only_extensions.split(",")
            if e.strip()
        }

    only_filenames = None
    if getattr(args, "only_filenames", None):
        only_filenames = {
            n.strip().casefold() for n in args.only_filenames.split(",") if n.strip()
        }

    # Serialize against the background runner and any other mutating command
    # operating on this run directory.
    with run_lock(run_dir):
        return run_tag_batch(
            run_dir,
            metadata,
            source_type=source_type,
            album=album,
            apply_changes=apply_changes,
            confidence=confidence,
            max_tags=max_tags,
            prefix=prefix,
            keep_exports=keep_exports,
            limit=limit,
            backend=backend,
            model=model,
            api_base=api_base,
            batch_size=batch_size,
            library_order=library_order,
            only_extensions=only_extensions,
            only_filenames=only_filenames,
        )


def run_tag_batch(
    run_dir: Path,
    metadata: dict[str, object],
    *,
    source_type: str,
    album: str,
    apply_changes: bool,
    confidence: float,
    max_tags: int,
    prefix: str,
    keep_exports: bool,
    limit: int,
    backend: str,
    model: str,
    batch_size: int,
    library_order: str,
    api_base: str = DEFAULT_OPENAI_API_BASE,
    only_extensions: set[str] | None = None,
    only_filenames: set[str] | None = None,
) -> int:
    if source_type == "library":
        if library_order not in {"ascending", "descending"}:
            raise RuntimeError(f"unknown saved library order: {library_order!r}")
        if "next_index" in metadata:
            # Keyed on the retired cursor field, not on manifest.jsonl existing:
            # an interrupted migration can leave a stale partial manifest behind.
            raise RuntimeError(
                "this run predates id-based traversal; migrate it first: "
                f"python3 scripts/migrate_run_to_manifest.py {run_dir}"
            )
        if not (run_dir / "manifest.jsonl").exists():
            print("Building the photo-id manifest (one-time full sweep)")
        manifest_total = (
            build_manifest(run_dir, library_order)
            if not (run_dir / "manifest.jsonl").exists()
            else None
        )
        manifest = load_manifest(run_dir)
        metadata["manifest_total"] = len(manifest)
        if manifest_total is not None:
            metadata["manifest_built_at"] = utc_now()
        save_run(run_dir, metadata)
        current_total = library_count()
        if current_total != len(manifest):
            print(
                f"note: Photos now reports {current_total} items vs {len(manifest)} in "
                "the manifest; ids that no longer resolve are recorded as not-found"
            )
        items = [
            PhotoItem(
                identifier=str(entry.get("photo_id", "")),
                filename=str(entry.get("filename", "")),
                keywords=[],
                capture_date=str(entry.get("capture_date", "")),
            )
            for entry in manifest
        ]
    else:
        items = inventory_album(album)
        if limit > 0:
            items = items[:limit]

    if (
        source_type == "library"
        and apply_changes
        and metadata.get("status") in {"batch_errors", "batch_in_progress"}
        and metadata.get("last_batch_photo_ids")
    ):
        # The previous invocation ended with errors — its verification pass
        # may have died partway. Re-verify every photo that batch applied so
        # an unconfirmed write can never be silently trusted; failures become
        # retryable records and re-enter pending below.
        prior_ids = [str(pid) for pid in metadata["last_batch_photo_ids"]]
        print(f"Re-verifying {len(prior_ids)} photos from the errored batch")
        prior_records = {
            photo_id: record
            for photo_id, record in latest_records_by_photo(
                read_jsonl(run_dir / "results.jsonl")
            ).items()
            if photo_id in set(prior_ids)
        }
        prior_current = [
            current
            for photo_id in prior_records
            if (current := library_item_by_id(photo_id)) is not None
        ]
        reverify_failures = verify_applied_batch(run_dir, prior_current, prior_records)
        if reverify_failures:
            print(
                f"{reverify_failures} photo(s) failed re-verification and will be retried",
                file=sys.stderr,
            )

    all_records = read_jsonl(run_dir / "results.jsonl")
    latest_by_photo = latest_records_by_photo(all_records)
    error_attempts = error_attempts_by_photo(all_records)
    all_pending = pending_items(
        items,
        latest_by_photo,
        only_extensions,
        error_attempts,
        only_filenames,
        apply_changes=apply_changes,
    )
    # Completion must be judged against the UNFILTERED manifest: a filter
    # narrows what this run processes, never what "complete" means.
    truly_pending_count = (
        len(all_pending)
        if not (only_extensions or only_filenames)
        else len(
            pending_items(
                items, latest_by_photo, None, error_attempts, apply_changes=apply_changes
            )
        )
    )
    gave_up = sum(1 for c in error_attempts.values() if c >= MAX_ERROR_ATTEMPTS)
    if gave_up:
        print(
            f"note: {gave_up} photo(s) failed {MAX_ERROR_ATTEMPTS} consecutive "
            "attempts with no success between and are excluded from retry; "
            "`coverage` reports them as unresolved"
        )
    pending = all_pending[:batch_size] if source_type == "library" else all_pending

    mode = "APPLY" if apply_changes else "DRY RUN"
    if source_type == "library":
        print(
            f"{mode} BATCH: {len(pending)} of {len(all_pending)} pending photos "
            f"({len(items) - truly_pending_count} of {len(items)} already complete, "
            f"{library_order})"
        )
    else:
        print(f"{mode}: {len(pending)} pending of {len(items)} selected photos in {album!r}")
    print(f"Run directory: {run_dir}")
    if not pending:
        if truly_pending_count:
            # The extension filter is exhausted but the manifest is not done.
            # Park the run (STOP) so a guarded runner exits instead of looping
            # no-op batches — each of which would pointlessly restart Photos.
            metadata["status"] = "batch_complete"
            save_run(run_dir, metadata)
            (run_dir / "STOP").touch()
            flags = " and ".join(
                flag
                for flag, active in (
                    ("--only-extensions", only_extensions),
                    ("--only-filenames", only_filenames),
                )
                if active
            )
            print(
                f"Filter exhausted; {truly_pending_count} photos outside "
                f"the filter remain pending. STOP placed; remove it and relaunch "
                f"without {flags} to continue."
            )
            return 0
        metadata["status"] = "complete"
        metadata["completed_at"] = utc_now()
        save_run(run_dir, metadata)
        if source_type == "album":
            write_review(run_dir)
        else:
            print(f"Library run complete: {len(items)} manifest photos covered")
        return 0

    if source_type == "library" and apply_changes:
        # Marker written BEFORE the write loop: a crash anywhere in this batch
        # (including during verification) leaves status batch_in_progress with
        # this batch's photo ids, so the next resume re-verifies them all.
        # results.jsonl remains the source of truth for what actually happened.
        metadata["status"] = "batch_in_progress"
        metadata["batch_started_at"] = utc_now()
        metadata["last_batch_photo_ids"] = [item.identifier for item in pending]
        save_run(run_dir, metadata)

    persistent_exports = run_dir / "exports"
    error_count = 0
    not_found_count = 0
    consecutive_errors = 0
    circuit_broken = False
    batch_records: list[dict[str, object]] = []
    for index, item in enumerate(pending, start=1):
        if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
            # Photos is almost certainly wedged (a hung app fails every
            # subsequent AppleEvent too). Stop the batch cleanly rather than
            # churning out hundreds of error records; everything so far is
            # durable and the batch resumes normally once Photos recovers.
            circuit_broken = True
            print(
                f"stopping batch after {consecutive_errors} consecutive errors; "
                "Photos may need to be reopened before resuming",
                file=sys.stderr,
                flush=True,
            )
            break
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
            if source_type == "library":
                # Manifest entries carry no live state; re-read by id so
                # filename and keywords reflect Photos right now (or learn
                # the photo is gone). Inside the try so a Photos hiccup on
                # the read becomes a retryable per-item error, not a crash.
                current = library_item_by_id(item.identifier)
                if current is None:
                    raise PhotoNotFoundError(item.identifier)
                item = current
                record.update(
                    {
                        "filename": item.filename,
                        "keywords_before": item.keywords,
                        "capture_date": item.capture_date or record["capture_date"],
                    }
                )
            if Path(item.filename).suffix.casefold() not in (
                IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
            ):
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
                    media_path = (
                        export_library_photo_by_id(item, item_export_dir)
                        if source_type == "library"
                        else export_photo(album, item, item_export_dir)
                    )
                    # Videos classify from an extracted frame; stills are their
                    # own input. Device metadata is read here, while the export
                    # still exists on disk.
                    image_path, device = prepare_for_classification(
                        media_path, prefix=prefix
                    )
                    palette = image_palette(image_path)
                    analysis = classify(
                        image_path,
                        backend=backend,
                        model=model,
                        album=album,
                        maximum=max_tags,
                        api_base=api_base,
                    )
                else:
                    with tempfile.TemporaryDirectory(prefix="phototagger-") as temp:
                        media_path = (
                            export_library_photo_by_id(item, Path(temp))
                            if source_type == "library"
                            else export_photo(album, item, Path(temp))
                        )
                        # Frame extraction and the metadata read both happen
                        # inside the block, before the temp dir is gone.
                        image_path, device = prepare_for_classification(
                            media_path, prefix=prefix
                        )
                        # Palette reads pixels, so it must run while the temp
                        # export still exists.
                        palette = image_palette(image_path)
                        analysis = classify(
                            image_path,
                            backend=backend,
                            model=model,
                            album=album,
                            maximum=max_tags,
                            api_base=api_base,
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
                if device:
                    generated = merge_keywords(generated, [device])
                record.update(
                    {
                        "classifications": classifications,
                        "determinations": determinations,
                        "generated_keywords": generated,
                        "device": device,
                        "palette": palette,
                    }
                )
                if apply_changes and generated:
                    # Write-ahead journal: persist intent durably BEFORE the
                    # Photos mutation. If we crash between here and the
                    # "applied" record below, this write-pending record is the
                    # latest for the photo, so resume retries the (idempotent)
                    # sync — a Photos write can never outrun its journal.
                    append_jsonl(
                        run_dir / "results.jsonl",
                        {**record, "status": "write-pending"},
                    )
                    if source_type == "library":
                        before, after = sync_library_keywords(item.identifier, generated)
                    else:
                        before, after = sync_keywords(album, item.identifier, generated)
                    # The sync AppleScript read the true pre-write keywords
                    # atomically with the write; journal those, not the
                    # inventory-time snapshot.
                    record.update(
                        {
                            "keywords_before": before,
                            "keywords_after": after,
                            "status": "applied",
                        }
                    )
                elif apply_changes:
                    record.update(
                        {"keywords_after": item.keywords, "status": "no-tags"}
                    )
                else:
                    record.update(
                        {
                            "keywords_after": merge_keywords(item.keywords, generated),
                            "status": "review",
                        }
                    )
        except PhotoNotFoundError:
            not_found_count += 1
            consecutive_errors = 0  # Photos responded; the photo is just gone
            record.update({"status": "not-found"})
            print("  photo no longer in the library", flush=True)
        except Exception as error:  # continue so one iCloud/export failure does not lose the run
            error_count += 1
            # Only PHOTOS timeouts indicate a wedged Photos and count toward
            # the hang circuit breaker — matched by type, not message text,
            # because a stalled backend (Ollama, hosted API) also stringifies
            # to "timed out" and once made the runner restart Photos for a
            # problem Photos didn't have. Ordinary per-item failures are
            # recorded as retryable and must never trigger a restart loop.
            if isinstance(error, PhotosTimeoutError):
                consecutive_errors += 1
            else:
                consecutive_errors = 0
            record.update({"error": str(error), "status": "error"})
            print(f"  ERROR: {error}", file=sys.stderr)
        else:
            consecutive_errors = 0
        record["timestamp"] = utc_now()
        append_jsonl(run_dir / "results.jsonl", record)
        batch_records.append(record)
        if source_type == "album":
            write_review(run_dir)

    if source_type == "library":
        batch_review_path = (
            run_dir
            / "batches"
            / f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        )
        if batch_records:
            write_review_file(batch_review_path, batch_records)
        if apply_changes:
            # Re-read every photo this batch applied (by id) and confirm this
            # run's generated tags are present. Failures become durable
            # retryable records, so they re-enter pending on resume.
            applied_records = {
                str(record["photo_id"]): record
                for record in batch_records
                if record.get("status") == "applied"
            }
            try:
                current_items = [
                    current
                    for photo_id in applied_records
                    if (current := library_item_by_id(photo_id)) is not None
                ]
                error_count += verify_applied_batch(
                    run_dir, current_items, applied_records
                )
            except RuntimeError as error:
                # Verification infrastructure itself failed (e.g. Photos
                # dropped its automation connection). Treat as inconclusive
                # rather than crashing before metadata is saved — every
                # per-item result is already durable in results.jsonl.
                error_count += 1
                if isinstance(error, PhotosTimeoutError):
                    circuit_broken = True  # hang signature; runner may restart Photos
                print(f"  VERIFICATION ERROR: {error}", file=sys.stderr)
        # Only meaningful when error_count == 0: every photo attempted this
        # batch then has a terminal status, so what remains is exactly the
        # pending photos this batch did not reach.
        remaining = truly_pending_count - len(pending)
        if error_count:
            metadata["status"] = "batch_errors"
        else:
            metadata["status"] = "batch_complete" if remaining > 0 else "complete"
        metadata["last_batch_size"] = len(pending)
        metadata["last_batch_photo_ids"] = [item.identifier for item in pending]
        metadata["last_batch_verified"] = apply_changes and not error_count
        metadata["last_batch_completed_at"] = utc_now()
    else:
        metadata["status"] = "complete_with_errors" if error_count else "complete"
        metadata["completed_at"] = utc_now()
    metadata["errors_this_invocation"] = error_count
    metadata["not_found_this_invocation"] = not_found_count
    metadata["applied_this_invocation"] = sum(
        1 for record in batch_records if record.get("status") == "applied"
    )
    save_run(run_dir, metadata)
    if source_type == "album":
        write_review(run_dir)
        review_path = run_dir / "review.csv"
    else:
        review_path = batch_review_path
    summary = f"Finished with {error_count} error(s)"
    if not_found_count:
        summary += f" and {not_found_count} no-longer-present photo(s)"
    print(f"{summary}. Review {review_path}")
    if source_type == "library" and not error_count:
        # Against the UNFILTERED backlog: all_pending is narrowed by any active
        # filter, so subtracting it from the whole manifest once reported
        # "77753 of 77753" after a 54-photo filtered batch, with ~54.5k photos
        # never processed. Coverage claims in this project have to be exact.
        done = len(items) - truly_pending_count + len(pending)
        print(f"Library progress: {done} of {len(items)} manifest photos ({library_order})")
    if circuit_broken:
        return EXIT_PHOTOS_HUNG
    return 1 if error_count else 0


def generated_tags_by_photo(
    records: Iterable[dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Per photo, the union of tags this run actually ADDED — never tags that
    already existed.

    A generated tag that coincided with a pre-existing keyword (the model says
    "bird"; the user hand-tagged "bird" years ago) was a no-op at write time,
    so removing it on rollback would delete the user's own keyword. Each
    record therefore contributes only its own additions:

    - applied: keywords_after − keywords_before (the sync script's atomic
      read makes these authoritative), intersected with generated_keywords.
    - write-pending (no authoritative after): generated_keywords −
      keywords_before, but ONLY when no applied record for that photo follows
      it. An applied record supersedes the journal entry that preceded it,
      because the journal is written before Photos is consulted and therefore
      cannot know which keywords already existed. Counting both over-claims:
      `tag-folder` journals without any before-list at all, so its
      write-pending records asserted that every location keyword was new, and
      rolling back runs/20260817-090511-Around-the-World would have deleted
      3,206 keywords it never added.

    A retried photo is handled naturally: attempt 1's record contributes the
    tag it added; attempt 2's before-list already contains it, so attempt 2
    contributes nothing — but attempt 1's contribution keeps it removable.

    write-pending records count because the crash window between the journal
    entry and the applied record means the write may or may not have reached
    Photos — removal is idempotent either way.
    """
    union: dict[str, dict[str, object]] = {}
    # Contributions from journal entries whose write has not yet been
    # confirmed by an applied record. Superseded — and discarded — as soon as
    # one arrives for that photo.
    unconfirmed: dict[str, list[tuple[list[str], str]]] = {}
    for record in records:
        status = str(record.get("status", ""))
        if status not in {"applied", "write-pending"}:
            continue
        generated = [str(k) for k in record.get("generated_keywords", [])]
        before_fold = {str(k).casefold() for k in record.get("keywords_before", [])}
        if status == "applied":
            after = [str(k) for k in record.get("keywords_after", [])]
            generated_fold = {g.casefold() for g in generated}
            added = [
                k for k in after
                if k.casefold() not in before_fold and k.casefold() in generated_fold
            ]
        else:
            added = [g for g in generated if g.casefold() not in before_fold]
        photo_id = str(record.get("photo_id", ""))
        # A journal entry that recorded no before-list at all cannot say which
        # keywords were new — absent is not the same as empty. Defer it: if an
        # applied record follows, its atomic read is authoritative; if none
        # does, the write may still have landed, so it stays removable.
        if status == "applied":
            unconfirmed.pop(photo_id, None)
        elif "keywords_before" not in record:
            if added:
                unconfirmed.setdefault(photo_id, []).append(
                    (added, str(record.get("filename", "")))
                )
            continue
        if not added:
            continue
        entry = union.setdefault(
            photo_id,
            {
                "filename": record.get("filename", ""),
                "generated_keywords": [],
                "photo_id": photo_id,
            },
        )
        entry["generated_keywords"] = merge_keywords(
            entry["generated_keywords"], added
        )
        entry["filename"] = record.get("filename", entry["filename"])
    # Journal entries that never saw an applied record: the write may or may
    # not have reached Photos, so they stay removable.
    for photo_id, contributions in unconfirmed.items():
        for added, filename in contributions:
            entry = union.setdefault(
                photo_id,
                {"filename": filename, "generated_keywords": [], "photo_id": photo_id},
            )
            entry["generated_keywords"] = merge_keywords(
                entry["generated_keywords"], added
            )
    return {pid: e for pid, e in union.items() if e["generated_keywords"]}


def rollback_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    metadata = load_run(run_dir)
    if not metadata.get("apply"):
        raise RuntimeError("This was a dry run; it did not change Photos")
    with run_lock(run_dir):
        return run_rollback(run_dir, metadata)


def run_rollback(run_dir: Path, metadata: dict[str, object]) -> int:
    source_type = str(metadata.get("source", "album"))
    album = str(metadata["album"])
    targets = list(generated_tags_by_photo(read_jsonl(run_dir / "results.jsonl")).values())
    audit_path = run_dir / f"rollback-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    print(
        f"Removing this run's generated keywords from {len(targets)} photos in {album!r}"
    )
    print("Keywords added manually in Photos are left untouched")
    errors = 0
    not_found = 0
    album_inventory: dict[str, PhotoItem] = {}
    if targets and source_type == "album":
        album_inventory = {item.identifier: item for item in inventory_album(album)}
    for index, entry in enumerate(targets, start=1):
        photo_id = str(entry["photo_id"])
        generated = list(entry["generated_keywords"])
        audit: dict[str, object] = {
            "filename": entry.get("filename", ""),
            "keywords_removed": generated,
            "photo_id": photo_id,
            "timestamp": utc_now(),
        }
        try:
            if source_type == "library":
                before, after = remove_library_keywords(photo_id, generated)
            else:
                if photo_id not in album_inventory:
                    raise PhotoNotFoundError(f"photo no longer in album: {photo_id}")
                before, after = remove_keywords(album, photo_id, generated)
            audit["keywords_before"] = before
            audit["keywords_after"] = after
            # The remove AppleScript returns the exact list it wrote; confirm
            # none of this run's tags survived it.
            if any(
                str(value).casefold() in {str(g).casefold() for g in generated}
                for value in after
            ):
                raise RuntimeError("Photos still holds generated tags after removal")
            audit["status"] = "removed"
            print(f"[{index}/{len(targets)}] cleaned {entry.get('filename', photo_id)}")
        except PhotoNotFoundError:
            not_found += 1
            audit["status"] = "not-found"
            print(
                f"[{index}/{len(targets)}] {entry.get('filename', photo_id)}: "
                "no longer in the library"
            )
        except Exception as error:
            errors += 1
            audit.update({"error": str(error), "status": "error"})
            print(
                f"ERROR cleaning {entry.get('filename', photo_id)}: {error}",
                file=sys.stderr,
            )
        append_jsonl(audit_path, audit)
    summary = f"Cleaned {len(targets) - errors - not_found} of {len(targets)} photos"
    if not_found:
        summary += f"; {not_found} no longer present"
    print(f"{summary}; {errors} error(s)")
    if targets:
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
    manifest = load_manifest(run_dir)
    if manifest and args.start is None and args.end is None:
        # Manifest runs are id-addressed, so coverage is a pure local diff:
        # every manifest photo must have a record. No AppleEvents needed.
        missing_entries = [
            entry for entry in manifest if str(entry.get("photo_id", "")) not in processed
        ]
        report_path = run_dir / f"coverage-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        with report_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["capture_date", "filename", "photo_id"])
            for entry in missing_entries:
                writer.writerow(
                    [
                        entry.get("capture_date", ""),
                        entry.get("filename", ""),
                        entry.get("photo_id", ""),
                    ]
                )
        print(
            f"{len(missing_entries)} of {len(manifest)} manifest photos have no record in this run"
        )
        if unresolved:
            print(
                f"{len(unresolved)} recorded items ended in a retryable state "
                "(error, verify-failed, or write-pending); resume the run to retry them"
            )
        print(f"Report: {report_path}")
        return 1 if missing_entries or unresolved else 0
    total = library_count()
    expected_total = int(metadata.get("total_count", metadata.get("manifest_total", 0)))
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
    with run_lock(run_dir):
        manifest_path = run_dir / "manifest.jsonl"
        if not manifest_path.exists():
            raise RuntimeError(
                "this run predates id-based traversal; migrate it first: "
                f"python3 scripts/migrate_run_to_manifest.py {run_dir}"
            )
        previous_order = str(metadata.get("order", "ascending"))
        if args.order != previous_order:
            # The manifest fixes the photo set; changing order just reverses
            # the traversal sequence. Pending computation is unaffected.
            entries = load_manifest(run_dir)
            entries.reverse()
            temp = run_dir / "manifest.jsonl.tmp"
            if temp.exists():
                temp.unlink()
            for entry in entries:
                append_jsonl(temp, entry)
            temp.replace(manifest_path)
        history = list(metadata.get("order_history", []))
        history.append(
            {
                "changed_at": utc_now(),
                "from": previous_order,
                "to": args.order,
            }
        )
        metadata["order_history"] = history
        metadata["order"] = args.order
        metadata["order_changed_at"] = utc_now()
        save_run(run_dir, metadata)
    print(
        f"Library order set to {args.order} (manifest traversal reversed). "
        "Existing results were preserved."
    )
    return 0


def rename_prefix_command(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    metadata = load_run(run_dir)
    if not metadata.get("apply"):
        raise RuntimeError("Prefixes can only be renamed from an applied run")
    if metadata.get("source") == "library":
        raise RuntimeError(
            "rename-prefix only supports album runs; whole-library runs are not supported"
        )
    with run_lock(run_dir):
        return run_rename_prefix(run_dir, metadata, args)


def run_rename_prefix(
    run_dir: Path, metadata: dict[str, object], args: argparse.Namespace
) -> int:
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


def sample_photo_for_test(album: str | None) -> PhotoItem:
    """One photo to validate a backend against — never mutated, only exported."""
    if album:
        items = inventory_album(album)
        if not items:
            raise RuntimeError(f"album {album!r} has no photos to test with")
        return items[0]
    items = inventory_library_batch(1, 1, "ascending", timeout=300)
    if not items:
        raise RuntimeError("the Photos library appears to be empty")
    return items[0]


def test_backend_command(args: argparse.Namespace) -> int:
    """Classify exactly one photo and print the result.

    This exists so a bad credential or an unsupported parameter costs one API
    call to discover, not a nine-thousand-photo run. Run it before every bulk
    run against a new provider, model, or key.
    """
    backend = args.backend
    api_base = normalize_api_base(args.api_base)

    if args.list_models:
        if backend != "openai-compatible":
            raise RuntimeError("--list-models only applies to --backend openai-compatible")
        names = list_openai_compatible_models(api_base)
        if not names:
            print("the provider returned no models")
            return 1
        print(f"{len(names)} models available at {api_base}:")
        for name in names:
            print(f"  {name}")
        return 0

    model = resolve_model(backend, args.model)
    album_label = args.album or "Photos Library"

    # Fail on a missing or malformed credential before exporting or converting
    # anything — that is the whole point of this subcommand.
    require_backend_credential(backend, api_base)

    if args.image:
        image_path = Path(args.image).expanduser().resolve()
        if not image_path.is_file():
            raise RuntimeError(f"no such image: {image_path}")
        print(f"Backend: {backend}   Model: {model or '(apple vision)'}")
        if backend == "openai-compatible":
            print(f"Endpoint: {api_base}   Credential: {provider_slug(api_base)}")
        print(f"Photo: {image_path}")
        analysis = classify(
            image_path,
            backend=backend,
            model=model,
            album=album_label,
            maximum=args.max_tags,
            api_base=api_base,
        )
        return report_test_analysis(analysis, args)

    item = sample_photo_for_test(args.album)
    print(f"Backend: {backend}   Model: {model or '(apple vision)'}")
    if backend == "openai-compatible":
        print(f"Endpoint: {api_base}   Credential: {provider_slug(api_base)}")
    print(f"Photo: {item.filename} ({item.identifier})")
    with tempfile.TemporaryDirectory(prefix="phototagger-test-") as temp:
        image_path = (
            export_photo(args.album, item, Path(temp))
            if args.album
            else export_library_photo_by_id(item, Path(temp))
        )
        analysis = classify(
            image_path,
            backend=backend,
            model=model,
            album=album_label,
            maximum=args.max_tags,
            api_base=api_base,
        )
    return report_test_analysis(analysis, args)


def report_test_analysis(analysis: dict[str, object], args: argparse.Namespace) -> int:
    classifications = list(analysis["classifications"])
    determinations = refine_determinations(
        classifications, dict(analysis["determinations"])
    )
    descriptive = normalized_tags(
        classifications, confidence=args.confidence, maximum=args.max_tags, prefix=args.prefix
    )
    generated = merge_keywords(
        descriptive, determination_keywords(determinations, prefix=args.prefix)
    )
    print("\nRaw labels:")
    for entry in classifications:
        print(f"  {entry['label']} ({float(entry['confidence']):.2f})")
    print("\nDeterminations:")
    for key, value in determinations.items():
        print(f"  {key}: {value}")
    print("\nKeywords this backend would write:")
    if generated:
        for keyword in generated:
            print(f"  {keyword}")
    else:
        print("  (none — nothing passed the confidence filter)")
    print("\nBackend OK. Nothing was written to Photos.")
    return 0


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
        choices=BACKEND_CHOICES,
        default="ollama",
        help="ollama (default, local Gemma) | openai-compatible (any provider "
        "speaking the OpenAI chat API; use --api-base) | anthropic (Claude API) | "
        "apple (local Vision). Validate with 'test-backend' before a bulk run.",
    )
    tag.add_argument(
        "--api-base",
        default=DEFAULT_OPENAI_API_BASE,
        help="base URL for --backend openai-compatible, ending at /v1 "
        f"(default: {DEFAULT_OPENAI_API_BASE})",
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
    tag.add_argument(
        "--model",
        default=None,
        help="vision model (default for ollama: gemma4:e4b-it-qat; "
        "required for the anthropic backend, e.g. claude-sonnet-5)",
    )
    tag.add_argument(
        "--order",
        choices=("ascending", "descending"),
        default="ascending",
        help="whole-library traversal order (default: ascending)",
    )
    tag.add_argument(
        "--only-extensions",
        default=None,
        help="process only these file types this run, e.g. CR2,CR3. Excluded "
        "photos stay pending, so dropping the filter later resumes them.",
    )
    tag.add_argument(
        "--only-filenames",
        default=None,
        help="process only photos whose filename contains one of these "
        "case-insensitive substrings this run, e.g. DJI,dji_fly. Excluded "
        "photos stay pending, exactly as with --only-extensions.",
    )
    tag.add_argument("--prefix", default="")
    tag.add_argument("--resume", help="existing run directory")
    tag.add_argument("--runs-dir", default=str(ROOT / "runs"))

    test_backend = subparsers.add_parser(
        "test-backend",
        help="classify ONE photo and print the tags — run this before any bulk run",
    )
    test_backend.add_argument(
        "--backend", choices=BACKEND_CHOICES, default="openai-compatible"
    )
    test_backend.add_argument("--model", default=None)
    test_backend.add_argument(
        "--api-base",
        default=DEFAULT_OPENAI_API_BASE,
        help=f"base URL ending at /v1 (default: {DEFAULT_OPENAI_API_BASE})",
    )
    test_backend.add_argument(
        "--list-models",
        action="store_true",
        help="list the provider's model names via GET /v1/models and exit",
    )
    test_backend.add_argument(
        "--image",
        default=None,
        help="classify this local image file instead of touching Photos "
        "(the quickest way to validate a key)",
    )
    test_backend.add_argument(
        "--album", default=None, help="sample the first photo of this album instead of the library"
    )
    test_backend.add_argument("--max-tags", type=int, default=5)
    test_backend.add_argument("--confidence", type=float, default=0.65)
    test_backend.add_argument("--prefix", default="")

    tag_folder = subparsers.add_parser(
        "tag-folder",
        help="write a Photos folder's album names and ancestry as keywords "
        "(e.g. location albums under a places folder)",
    )
    tag_folder.add_argument(
        "--folder", required=True, help="Photos folder name (searched at any depth)"
    )
    tag_folder.add_argument(
        "--apply", action="store_true", help="write keywords into Photos"
    )
    tag_folder.add_argument(
        "--rename",
        action="append",
        metavar="OLD=NEW",
        help="rewrite a folder/album name before keywording (repeatable)",
    )
    tag_folder.add_argument("--runs-dir", default="runs", help=argparse.SUPPRESS)

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
        if args.command == "test-backend":
            if args.max_tags < 1:
                raise RuntimeError("--max-tags must be positive")
            if not 0.0 <= args.confidence <= 1.0:
                raise RuntimeError("--confidence must be between 0 and 1")
            return test_backend_command(args)
        if args.command == "tag-folder":
            return tag_folder_command(args)
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
