"""Content-type authority for original main-story speech coverage."""

from collections.abc import Mapping

MAIN_STORY_SOURCE_AUDIO_REASON = "main-story-available-source-audio"
SOURCE_AUDIO_COMPLETENESS_POLICY = "main-story-available-source-audio-v1"


def has_full_main_story_source_audio(record: Mapping[str, object]) -> bool:
    """Main-story originals may use a different language than their subtitles."""
    return (
        record.get("collection_kind") == "main_story"
        and record.get("source_audio_status") == "available"
    )


def apply_main_story_source_audio_policy(metadata: dict, records: list[dict]) -> bool:
    """Replace superseded ASR verdicts in memory and retain other evidence bindings."""
    changed = False
    eligible = False
    for record in records:
        if not has_full_main_story_source_audio(record):
            continue
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
