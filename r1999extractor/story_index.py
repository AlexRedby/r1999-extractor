import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index as write_story_index_document

from r1999extractor.reverse1999_aliases import canonical_voice_name
from r1999extractor.reverse1999_config import (
    Reverse1999ConfigError,
    find_game_config_directory,
    load_config_directory,
)
from r1999extractor.reverse1999_index import (
    Reverse1999IndexError,
    build_bank_index,
)
from r1999extractor.reverse1999_index import (
    default_output as default_bank_index,
)
from r1999extractor.reverse1999_voice_import import find_game_audio_directory
from r1999extractor.settings import get_local_data_directory
from r1999extractor.story_audio import (
    StoryAudioResolutionError,
    StoryAudioResolver,
    build_audio_registry,
)

story_asset_name = "configs/story"
story_bundle_filename = f"{hashlib.md5(story_asset_name.encode()).hexdigest()}.dat"
default_output = get_local_data_directory() / "reverse1999" / "story-index.jsonl"
rich_text_pattern = re.compile(r"<[^>]*>")
latin_pattern = re.compile(r"[A-Za-z]")
cjk_pattern = re.compile(r"[\u3400-\u9fff]")
ascii_word_pattern = re.compile(r"[A-Za-z]{2,}")


class Reverse1999StoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoryLine:
    record_type: str
    line_id: str
    chapter: str
    sequence: int
    speaker: str
    text: str
    source: str
    portrait: str | None
    source_voice_id: str | None
    source_voice_spec: str | None
    display_seconds: float | None
    kind: str
    voice_character: str
    text_sha256: str
    previous_text: str | None = None
    next_text: str | None = None
    speakable: bool = True
    filter_reason: str | None = None
    audio_status: str = "unchecked"
    audio_reason: str = "not_resolved"
    source_event: str | None = None
    source_bank: str | None = None
    source_media_ids: tuple[int, ...] = ()
    available_media_ids: tuple[int, ...] = ()
    source_kind: str = "story"
    story_group: str | None = None
    story_title: str | None = None
    episode_title: str | None = None
    story_order: int | None = None


def _localized(value, language_index):
    if not isinstance(value, list) or len(value) <= language_index:
        return ""
    localized = value[language_index]
    return localized.strip() if isinstance(localized, str) else ""


def clean_story_text(value):
    value = rich_text_pattern.sub("", str(value))
    return " ".join(value.split())


def classify_speakable_english(text, chapter):
    if not latin_pattern.search(text):
        return False, "no_latin_text"
    try:
        if int(chapter) < 1000:
            return False, "test_asset"
    except ValueError:
        pass
    if cjk_pattern.search(text) and len(ascii_word_pattern.findall(text)) < 3:
        return False, "mixed_language_placeholder"
    return True, None


def parse_story_document(document, source, *, language_index=2, include_non_speakable=False):
    if not isinstance(document, list) or len(document) < 3 or not isinstance(document[2], list):
        raise Reverse1999StoryError(f"Story asset {source} has an unsupported structure")
    chapter = source.removeprefix("json_story_step_")
    lines = []
    for position, step in enumerate(document[2]):
        if not isinstance(step, list) or len(step) < 3 or not isinstance(step[2], list):
            continue
        payload = step[2]
        if len(payload) <= 15:
            continue
        raw_text = _localized(payload[15], language_index)
        if not raw_text:
            continue
        text = clean_story_text(raw_text)
        speakable, filter_reason = classify_speakable_english(text, chapter)
        if not speakable and not include_non_speakable:
            continue
        speaker = _localized(payload[11], language_index) or "Narrator"
        try:
            sequence = int(step[0])
        except (TypeError, ValueError):
            sequence = position
        portrait = payload[13].strip() if isinstance(payload[13], str) else ""
        source_voice_id = payload[14].strip() if isinstance(payload[14], str) else ""
        display_seconds = None
        if isinstance(payload[1], list) and len(payload[1]) > language_index:
            value = payload[1][language_index]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                display_seconds = float(value)
        lines.append(
            StoryLine(
                record_type="line",
                line_id=f"reverse1999:{chapter}:{sequence}",
                chapter=chapter,
                sequence=sequence,
                speaker=speaker,
                text=text,
                source=source,
                portrait=portrait or None,
                source_voice_id=source_voice_id or None,
                source_voice_spec=source_voice_id or None,
                display_seconds=display_seconds,
                kind="narration" if speaker == "Narrator" else "dialogue",
                voice_character=canonical_voice_name(speaker) or speaker,
                text_sha256=text_sha256(text),
                speakable=speakable,
                filter_reason=filter_reason,
            )
        )
    return lines


