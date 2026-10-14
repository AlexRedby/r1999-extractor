"""Extract exact story portrait sprites from an installed game."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zlib
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_bytes, atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from r1999extractor.source_audio_progress import report_source_audio_progress

INDEX_SCHEMA = "r1999.story-portrait-sources"
INDEX_VERSION = 1
PORTRAIT_BUNDLE_NAMES = (
    hashlib.md5(b"singlebg/headicon_room", usedforsecurity=False).hexdigest() + ".dat",
    # Legacy bundle hints; manifest routing discovers update shards.
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
    progress=None,
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
    missing = set(requested)
    manifest_bundles = _manifest_portrait_bundles(bundle_directory, requested)
    unknown = requested if manifest_bundles is None else requested - manifest_bundles.keys()
    mapped_bundles = set() if manifest_bundles is None else set().union(*manifest_bundles.values())
    cached_bundles = {sources[filename] for filename in requested if filename in sources}
    names = dict.fromkeys(
        name
        for name in (*sorted(cached_bundles), *PORTRAIT_BUNDLE_NAMES, *sorted(mapped_bundles))
        if unknown or name in mapped_bundles
    )
    bundles = [bundle_directory / name for name in names if (bundle_directory / name).is_file()]
    fallback = (
        [bundle for bundle in sorted(bundle_directory.glob("*.dat")) if bundle.name not in names]
        if unknown
        else []
    )
    processed = 0
    total = len(bundles) + len(fallback)
    report_source_audio_progress(progress, "portraits", processed, total)
    for candidates, allowed in ((bundles, requested), (fallback, unknown)):
        for bundle in candidates:
            targets = missing & allowed
            if not targets:
                break
            targets = {
                filename
                for filename in targets
                if filename in unknown or bundle.name in manifest_bundles[filename]
            }
            if not targets:
                continue
            found = _portraits_from_bundle(bundle, targets, loader)
            for filename, payload in found.items():
                payloads[filename] = payload
                sources[filename] = bundle.name
            missing -= found.keys()
            processed += 1
            report_source_audio_progress(progress, "portraits", processed, total)

    hashes = {}
    report_source_audio_progress(progress, "portraits", processed, total, "publishing")
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


def _manifest_portrait_bundles(bundle_directory, requested):
    root = bundle_directory.parent
    manifests = [root / "configs" / "allmanifest.dat"]
    if root.name in {"Windows", "PersistentRoot"} and root.parent.name == "StreamingAssets":
        sibling = "PersistentRoot" if root.name == "Windows" else "Windows"
        manifests.append(root.parent / sibling / "configs" / "allmanifest.dat")
    for manifest in manifests:
        bundles = _portrait_bundles_from_manifest(manifest, requested)
        if bundles is not None:
            return bundles
    return None


def _portrait_bundles_from_manifest(manifest, requested):
    try:
        with gzip.open(manifest, "rt", encoding="utf-8") as stream:
            entries = json.load(stream)
        if not isinstance(entries, list):
            return None
        bundles = {}
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            name = entry.get("abName")
            assets = entry.get("assets")
            if (
                not isinstance(name, str)
                or not _safe_asset_path(name)
                or not isinstance(assets, list)
                or any(
                    not isinstance(asset, str) or not _safe_asset_path(asset) for asset in assets
                )
            ):
                return None
            bundle = hashlib.md5(name.encode("utf-8"), usedforsecurity=False).hexdigest() + ".dat"
            for asset in assets:
                filename = Path(asset).name
                if filename in requested:
                    bundles.setdefault(filename, set()).add(bundle)
        return bundles
    except (OSError, UnicodeError, json.JSONDecodeError, EOFError, zlib.error):
        return None


def _safe_asset_path(value):
    path = Path(value)
    return (
        bool(path.parts)
        and value == value.strip()
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
    )


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
