import argparse
import hashlib
import json
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from r1999extractor.cli import cli_error
from r1999extractor.reverse1999_catalog import (
    _load_overlay,
    build_catalog_document,
    default_overlay_path,
    write_catalog,
)
from r1999extractor.reverse1999_config import (
    find_game_config_directory,
    load_config_directory,
)
from r1999extractor.reverse1999_index import build_bank_index
from r1999extractor.reverse1999_voice_import import (
    find_game_audio_directory,
)
from r1999extractor.settings import get_local_data_directory
from r1999extractor.story_audio import StoryAudioResolver, build_audio_registry
from r1999extractor.story_index import (
    enrich_story_sources,
    extract_story_lines,
    find_story_bundle,
    resolve_story_audio,
    write_story_index,
)
from r1999extractor.story_portraits import StoryPortraitError, extract_story_portraits
from r1999extractor.story_voice_candidates import (
    REPORT_SCHEMA,
    SUPPORTED_REPORT_VERSIONS,
    build_story_voice_candidates,
)
from r1999extractor.structured_story import audit_story_like_tables

PLAYER_VOICE_CANDIDATES_FIELD = "vntts.player.voice_candidates"
PLAYER_VOICE_CANDIDATES_SCHEMA = "vntts.player-voice-candidates"
PLAYER_VOICE_CANDIDATES_VERSION = 2


class BootstrapError(RuntimeError):
    pass