def _language_text(language, key, fallback=""):
    value = language.get(key) if isinstance(key, str) else None
    if not isinstance(value, str) or not value.strip():
        value = fallback
    return clean_story_text(value) if isinstance(value, str) and value.strip() else ""


def annotate_anecdote_lines(lines, language, tables):
    """Classify story-step assets owned by the first-generation anecdote system."""
    anecdotes = {}
    for story_position, row in enumerate(tables.get("json_hero_story", []), start=1):
        if not isinstance(row, list) or len(row) <= 9:
            continue
        try:
            anecdote_chapter = int(row[1])
        except (TypeError, ValueError):
            continue
        if anecdote_chapter <= 0:
            continue
        anecdotes[anecdote_chapter] = {
            "story_title": _language_text(language, row[8], row[9]),
            "story_order": story_position,
        }

    by_source = {}
    episode_positions = defaultdict(int)
    for row in tables.get("json_episode", []):
        if not isinstance(row, list) or len(row) <= 7:
            continue
        try:
            anecdote_chapter = int(row[1])
            story_step = int(row[7])
        except (TypeError, ValueError):
            continue
        anecdote = anecdotes.get(anecdote_chapter)
        if anecdote is None or story_step <= 0:
            continue
        episode_positions[anecdote_chapter] += 1
        by_source[f"json_story_step_{story_step}"] = {
            **anecdote,
            "story_group": str(anecdote_chapter),
            "episode_title": _language_text(
                language,
                row[3] if len(row) > 3 else "",
                row[4] if len(row) > 4 else "",
            ),
            "episode_order": episode_positions[anecdote_chapter],
        }

    annotated = []
    for line in lines:
        metadata = by_source.get(line.source)
        if metadata is None:
            annotated.append(line)
            continue
        annotated.append(
            replace(
                line,
                source_kind="anecdote",
                story_group=metadata["story_group"],
                story_title=metadata["story_title"] or None,
                episode_title=metadata["episode_title"] or None,
                story_order=(metadata["story_order"] * 1_000) + metadata["episode_order"],
            )
        )
    return annotated


def extract_hero_story_plot_lines(language, tables, *, include_non_speakable=False):
    """Extract spoken and narratable text from config-only interactive hero stories."""
    hero_stories = {}
    for position, row in enumerate(tables.get("json_hero_story", []), start=1):
        if not isinstance(row, list) or len(row) <= 9:
            continue
        try:
            story_id = int(row[0])
        except (TypeError, ValueError):
            continue
        hero_stories[story_id] = {
            "title": _language_text(language, row[8], row[9]),
            "character": _language_text(language, row[4]),
            "order": position,
        }

    plot_groups = {}
    for row in tables.get("json_hero_story_plot_group", []):
        if not isinstance(row, list) or len(row) <= 7:
            continue
        try:
            group_id = int(row[0])
            story_id = int(row[1])
        except (TypeError, ValueError):
            continue
        story = hero_stories.get(story_id, {})
        plot_groups[group_id] = {
            "story_title": story.get("title") or _language_text(language, row[2], row[3]),
            "episode_title": _language_text(language, row[2], row[3]),
            "character": story.get("character") or _language_text(language, row[7]),
            "story_order": story.get("order", story_id),
        }

    allowed_types = {"dialog", "aside", "location"}
    lines = []
    group_positions = defaultdict(int)
    for row in tables.get("json_hero_story_plot", []):
        if not isinstance(row, list) or len(row) <= 5:
            continue
        plot_type = str(row[2]).strip()
        if plot_type not in allowed_types:
            continue
        try:
            plot_id = int(row[0])
            group_id = int(row[1])
        except (TypeError, ValueError):
            continue
        text = _language_text(language, row[5])
        if not text:
            continue
        speakable, filter_reason = classify_speakable_english(text, str(group_id))
        if not speakable and not include_non_speakable:
            continue
        group = plot_groups.get(group_id, {})
        raw_speaker = _language_text(language, row[4]) if plot_type == "dialog" else ""
        if "{roleName}" in raw_speaker:
            raw_speaker = raw_speaker.replace("{roleName}", group.get("character", ""))
        speaker = raw_speaker or "Narrator"
        group_positions[group_id] += 1
        lines.append(
            StoryLine(
                record_type="line",
                line_id=f"reverse1999:hero-story-plot:{plot_id}",
                chapter=str(group_id),
                sequence=plot_id,
                speaker=speaker,
                text=text,
                source="json_hero_story_plot",
                portrait=None,
                source_voice_id=None,
                source_voice_spec=None,
                display_seconds=None,
                kind="dialogue" if plot_type == "dialog" else "narration",
                voice_character=canonical_voice_name(speaker) or speaker,
                text_sha256=text_sha256(text),
                speakable=speakable,
                filter_reason=filter_reason,
                audio_status="no_audio",
                audio_reason="config_story_has_no_voice_cue",
                source_kind="hero_story_plot",
                story_group=str(group_id),
                story_title=group.get("story_title") or None,
                episode_title=group.get("episode_title") or None,
                story_order=(group.get("story_order", group_id) * 10_000)
                + group_positions[group_id],
            )
        )
    return add_story_context(lines)


