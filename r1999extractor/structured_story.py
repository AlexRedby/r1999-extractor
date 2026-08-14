from collections import Counter, defaultdict
from dataclasses import dataclass

from r1999extractor.reverse1999_aliases import canonical_voice_name
from r1999extractor.reverse1999_config import extract_character_identities, resolve_speaker_name
from r1999extractor.story_index import (
    StoryLine,
    add_story_context,
    classify_speakable_english,
    clean_story_text,
)


@dataclass(frozen=True)
class StructuredSourceSpec:
    table: str
    text_index: int
    group_index: int = 0
    sequence_index: int = 1
    type_index: int | None = None
    allowed_types: tuple[str, ...] = ()
    speaker_id_index: int | None = None
    speaker_name_index: int | None = None
    voice_index: int | None = None
    default_kind: str = "narration"


structured_source_specs = (
    StructuredSourceSpec("json_activity107_bubble_talk_step", 2),
    StructuredSourceSpec(
        "json_activity108_dialog", 6, type_index=2, allowed_types=("dialog", "aside", "location")
    ),
    StructuredSourceSpec("json_activity117_talk", 2, default_kind="dialogue"),
    StructuredSourceSpec(
        "json_activity163_dialog",
        6,
        sequence_index=2,
        speaker_id_index=5,
        speaker_name_index=3,
    ),
    StructuredSourceSpec(
        "json_activity206_dialogue", 5, speaker_id_index=2, speaker_name_index=3
    ),
    StructuredSourceSpec("json_activity231_talk", 2, group_index=1, sequence_index=0),
    StructuredSourceSpec("json_arcade_talk_step", 2),
    StructuredSourceSpec("json_battle_dialog", 8, speaker_id_index=6, default_kind="dialogue"),
    StructuredSourceSpec("json_bbs_dialog", 4, speaker_id_index=2),
    StructuredSourceSpec(
        "json_chapter_map_element_dialog",
        6,
        type_index=2,
        allowed_types=("dialog", "aside", "location"),
        speaker_name_index=5,
        voice_index=4,
    ),
    StructuredSourceSpec(
        "json_dialog_step",
        3,
        speaker_id_index=5,
        speaker_name_index=4,
        default_kind="dialogue",
    ),
    StructuredSourceSpec("json_dice_dialogue", 3),
    StructuredSourceSpec("json_explore_dialogue", 5),
    StructuredSourceSpec(
        "json_fairyland_puzzle_talk",
        6,
        type_index=2,
        allowed_types=("dialog", "aside", "location"),
        speaker_name_index=5,
        voice_index=4,
    ),
    StructuredSourceSpec("json_guide_step", 9, speaker_id_index=6, default_kind="dialogue"),
    StructuredSourceSpec("json_hero_story_dispatch_talk", 2),
    StructuredSourceSpec("json_mail", 4, sequence_index=0, speaker_name_index=2),
    StructuredSourceSpec(
        "json_odyssey_dialog_element", 6, type_index=2, speaker_name_index=5
    ),
    StructuredSourceSpec("json_rogue_dialog", 2),
    StructuredSourceSpec("json_room_character_dialog", 3),
    StructuredSourceSpec(
        "json_room_character_dialog_select", 2, group_index=1, sequence_index=0
    ),
    StructuredSourceSpec("json_rouge_piece_talk", 2, sequence_index=0),
    StructuredSourceSpec("json_rouge_talk", 1, sequence_index=0),
    StructuredSourceSpec(
        "json_sodache_dialog",
        6,
        group_index=1,
        sequence_index=2,
        speaker_id_index=5,
        speaker_name_index=4,
    ),
    StructuredSourceSpec("json_sodache_bubble_talk_step", 2),
    StructuredSourceSpec("json_story_prologue_synopsis", 2, sequence_index=0),
    StructuredSourceSpec("json_survival_talk", 2, sequence_index=0),
    StructuredSourceSpec(
        "json_tip_dialog", 5, type_index=2, allowed_types=("talk",), speaker_id_index=4
    ),
    StructuredSourceSpec("json_v2a4_warmup_dialog", 2, sequence_index=0),
    StructuredSourceSpec("json_v3a6_warmup_story", 2, sequence_index=0),
    StructuredSourceSpec(
        "json_weekwalk_dialog",
        7,
        type_index=2,
        allowed_types=("dialog", "aside", "location"),
        speaker_name_index=6,
    ),
    StructuredSourceSpec(
        "json_tower_v3a7_story", 3, speaker_id_index=2, default_kind="dialogue"
    ),
)


def _language_text(language, value):
    text = language.get(value) if isinstance(value, str) else None
    return clean_story_text(text) if isinstance(text, str) and text.strip() else ""


def _value(row, index, default=""):
    return row[index] if index is not None and len(row) > index else default