def prepare_player_voice_candidates(*, roles, data_directory=None):
    """Build or reuse safe, selected-role candidates for the player workflow."""
    roles = tuple(
        sorted(
            {str(role).strip() for role in roles if str(role).strip()},
            key=lambda value: normalize_character_name(value),
        )
    )
    if not roles:
        raise BootstrapError("At least one voice candidate role is required")
    output = (
        Path(data_directory or get_local_data_directory()).expanduser().resolve()
        / "reverse1999"
    )
    story_index = output / "story-index.jsonl"
    bank_index = output / "english-bank-index.json"
    if not story_index.is_file() or not bank_index.is_file():
        raise BootstrapError("Import the installed game before preparing character voices")
    story_sha256 = sha256_file(story_index)
    identity = hashlib.sha256(
        json.dumps(
            {
                "story_index_sha256": story_sha256,
                "roles": [normalize_character_name(role) for role in roles],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    directory = output / "voice-candidates" / identity
    report_path = directory / "report.json"
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest, _entries = load_voice_manifest(manifest_path, allow_legacy=False)
            evidence = manifest.get(PLAYER_VOICE_CANDIDATES_FIELD, {})
            if (
                evidence.get("story_index_sha256") == story_sha256
                and evidence.get("schema_version") == PLAYER_VOICE_CANDIDATES_VERSION
            ):
                return manifest_path
        except (OSError, RuntimeError, ValueError):
            pass
    try:
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            report_path, report = build_story_voice_candidates(
                story_index,
                bank_index,
                roles,
                directory,
            )
        return _publish_player_voice_manifest(
            report_path,
            report,
            manifest_path,
            story_index,
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise BootstrapError(f"Unable to prepare character voice candidates: {error}") from error


def _publish_player_voice_manifest(report_path, report, manifest_path, story_index):
    story_index = Path(story_index).resolve()
    if (
        not isinstance(report, dict)
        or report.get("schema") != REPORT_SCHEMA
        or report.get("schema_version") not in SUPPORTED_REPORT_VERSIONS
        or Path(report.get("story_index", "")).expanduser().resolve() != story_index
        or report.get("story_index_sha256") != sha256_file(story_index)
    ):
        raise BootstrapError("Voice candidate report story index changed")
    recommended = {
        (
            group.get("character"),
            group.get("portrait"),
            group.get("source_bank"),
            media_id,
        )
        for group in report.get("groups", ())
        if isinstance(group, dict)
        for media_id in group.get("recommended_media_ids_for_audition", ())
    }
    portrait_hashes = _prepare_player_portraits(
        story_index,
        {
            candidate.get("portrait")
            for candidate in report.get("candidates", ())
            if (
                candidate.get("character"),
                candidate.get("portrait"),
                candidate.get("source_bank"),
                candidate.get("media_id"),
            )
            in recommended
        },
    )
    voices = []
    variants = []
    root = Path(report_path).resolve().parent
    for candidate in report.get("candidates", ()):
        identity = (
            candidate.get("character"),
            candidate.get("portrait"),
            candidate.get("source_bank"),
            candidate.get("media_id"),
        )
        if identity not in recommended:
            continue
        reference = str(candidate.get("reference") or "").strip()
        reference_path = (root / reference).resolve()
        try:
            reference_path.relative_to(root)
        except ValueError as error:
            raise BootstrapError("Voice candidate reference escapes its report") from error
        reference_sha256 = str(candidate.get("reference_sha256") or "").strip()
        if reference_path.is_symlink() or sha256_file(reference_path) != reference_sha256:
            raise BootstrapError("Voice candidate reference changed before publication")
        variant_id = hashlib.sha256(
            json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        voice_character = (
            f"Player candidate {candidate['character']} {variant_id[:12]}"
        )
        voices.append(
            {
                "character": voice_character,
                "speaker": f"player-candidate:{variant_id}",
                "references": [reference],
            }
        )
        source_lines = candidate.get("source_lines", ())
        metrics = candidate.get("metrics", {})
        variants.append(
            {
                "variant_id": variant_id,
                "character": candidate["character"],
                "portrait": candidate.get("portrait"),
                "portrait_image_sha256": _portrait_hash(
                    portrait_hashes,
                    candidate.get("portrait"),
                ),
                "source_bank": candidate["source_bank"],
                "source_voice_ids": sorted(
                    {
                        str(line.get("source_audio_id") or "").strip()
                        for line in source_lines
                        if isinstance(line, dict)
                        and str(line.get("source_audio_id") or "").strip()
                    }
                ),
                "voice_character": voice_character,
                "reference_sha256": reference_sha256,
                "source_line_ids": sorted(
                    {
                        str(line.get("line_id") or "").strip()
                        for line in source_lines
                        if isinstance(line, dict) and str(line.get("line_id") or "").strip()
                    }
                ),
                "source_event_ids": candidate.get("source_event_ids", []),
                "duration_seconds": metrics.get("duration_seconds"),
                "quality_score": metrics.get("quality_score"),
            }
        )
    if not voices:
        raise BootstrapError("No technically usable character voice candidates were found")
    report_sha256 = sha256_file(report_path)
    manifest = {
        "version": 2,
        "game": "reverse1999",
        "language": "en",
        "voices": voices,
        PLAYER_VOICE_CANDIDATES_FIELD: {
            "schema": PLAYER_VOICE_CANDIDATES_SCHEMA,
            "schema_version": PLAYER_VOICE_CANDIDATES_VERSION,
            "story_index_sha256": report["story_index_sha256"],
            "candidate_report": report_path.name,
            "candidate_report_sha256": report_sha256,
            "variants": variants,
        },
    }
    write_voice_manifest(manifest_path, manifest)
    return manifest_path


def _prepare_player_portraits(story_index, portraits):
    try:
        with Path(story_index).open(encoding="utf-8") as source:
            metadata = json.loads(next(source))
        if not isinstance(metadata, dict):
            return {}
        source_bundle = metadata.get("source_bundle")
        if not isinstance(source_bundle, str) or not source_bundle.strip():
            return {}
        return extract_story_portraits(
            Path(source_bundle).expanduser().resolve().parent,
            portraits,
            Path(story_index).resolve().parent / "portraits",
            cache_key=sha256_file(story_index),
        )
    except (OSError, StopIteration, json.JSONDecodeError, StoryPortraitError):
        return {}


def _portrait_hash(hashes, portrait):
    if portrait is None:
        return None
    text = str(portrait).strip()
    if not text or "\\" in text or Path(text).name != text:
        return None
    return hashes.get(Path(text).with_suffix(".png").name)


def bootstrap_local_artifacts(
    *,
    resource_root=None,
    config_directory=None,
    game_audio_directory=None,
    data_directory=None,
    overlay_path=None,
    game_version="installed",
    progress=None,
):
    progress = progress or (lambda _message: None)
    data_directory = Path(data_directory or get_local_data_directory()).expanduser().resolve()
    output = data_directory / "reverse1999"
    output.mkdir(parents=True, exist_ok=True)
    config_directory = config_directory or find_game_config_directory()
    if config_directory is None:
        raise BootstrapError("Unable to find installed game configs")
    game_audio_directory = game_audio_directory or find_game_audio_directory()
    if game_audio_directory is None:
        raise BootstrapError("Unable to find installed English game audio")
    bundle = find_story_bundle(resource_root)
    if bundle is None:
        raise BootstrapError("Unable to find installed story bundle")

    progress("Loading encrypted configuration")
    language, tables = load_config_directory(config_directory)
    progress("Indexing installed Wwise banks")
    bank_index_path = output / "english-bank-index.json"
    bank_index, _path = build_bank_index(game_audio_directory, output=bank_index_path)
    progress("Building local NPC catalog")
    overlay_path = overlay_path or output / default_overlay_path.name
    catalog = build_catalog_document(
        language,
        tables,
        bank_index,
        overlay=_load_overlay(overlay_path),
        game_version=game_version,
    )
    catalog_path = write_catalog(catalog, output / "npc-catalog.json")

    progress("Extracting story and config-only dialogue")
    lines = extract_story_lines(bundle)
    lines = enrich_story_sources(lines, language, tables)
    resolver = StoryAudioResolver(build_audio_registry(tables), bank_index)
    lines = resolve_story_audio(lines, resolver)
    story_path = write_story_index(lines, output / "story-index.jsonl", bundle=bundle)

    progress("Auditing story source coverage")
    audit = audit_story_like_tables(language, tables)
    audit.update({"schema": "r1999.story-source-audit", "schema_version": 1})
    audit_path = atomic_write_json(output / "story-source-audit.json", audit, sort_keys=True)
    return {
        "bank_index": bank_index_path,
        "catalog": catalog_path,
        "story_index": story_path,
        "source_audit": audit_path,
        "story_line_count": len(lines),
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description="Rebuild every local extractor artifact from an installed game."
    )
    parser.add_argument("--resource-root", type=Path)
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--game-audio-directory", type=Path)
    parser.add_argument("--data-directory", type=Path)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--game-version", default="installed")
    parser.add_argument("--prepare-voice-candidates-only", action="store_true")
    parser.add_argument("--voice-candidate-role", action="append", default=[])
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        if options.prepare_voice_candidates_only:
            manifest = prepare_player_voice_candidates(
                roles=options.voice_candidate_role,
                data_directory=options.data_directory,
            )
            print(json.dumps({"voice_manifest": str(manifest)}, sort_keys=True))
            return 0
        result = bootstrap_local_artifacts(
            resource_root=options.resource_root,
            config_directory=options.config_directory,
            game_audio_directory=options.game_audio_directory,
            data_directory=options.data_directory,
            overlay_path=options.overlay,
            game_version=options.game_version,
            progress=lambda message: print(message),
        )
    except (BootstrapError, OSError, ValueError) as error:
        return cli_error(error)
    print(f"Built {result['story_line_count']} story lines and source artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