def enrich_story_sources(lines, language, tables, *, include_non_speakable=False):
    from r1999extractor.structured_story import extract_structured_story_lines

    annotated = annotate_anecdote_lines(lines, language, tables)
    hero_lines = extract_hero_story_plot_lines(
        language,
        tables,
        include_non_speakable=include_non_speakable,
    )
    structured_lines = extract_structured_story_lines(
        language,
        tables,
        include_non_speakable=include_non_speakable,
    )
    existing_ids = {line.line_id for line in annotated}
    structured_lines = [line for line in structured_lines if line.line_id not in existing_ids]
    return annotated + hero_lines + structured_lines


def find_game_resource_root(home=None, environment=None):
    home = Path.home() if home is None else Path(home)
    environment = os.environ if environment is None else environment
    candidates = []
    containers = home / "Library" / "Containers"
    candidates.extend(containers.glob("*/Data/Documents/ResLib/iOS"))
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(Path(local_app_data).glob("**/ResLib/*"))
    for candidate in candidates:
        if (candidate / "bundles" / story_bundle_filename).is_file():
            return candidate.resolve()
    return None


def find_story_bundle(resource_root=None):
    root = (
        find_game_resource_root()
        if resource_root is None
        else Path(resource_root).expanduser().resolve()
    )
    if root is None:
        return None
    candidate = root / "bundles" / story_bundle_filename
    return candidate if candidate.is_file() else None


def _load_unity_environment(path):
    try:
        import UnityPy
    except ImportError as error:
        raise Reverse1999StoryError(
            "UnityPy is required for story extraction; install the project dependencies"
        ) from error
    data = Path(path).read_bytes()
    header = data.find(b"UnityFS")
    if header < 0:
        raise Reverse1999StoryError(f"UnityFS header not found in {path}")
    try:
        return UnityPy.load(data[header:])
    except Exception as error:
        raise Reverse1999StoryError(f"Unable to load story bundle {path}: {error}") from error


def extract_story_lines(bundle, *, language_index=2, include_non_speakable=False, progress=None):
    progress = progress or (lambda _current, _total, _source: None)
    environment = _load_unity_environment(bundle)
    assets = [obj for obj in environment.objects if obj.type.name == "TextAsset"]
    lines = []
    story_assets = []
    for obj in assets:
        asset = obj.read()
        if asset.m_Name.startswith("json_story_step_"):
            story_assets.append(asset)
    story_assets.sort(key=lambda asset: asset.m_Name)
    for current, asset in enumerate(story_assets, start=1):
        try:
            document = json.loads(asset.m_Script.lstrip("\ufeff"))
        except (TypeError, json.JSONDecodeError) as error:
            raise Reverse1999StoryError(f"Invalid JSON in {asset.m_Name}: {error}") from error
        lines.extend(
            parse_story_document(
                document,
                asset.m_Name,
                language_index=language_index,
                include_non_speakable=include_non_speakable,
            )
        )
        progress(current, len(story_assets), asset.m_Name)
    lines.sort(key=lambda line: (line.chapter, line.sequence, line.line_id))
    return add_story_context(lines)


def add_story_context(lines):
    by_chapter = defaultdict(list)
    for line in lines:
        if line.speakable:
            by_chapter[line.chapter].append(line)
    context = {}
    for chapter_lines in by_chapter.values():
        chapter_lines.sort(key=lambda line: (line.sequence, line.line_id))
        for index, line in enumerate(chapter_lines):
            context[line.line_id] = (
                chapter_lines[index - 1].text if index else None,
                chapter_lines[index + 1].text if index + 1 < len(chapter_lines) else None,
            )
    return [
        replace(
            line,
            previous_text=context.get(line.line_id, (None, None))[0],
            next_text=context.get(line.line_id, (None, None))[1],
        )
        for line in lines
    ]


def resolve_story_audio(lines, resolver):
    resolved = []
    for line in lines:
        resolution = resolver.resolve(line.source_voice_spec)
        resolved.append(
            replace(
                line,
                audio_status=resolution.status,
                audio_reason=resolution.reason,
                source_voice_id=resolution.audio_id,
                source_event=resolution.event,
                source_bank=resolution.bank,
                source_media_ids=resolution.media_ids,
                available_media_ids=resolution.available_media_ids,
            )
        )
    return resolved


