"""Portable, source-only Reverse: 1999 game-pack export."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from vntts_artifacts.game_pack import GamePackError, load_game_pack, write_game_pack
from vntts_artifacts.live_sequence import LiveSequencePlanError, load_live_sequence_plan
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    write_voice_manifest,
)

from r1999extractor import __version__
from r1999extractor.cli import cli_error

GAME_ID = "reverse1999"
PACK_MANIFEST_NAME = "game-pack.json"


class SourceGamePackError(RuntimeError):
    pass


def export_source_game_pack(
    output_directory,
    *,
    story_index,
    voice_manifest,
    game_version,
    live_sequence_plan=None,
    created_at=None,
):
    """Export a new portable source pack and return its validated contract.

    The source story index is copied byte-for-byte. The v2 voice manifest and
    all of its safe relative WAV references are copied under ``voice/``. The
    shared artifact library derives and validates every SHA-256 binding.
    """
    output_directory = Path(output_directory).expanduser().resolve()
    story_index = Path(story_index).expanduser().resolve()
    voice_manifest = Path(voice_manifest).expanduser().resolve()
    live_sequence_plan = (
        None if live_sequence_plan is None else Path(live_sequence_plan).expanduser().resolve()
    )
    game_version = str(game_version).strip()
    if not game_version:
        raise SourceGamePackError("Game version must be non-empty text")
    if output_directory.exists():
        raise SourceGamePackError(f"Output directory already exists: {output_directory}")

    try:
        manifest, entries = load_voice_manifest(voice_manifest, allow_legacy=False)
        if live_sequence_plan is not None:
            plan = load_live_sequence_plan(live_sequence_plan, story_index)
            if plan.game_id != GAME_ID:
                raise SourceGamePackError("Live sequence plan belongs to a different game")
        references = _reference_sources(voice_manifest, entries)
        output_directory.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix=f".{output_directory.name}-",
            dir=output_directory.parent,
        ) as temporary_directory:
            staging = Path(temporary_directory)
            staged_story = staging / "story-index.jsonl"
            staged_manifest = staging / "voice" / "manifest.json"
            staged_sequence = (
                staging / "live-sequence.json" if live_sequence_plan is not None else None
            )
            staged_manifest.parent.mkdir(parents=True)
            shutil.copy2(story_index, staged_story)
            if staged_sequence is not None:
                shutil.copy2(live_sequence_plan, staged_sequence)
            write_voice_manifest(staged_manifest, manifest)
            for relative, source in references.items():
                destination = staged_manifest.parent / Path(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

            components = {
                "story_index": staged_story,
                "voice_manifest": staged_manifest,
            }
            if staged_sequence is not None:
                components["live_sequence_plan"] = staged_sequence
            write_game_pack(
                staging / PACK_MANIFEST_NAME,
                {
                    "game": {"id": GAME_ID, "version": game_version},
                    "producers": [{"name": "reverse1999-extractor", "version": __version__}],
                    "created_at": created_at or datetime.now(timezone.utc).isoformat(),
                },
                components,
            )
            if output_directory.exists():
                raise SourceGamePackError(
                    f"Output directory appeared during export: {output_directory}"
                )
            staging.rename(output_directory)

        return load_game_pack(output_directory / PACK_MANIFEST_NAME)
    except SourceGamePackError:
        raise
    except (GamePackError, LiveSequencePlanError, VoiceManifestError, OSError) as error:
        raise SourceGamePackError(str(error)) from error


def _reference_sources(manifest_path, entries):
    sources = {}
    for entry in entries:
        for value in entry.references:
            relative = _safe_reference(value)
            source = (manifest_path.parent / Path(*relative.parts)).resolve()
            if source.suffix.casefold() != ".wav":
                raise SourceGamePackError(f"Voice reference is not a WAV file: {source}")
            if not source.is_file():
                raise SourceGamePackError(f"Voice reference does not exist: {source}")
            sources[relative] = source
    return sources


def _safe_reference(value):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise SourceGamePackError("Voice reference must be a safe POSIX-relative path")
    relative = PurePosixPath(value.strip())
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise SourceGamePackError("Voice reference must be a safe POSIX-relative path")
    return relative


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Export a portable, checksum-bound Reverse: 1999 source game pack. "
            "Generated audio is intentionally excluded."
        )
    )
    parser.add_argument("--story-index", type=Path, required=True)
    parser.add_argument("--voice-manifest", type=Path, required=True)
    parser.add_argument("--live-sequence-plan", type=Path)
    parser.add_argument("--game-version", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New pack directory")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        pack = export_source_game_pack(
            options.output,
            story_index=options.story_index,
            voice_manifest=options.voice_manifest,
            live_sequence_plan=options.live_sequence_plan,
            game_version=options.game_version,
        )
    except SourceGamePackError as error:
        return cli_error(error)
    summary = {
        "game": {"id": pack.game_id, "version": pack.game_version},
        "manifest": str(pack.manifest_path),
        "producer": {"name": pack.producers[0].name, "version": pack.producers[0].version},
        "story_index": str(pack.story_index.path),
        "voice_manifest": str(pack.voice_manifest.path),
        "voice_reference_count": len(pack.voice_wavs),
        "live_sequence_plan": (
            str(pack.live_sequence_plan.path) if pack.live_sequence_plan is not None else None
        ),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
