"""Extract exact story portrait sprites from an installed game."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_bytes, atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

INDEX_SCHEMA = "r1999.story-portrait-sources"
INDEX_VERSION = 1
PORTRAIT_BUNDLE_NAMES = (
    hashlib.md5(b"singlebg/headicon_room", usedforsecurity=False).hexdigest() + ".dat",
    # ponytail: current content-addressed update shards; fallback scan covers new shards.
    "ac7537266c12805233251be3166bcf13.dat",
    "f1b6e5893c3d4c416aefa5b88a18b8eb.dat",
)


class StoryPortraitError(RuntimeError):
    """Installed portrait sprites cannot be extracted safely."""


def extract_story_portraits(
    bundle_directory,
    portraits,
    output_directory,
    *,
    cache_key=None,
    environment_loader=None,
):
    """Extract requested exact Sprite names and return filename-to-SHA-256."""
    bundle_directory = Path(bundle_directory).expanduser().resolve()
    output_directory = Path(output_directory).expanduser().resolve()
    if not bundle_directory.is_dir():
        raise StoryPortraitError(f"Installed bundle directory is missing: {bundle_directory}")
    requested = {_portrait_filename(value) for value in portraits if value is not None}
    if not requested:
        return {}
    output_directory.mkdir(parents=True, exist_ok=True)
    index_path = output_directory / "source-index.json"
    sources = _load_sources(index_path, cache_key)
    loader = environment_loader or _load_environment
    payloads = {}
    attempted = set()
    missing = set(requested)

    cached_bundles = {
        sources[filename] for filename in requested if filename in sources
    }
    for bundle_name in sorted(cached_bundles):
        bundle = bundle_directory / bundle_name
        found = _portraits_from_bundle(bundle, missing, loader)
        payloads.update(found)
        missing -= found.keys()
        attempted.add(bundle)

    for bundle_name in PORTRAIT_BUNDLE_NAMES:
        preferred = bundle_directory / bundle_name
        if not missing or not preferred.is_file() or preferred in attempted:
            continue
        found = _portraits_from_bundle(preferred, missing, loader)
        for filename, payload in found.items():
            payloads[filename] = payload
            sources[filename] = preferred.name
        missing -= found.keys()
        attempted.add(preferred)
    for bundle in sorted(bundle_directory.glob("*.dat")):
        if not missing or bundle in attempted:
            continue
        found = _portraits_from_bundle(bundle, missing, loader)
        for filename, payload in found.items():
            payloads[filename] = payload
            sources[filename] = bundle.name
        missing -= found.keys()

    hashes = {}
    for filename, payload in payloads.items():
        destination = output_directory / filename
        atomic_write_bytes(destination, payload)
        hashes[filename] = sha256_file(destination)
    atomic_write_json(
        index_path,
        {
            "schema": INDEX_SCHEMA,
            "schema_version": INDEX_VERSION,
            "cache_key": cache_key,
            "sources": dict(sorted(sources.items())),
        },
    )
    return hashes


def _portrait_filename(value):
    text = str(value).strip()
    if not text or "\\" in text or Path(text).name != text:
        raise StoryPortraitError(f"Unsafe story portrait identity: {value!r}")
    path = Path(text)
    if path.suffix and path.suffix.casefold() != ".png":
        raise StoryPortraitError(f"Story portrait must be a PNG identity: {value!r}")
    return path.with_suffix(".png").name


def _load_sources(path, cache_key):
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    sources = document.get("sources")
    if (
        document.get("schema") != INDEX_SCHEMA
        or document.get("schema_version") != INDEX_VERSION
        or document.get("cache_key") != cache_key
        or not isinstance(sources, dict)
    ):
        return {}
    return {
        filename: bundle
        for filename, bundle in sources.items()
        if isinstance(filename, str)
        and isinstance(bundle, str)
        and _safe_leaf(filename)
        and _safe_leaf(bundle)
    }


def _safe_leaf(value):
    return bool(value) and "\\" not in value and Path(value).name == value


def _load_environment(bundle):
    try:
        import UnityPy
    except ImportError as error:
        raise StoryPortraitError("UnityPy is required for portrait extraction") from error
    try:
        payload = Path(bundle).read_bytes()
        header = payload.find(b"UnityFS")
        if header < 0:
            return None
        return UnityPy.load(payload[header:])
    except Exception:
        return None


def _portraits_from_bundle(bundle, filenames, loader):
    try:
        environment = loader(bundle)
        if environment is None:
            return {}
        targets = {Path(filename).stem: filename for filename in filenames}
        found = {}
        for value in environment.objects:
            if value.type.name != "Sprite":
                continue
            sprite = value.read()
            filename = targets.get(str(sprite.m_Name))
            if filename is None:
                continue
            buffer = io.BytesIO()
            sprite.image.save(buffer, format="PNG")
            found[filename] = buffer.getvalue()
        return found
    except Exception:
        return {}