def build_story_audio_resolver(
    *,
    config_directory=None,
    bank_index_path=default_bank_index,
    game_audio_directory=None,
    progress=None,
):
    config_directory = config_directory or find_game_config_directory()
    if config_directory is None:
        raise Reverse1999StoryError(
            "Unable to find installed game configs; pass --config-directory"
        )
    _language, tables = load_config_directory(config_directory)
    try:
        registry = build_audio_registry(tables)
    except StoryAudioResolutionError as error:
        raise Reverse1999StoryError(str(error)) from error
    bank_index_path = Path(bank_index_path).expanduser().resolve()
    try:
        resolver = StoryAudioResolver.from_file(registry, bank_index_path)
    except StoryAudioResolutionError as error:
        audio_root = game_audio_directory or find_game_audio_directory()
        if audio_root is None:
            raise Reverse1999StoryError(
                "Unable to find installed English audio; pass --game-audio-directory"
            ) from error
        bank_index, _output = build_bank_index(
            audio_root,
            output=bank_index_path,
            progress=progress,
        )
        try:
            resolver = StoryAudioResolver(registry, bank_index)
        except StoryAudioResolutionError as resolver_error:
            raise Reverse1999StoryError(str(resolver_error)) from resolver_error
    return resolver


def write_story_index(lines, output=default_output, *, bundle=None):
    output = Path(output).expanduser().resolve()
    metadata = {
        "game": "Reverse: 1999",
        "language": "en",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_bundle": str(Path(bundle).resolve()) if bundle else None,
        "line_count": len(lines),
        "speakable_count": sum(line.speakable for line in lines),
        "audio_status_counts": dict(sorted(Counter(line.audio_status for line in lines).items())),
        "source_kind_counts": dict(sorted(Counter(line.source_kind for line in lines).items())),
        "story_group_counts": dict(
            sorted(Counter(line.story_group for line in lines if line.story_group).items())
        ),
    }
    return write_story_index_document(output, metadata, lines)


def create_parser():
    parser = argparse.ArgumentParser(
        description="Extract local Reverse: 1999 story text into a generic VNTTS JSONL index."
    )
    parser.add_argument(
        "--resource-root", type=Path, help="Installed ResLib platform directory containing bundles/"
    )
    parser.add_argument(
        "--bundle", type=Path, help="Story Unity bundle; overrides automatic discovery"
    )
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--game-audio-directory", type=Path)
    parser.add_argument("--bank-index", type=Path, default=default_bank_index)
    parser.add_argument(
        "--include-non-speakable",
        action="store_true",
        help="Include localized test, placeholder, and non-English records.",
    )
    parser.add_argument(
        "--skip-audio-resolution",
        action="store_true",
        help="Write unchecked lines without scanning config and local Wwise media.",
    )
    parser.add_argument(
        "--skip-config-sources",
        action="store_true",
        help="Exclude config-only hero stories and anecdote classification.",
    )
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    bundle = options.bundle or find_story_bundle(options.resource_root)
    if bundle is None:
        print(
            "Unable to find the installed Reverse: 1999 story bundle; pass --resource-root or --bundle",
            file=sys.stderr,
        )
        return 1
    try:
        lines = extract_story_lines(
            bundle,
            include_non_speakable=options.include_non_speakable,
            progress=lambda current, total, source: (
                print(f"Parsed {current}/{total}: {source}")
                if current == total or current % 250 == 0
                else None
            ),
        )
        if not options.skip_config_sources:
            config_directory = options.config_directory or find_game_config_directory()
            if config_directory is None:
                raise Reverse1999StoryError(
                    "Unable to find installed game configs; pass --config-directory "
                    "or --skip-config-sources"
                )
            language, tables = load_config_directory(config_directory)
            lines = enrich_story_sources(
                lines,
                language,
                tables,
                include_non_speakable=options.include_non_speakable,
            )
        if not options.skip_audio_resolution:
            resolver = build_story_audio_resolver(
                config_directory=options.config_directory,
                bank_index_path=options.bank_index,
                game_audio_directory=options.game_audio_directory,
                progress=lambda current, total, bank, reused: (
                    print(f"{'Reused' if reused else 'Indexed'} {current}/{total}: {bank.name}")
                    if current == total or current % 250 == 0
                    else None
                ),
            )
            lines = resolve_story_audio(lines, resolver)
        output = write_story_index(lines, options.output, bundle=bundle)
    except (
        OSError,
        Reverse1999ConfigError,
        Reverse1999IndexError,
        Reverse1999StoryError,
        StoryAudioResolutionError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    dialogue = sum(line.kind == "dialogue" for line in lines)
    statuses = Counter(line.audio_status for line in lines)
    print(
        f"Wrote {len(lines)} lines ({dialogue} dialogue, "
        f"{len(lines) - dialogue} narration; "
        f"{', '.join(f'{key}={value}' for key, value in sorted(statuses.items()))}) "
        f"to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