def _room_dialog_speakers(tables):
    speakers = {}
    for row in tables.get("json_room_character_interaction", []):
        if not isinstance(row, list) or len(row) <= 16:
            continue
        group = str(row[16]).strip()
        speaker_id = str(row[1]).strip()
        if group and group != "0" and speaker_id and speaker_id != "0":
            speakers[group] = speaker_id
    return speakers


def extract_structured_story_lines(
    language,
    tables,
    *,
    catalog=None,
    include_non_speakable=False,
    specs=structured_source_specs,
):
    identities = extract_character_identities(language, tables)
    room_speakers = _room_dialog_speakers(tables)
    lines = []
    seen_line_ids = set()
    for spec in specs:
        group_positions = defaultdict(int)
        for row_position, row in enumerate(tables.get(spec.table, []), start=1):
            required = max(
                index
                for index in (
                    spec.text_index,
                    spec.group_index,
                    spec.sequence_index,
                    spec.type_index or 0,
                )
            )
            if not isinstance(row, list) or len(row) <= required:
                continue
            row_type = str(_value(row, spec.type_index)).strip()
            if spec.allowed_types and row_type not in spec.allowed_types:
                continue
            text = _language_text(language, row[spec.text_index])
            if not text:
                continue
            group = str(row[spec.group_index]).strip() or spec.table
            group_positions[group] += 1
            try:
                sequence = int(row[spec.sequence_index])
            except (TypeError, ValueError):
                sequence = group_positions[group]
            stable_row = f"{group}:{sequence}:{row_position}"
            line_id = f"reverse1999:config:{spec.table.removeprefix('json_')}:{stable_row}"
            if line_id in seen_line_ids:
                continue
            seen_line_ids.add(line_id)

            speaker_id = str(_value(row, spec.speaker_id_index)).strip()
            if spec.table == "json_room_character_dialog":
                speaker_id = room_speakers.get(group, speaker_id)
            speaker = _language_text(language, _value(row, spec.speaker_name_index))
            if not speaker and speaker_id and speaker_id != "0":
                speaker = resolve_speaker_name(speaker_id, identities, catalog=catalog) or ""
            kind = spec.default_kind
            if row_type in {"aside", "location", "narration", "system"}:
                kind = "narration"
            elif speaker or row_type in {"dialog", "talk"}:
                kind = "dialogue"
            speaker = speaker or "Narrator"
            # Config table IDs are not Unity story chapter IDs; values below
            # 1000 are valid and must not be treated as test assets.
            speakable, filter_reason = classify_speakable_english(text, "1000")
            if not speakable and not include_non_speakable:
                continue
            voice_spec = str(_value(row, spec.voice_index)).strip()
            if voice_spec == "0":
                voice_spec = ""
            lines.append(
                StoryLine(
                    record_type="line",
                    line_id=line_id,
                    chapter=group,
                    sequence=sequence,
                    speaker=speaker,
                    text=text,
                    source=spec.table,
                    portrait=None,
                    source_voice_id=voice_spec or None,
                    source_voice_spec=voice_spec or None,
                    display_seconds=None,
                    kind=kind,
                    voice_character=canonical_voice_name(speaker) or speaker,
                    text_sha256=__import__("hashlib").sha256(text.encode("utf-8")).hexdigest(),
                    speakable=speakable,
                    filter_reason=filter_reason,
                    audio_status="unchecked" if voice_spec else "no_audio",
                    audio_reason=(
                        "structured_source_voice_cue" if voice_spec else "structured_text_has_no_voice_cue"
                    ),
                    source_kind="structured_dialogue",
                    story_group=f"{spec.table}:{group}",
                    story_title=spec.table.removeprefix("json_").replace("_", " "),
                    story_order=row_position,
                )
            )
    lines.sort(key=lambda line: (line.source, line.chapter, line.story_order or 0, line.line_id))
    return add_story_context(lines)


def audit_story_like_tables(language, tables, specs=structured_source_specs):
    handled = {spec.table: spec for spec in specs}
    candidates = {}
    for table, rows in tables.items():
        lowered = table.casefold()
        if not any(token in lowered for token in ("dialog", "talk", "story", "episode", "mail")):
            continue
        localized = Counter()
        for row in rows:
            if not isinstance(row, list):
                continue
            for index, value in enumerate(row):
                if isinstance(value, str) and value in language and language[value].strip():
                    localized[index] += 1
        if localized:
            candidates[table] = {
                "row_count": len(rows),
                "localized_columns": dict(sorted(localized.items())),
                "status": "handled" if table in handled else "reviewed_not_extracted",
            }
    return {
        "handled_table_count": sum(item["status"] == "handled" for item in candidates.values()),
        "reviewed_table_count": len(candidates),
        "tables": dict(sorted(candidates.items())),
    }
