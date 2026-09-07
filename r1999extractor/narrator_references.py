"""Select narrator auditions from exact spoken dialogue, never arbitrary bank media."""

import hashlib
import json
import re
from pathlib import Path

from vntts_artifacts.voice_manifest import normalize_character_name, write_voice_manifest

from r1999extractor.reverse1999_voice_import import decode_reference_data
from r1999extractor.story_audio import AudioConfiguration, StoryAudioResolver, wwise_event_id
from r1999extractor.story_voice_candidates import (
    StoryVoiceCandidateError,
    collect_story_voice_lines,
    snapshot_bank,
)
from r1999extractor.wwise import resolve_decoder


def list_narrator_references(story_index, role):
    """Rank all suitable transcripts without reading or decoding audio payloads."""
    missing = (
        f"No suitable installed spoken references for {role}. "
        "Check that English character voice audio is installed, then find game voices again. "
        "Unlabelled story sounds and combat clips are not used as narrator references."
    )
    try:
        lines, _story_sha256 = collect_story_voice_lines(story_index, (role,))
    except StoryVoiceCandidateError as error:
        if "No installed same-speaker" in str(error):
            raise StoryVoiceCandidateError(missing) from error
        raise
    playable = [line for line in lines if line.line_id.startswith("playable-voice:")]
    if playable:
        lines = [
            line
            for line in playable
            if Path(line.source_bank).stem.startswith("mianvoc_hero")
            or Path(line.source_bank).stem.endswith("_mainvoc")
        ]
    elif normalize_character_name(role) in {
        normalize_character_name(name)
        for name in json.loads(
            (Path(story_index).parent / "narrator-banks.json").read_text(encoding="utf-8")
        )
    }:
        # A known playable role with no playable speech must not fall back to story effects.
        raise StoryVoiceCandidateError(missing)

    grouped = {}
    for line in lines:
        if len(line.source_media_ids) == 1:
            grouped.setdefault((line.source_bank, line.source_media_ids[0]), []).append(line)
    candidates = []
    for group in grouped.values():
        line = group[0]
        words = re.findall(r"[a-z]+(?:['’][a-z]+)?", line.text.casefold())
        # ponytail: conservative transcript gate, not ASR; verify speech if routed text proves unreliable.
        if len(words) < 6 or len(set(words)) < 4 or len({v.text_sha256 for v in group}) != 1:
            continue
        candidates.append((abs(len(words) - 20), line.line_id, line))
    if not candidates:
        raise StoryVoiceCandidateError(missing)
    return tuple(line for _score, _id, line in sorted(candidates))


def prepare_narrator_references(story_index, bank_index, role, output, *, line_id=None):
    selected = list_narrator_references(story_index, role)
    if line_id is not None:
        selected = tuple(line for line in selected if line.line_id == line_id)
        if not selected:
            raise StoryVoiceCandidateError("Selected narrator reference is no longer available")
    index = json.loads(Path(bank_index).read_text(encoding="utf-8"))
    snapshots = {
        bank: snapshot_bank(index, bank) for bank in {line.source_bank for line in selected}
    }
    resolver = StoryAudioResolver(
        {
            line.source_audio_id: AudioConfiguration(
                line.source_audio_id, line.source_event, line.source_bank, "narrator"
            )
            for line in selected
        },
        index,
    )
    payloads = []
    for line in selected:
        if (
            snapshots[line.source_bank].routes.get(wwise_event_id(line.source_event))
            != line.source_media_ids
        ):
            raise StoryVoiceCandidateError(f"Exact bank route changed for {line.line_id}")
        resolution = resolver.resolve(line.source_audio_id)
        if (
            resolution.status != "installed"
            or resolution.media_ids != line.source_media_ids
            or resolution.bank != line.source_bank
            or resolution.event != line.source_event
        ):
            raise StoryVoiceCandidateError(
                f"Spoken audio is no longer available for {line.line_id}"
            )
        _media_id, payload = resolver.read_single_available_media(resolution)
        payloads.append(payload)
    identity = hashlib.sha256(
        json.dumps(
            [
                (line.line_id, line.text_sha256, hashlib.sha256(payload).hexdigest())
                for line, payload in zip(selected, payloads, strict=True)
            ]
        ).encode()
    ).hexdigest()
    directory = Path(output).expanduser().resolve() / f"narrator-spoken-v1-{identity}"
    decoder = resolve_decoder("vgmstream-cli")
    voices = []
    for position, (line, payload) in enumerate(zip(selected, payloads, strict=True), 1):
        reference = decode_reference_data(
            payload,
            directory / "references" / f"{position}.wav",
            line.source_media_ids[0],
            decoder,
            bank=line.source_bank,
        )
        voices.append(
            {
                "character": f"{role} spoken reference {position}",
                "speaker": f"narrator-{identity}-{position}",
                "aliases": [],
                "references": [reference.path.relative_to(directory).as_posix()],
                "vntts.narrator_reference": {
                    "title": line.collection_title or f"Voice {line.source_audio_id}",
                    "text": line.text,
                    "line_id": line.line_id,
                    "bank": line.source_bank,
                    "bank_sha256": snapshots[line.source_bank].sha256,
                    "media_id": reference.media_id,
                    "source_sha256": reference.source_sha256,
                    "reference_sha256": reference.reference_sha256,
                },
            }
        )
    manifest = directory / "manifest.json"
    write_voice_manifest(manifest, {"version": 2, "voices": voices})
    return manifest
