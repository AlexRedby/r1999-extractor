"""Select narrator auditions from exact spoken dialogue, never arbitrary bank media."""

import hashlib
import json
import re
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import normalize_character_name, write_voice_manifest

from r1999extractor.reverse1999_voice_import import REFERENCE_DECODE_VERSION, decode_reference_data
from r1999extractor.story_audio import AudioConfiguration, StoryAudioResolver, wwise_event_id
from r1999extractor.story_voice_candidates import (
    StoryVoiceCandidateError,
    collect_story_voice_lines,
    is_playable_main_voice_reference,
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
            if is_playable_main_voice_reference(line.line_id, line.source_bank)
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


def _fingerprint(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


class NarratorReferenceSession:
    """One character's validated catalog and bank snapshots, reused by a serial worker."""

    def __init__(self, story_index, bank_index, role, output):
        self.role = role
        self.output = Path(output).expanduser().resolve()
        self.inputs = {
            Path(path): _fingerprint(path)
            for path in (story_index, bank_index, Path(story_index).parent / "narrator-banks.json")
        }
        self.references = list_narrator_references(story_index, role)
        self.index = json.loads(Path(bank_index).read_text(encoding="utf-8"))
        self.snapshots = {}
        self.resolver = StoryAudioResolver(
            {
                line.source_audio_id: AudioConfiguration(
                    line.source_audio_id, line.source_event, line.source_bank, "narrator"
                )
                for line in self.references
            },
            self.index,
        )
        self._validate_inputs()

    def _validate_inputs(self):
        if any(_fingerprint(path) != fingerprint for path, fingerprint in self.inputs.items()):
            raise StoryVoiceCandidateError(
                "Narrator catalog changed. Reload the character references."
            )

    def prepare(self, *, line_id=None, decoder=None, runner=None):
        self._validate_inputs()
        selected = tuple(
            line for line in self.references if line_id is None or line.line_id == line_id
        )
        if not selected:
            raise StoryVoiceCandidateError("Selected narrator reference is no longer available")
        payloads = []
        source_kinds = []
        for line in selected:
            bank = line.source_bank
            if bank not in self.snapshots:
                snapshot = snapshot_bank(self.index, bank)
                self.snapshots[bank] = snapshot, _fingerprint(snapshot.path)
            snapshot, fingerprint = self.snapshots[bank]
            if _fingerprint(snapshot.path) != fingerprint:
                raise StoryVoiceCandidateError(
                    "Narrator audio bank changed. Reload the character references."
                )
            if snapshot.routes.get(wwise_event_id(line.source_event)) != line.source_media_ids:
                raise StoryVoiceCandidateError(f"Exact bank route changed for {line.line_id}")
            resolution = self.resolver.resolve(line.source_audio_id)
            if (
                resolution.status != "installed"
                or resolution.media_ids != line.source_media_ids
                or resolution.bank != bank
                or resolution.event != line.source_event
                or snapshot.streamed_routes.get(wwise_event_id(line.source_event), ())
                != resolution.streamed_media_ids
            ):
                raise StoryVoiceCandidateError(
                    f"Spoken audio is no longer available for {line.line_id}"
                )
            media_id = line.source_media_ids[0]
            # Reuse the validated snapshot instead of reading/parsing this bank twice.
            payload = self.resolver.read_media(resolution, media_id, embedded_media=snapshot.media)
            payloads.append(payload)
            source_kinds.append(
                "external"
                if media_id in resolution.streamed_media_ids or media_id not in snapshot.media
                else "embedded"
            )
        identity = hashlib.sha256(
            json.dumps(
                [
                    (line.line_id, line.text_sha256, hashlib.sha256(payload).hexdigest())
                    for line, payload in zip(selected, payloads, strict=True)
                ]
            ).encode()
        ).hexdigest()
        directory = self.output / f"narrator-spoken-v{REFERENCE_DECODE_VERSION}-{identity}"
        directory.resolve().relative_to(self.output)
        manifest = directory / "manifest.json"
        voices = [
            {
                "character": f"{self.role} spoken reference {position}",
                "speaker": f"narrator-{identity}-{position}",
                "aliases": [],
                "references": [f"references/{position}.wav"],
                "vntts.narrator_reference": {
                    "title": line.collection_title or f"Voice {line.source_audio_id}",
                    "text": line.text,
                    "line_id": line.line_id,
                    "bank": line.source_bank,
                    "bank_sha256": self.snapshots[line.source_bank][0].sha256,
                    "media_id": line.source_media_ids[0],
                    "source_sha256": hashlib.sha256(payload).hexdigest(),
                    "source_kind": source_kinds[position - 1],
                    "source_size_bytes": len(payload),
                    "decode_version": REFERENCE_DECODE_VERSION,
                },
            }
            for position, (line, payload) in enumerate(zip(selected, payloads, strict=True), 1)
        ]
        if _cached_audio_matches(manifest, voices):
            return manifest
        decoder = decoder or resolve_decoder("vgmstream-cli")
        for voice, line, payload in zip(voices, selected, payloads, strict=True):
            destination = directory / voice["references"][0]
            destination.resolve().relative_to(directory.resolve())
            if destination.is_symlink():
                raise StoryVoiceCandidateError("Cached narrator reference must not be a symlink")
            reference = decode_reference_data(
                payload,
                destination,
                line.source_media_ids[0],
                decoder,
                bank=line.source_bank,
                **({"runner": runner} if runner is not None else {}),
            )
            voice["vntts.narrator_reference"]["reference_sha256"] = reference.reference_sha256
            voice["vntts.narrator_reference"]["decoded_duration_seconds"] = (
                reference.decoded_duration_seconds
            )
            voice["vntts.narrator_reference"]["reference_duration_seconds"] = (
                reference.reference_duration_seconds
            )
        write_voice_manifest(manifest, {"version": 2, "voices": voices})
        return manifest


def _cached_audio_matches(manifest, expected):
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        for voice in saved["voices"]:
            digest = voice["vntts.narrator_reference"].pop("reference_sha256")
            voice["vntts.narrator_reference"].pop("decoded_duration_seconds")
            voice["vntts.narrator_reference"].pop("reference_duration_seconds")
            reference = manifest.parent / voice["references"][0]
            reference.resolve().relative_to(manifest.parent.resolve())
            if reference.is_symlink() or sha256_file(reference) != digest:
                return False
        return saved == {"version": 2, "voices": expected}
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return False


def prepare_narrator_references(story_index, bank_index, role, output, *, line_id=None):
    return NarratorReferenceSession(story_index, bank_index, role, output).prepare(line_id=line_id)
