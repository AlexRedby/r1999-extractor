"""Content-type authority for original main-story speech coverage."""

import re
from collections.abc import Mapping
from pathlib import Path

MAIN_STORY_SOURCE_AUDIO_REASON = "main-story-available-source-audio"
SOURCE_AUDIO_COMPLETENESS_POLICY = "main-story-available-source-audio-v2"


def classify_story_audio_cue(record: Mapping[str, object]) -> str:
    bank = str(record.get("source_bank") or "").casefold()
    event = str(record.get("source_event") or "").casefold()
    if not bank or not event:
        return "unknown"
    bank = Path(bank).stem
    if any(re.search(r"(?:^|_)sfx(?:_|$)", value) for value in (bank, event)):
        return "other_audio"
    if bank.startswith(("plotvoc_", "activityvoc_")) and event.startswith(
        ("play_plotvoc_", "play_activityvoc_")
    ):
        return "voice_candidate"
    return "other_audio"


def main_story_plotvoc_cues(record: Mapping[str, object]) -> list[Mapping]:
    """Installed main-story PlotVoc cues in the game's declared order."""
    if record.get("collection_kind") != "main_story":
        return []
    cues = []
    for cue in record.get("story_audio_cues", ()):
        if (
            cue.get("source_audio_status") == "available"
            and str(cue.get("source_bank") or "").casefold().startswith("plotvoc_")
            and classify_story_audio_cue(cue) == "voice_candidate"
        ):
            cues.append(cue)
    return cues


def effective_main_story_source_audio(record: Mapping[str, object]) -> Mapping | None:
    """Return the primary original, otherwise the first installed PlotVoc cue in game order."""
    if record.get("collection_kind") != "main_story":
        return None
    if record.get("source_audio_status") == "available":
        return record
    return next(iter(main_story_plotvoc_cues(record)), None)


def has_full_main_story_source_audio(record: Mapping[str, object]) -> bool:
    """Main-story originals may use a different language than their subtitles."""
    return effective_main_story_source_audio(record) is not None


def apply_main_story_source_audio_policy(metadata: dict, records: list[dict]) -> bool:
    """Replace superseded ASR verdicts in memory and retain other evidence bindings."""
    changed = False
    eligible = False
    for record in records:
        source = effective_main_story_source_audio(record)
        if source is None:
            continue
        if source is not record:
            record.pop("source_audio_wait_seconds", None)
            for key in tuple(record):
                if key.startswith("source_audio_duration_"):
                    record.pop(key)
            record.update(
                source_audio_status="available",
                source_audio_id=source["source_audio_id"],
                source_voice_id=source["source_audio_id"],
                source_audio_cue_index=source["cue_index"],
                audio_status="installed",
                audio_reason=source.get("audio_reason", "resolved_local_media"),
                **{
                    key: source.get(key)
                    for key in (
                        "source_event",
                        "source_bank",
                        "source_media_ids",
                        "available_media_ids",
                    )
                },
            )
            changed = True
        eligible = True
        for key, value in (
            ("source_audio_completeness", "full"),
            ("source_audio_completeness_reason", MAIN_STORY_SOURCE_AUDIO_REASON),
        ):
            if record.get(key) != value:
                record[key] = value
                changed = True
        for key in (
            "source_audio_semantic_evidence_id",
            "source_audio_semantic_evidence_entry_id",
        ):
            if key in record:
                record.pop(key)
                changed = True
    if not eligible:
        return changed
    if metadata.get("source_audio_completeness_policy") != SOURCE_AUDIO_COMPLETENESS_POLICY:
        metadata["source_audio_completeness_policy"] = SOURCE_AUDIO_COMPLETENESS_POLICY
        changed = True
    binding = metadata.get("source_audio_semantics")
    if isinstance(binding, dict):
        applied_count = sum(
            record.get("source_audio_semantic_evidence_entry_id") is not None for record in records
        )
        if not applied_count:
            metadata.pop("source_audio_semantics")
            changed = True
        elif binding.get("applied_count") != applied_count:
            metadata["source_audio_semantics"] = {**binding, "applied_count": applied_count}
            changed = True
    return changed
