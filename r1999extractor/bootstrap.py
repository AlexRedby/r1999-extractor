import argparse
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json

from r1999extractor.cli import cli_error
from r1999extractor.generation_queue import (
    build_generation_queue,
    load_story_records,
    write_generation_queue,
)
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
from r1999extractor.structured_story import audit_story_like_tables


class BootstrapError(RuntimeError):
    pass


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

    progress("Building generation queue")
    _metadata, records = load_story_records(story_path)
    queue = build_generation_queue(records)
    queue_path, queue_metadata = write_generation_queue(
        queue,
        story_path,
        output / "generation-queue.jsonl",
    )
    audit = audit_story_like_tables(language, tables)
    audit.update({"schema": "r1999.story-source-audit", "schema_version": 1})
    audit_path = atomic_write_json(output / "story-source-audit.json", audit, sort_keys=True)
    return {
        "bank_index": bank_index_path,
        "catalog": catalog_path,
        "story_index": story_path,
        "generation_queue": queue_path,
        "source_audit": audit_path,
        "story_line_count": len(lines),
        "generation_item_count": queue_metadata["item_count"],
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
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
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
    print(
        f"Built {result['story_line_count']} story lines and "
        f"{result['generation_item_count']} generation items"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
