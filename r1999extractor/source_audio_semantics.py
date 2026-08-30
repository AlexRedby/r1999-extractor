"""Checksum-bound semantic evidence for exact installed source-audio cues."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import unicodedata
import wave
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.story_index import (
    StoryIndexError,
    load_story_index_document,
    write_story_index_document,
)

from r1999extractor.reverse1999_index import bank_index_staleness_reasons
from r1999extractor.source_audio_duration import _audio_resolution
from r1999extractor.story_audio import StoryAudioResolutionError, StoryAudioResolver
from r1999extractor.wwise import AudioConversionError, convert_audio

SEMANTIC_EVIDENCE_SCHEMA = "r1999.source-audio-semantic-evidence"
SEMANTIC_EVIDENCE_VERSION = 1
SEMANTIC_EVIDENCE_METHOD = "local-asr-exact-normalized-transcript"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", flags=re.UNICODE)


class SourceAudioSemanticEvidenceError(RuntimeError):
    """Source-audio semantic evidence cannot be produced or applied safely."""


def normalize_semantic_text(text):
    """Normalize only spoken word identity, not punctuation or typography."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(
        token.casefold().replace("’", "'") for token in WORD_PATTERN.findall(normalized)
    )


def semantic_text_sha256(text):
    return hashlib.sha256(normalize_semantic_text(text).encode("utf-8")).hexdigest()


def _canonical_sha256(document):
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_control_path(path):
    """Hash one model file or a complete local model snapshot."""
    path = Path(path).expanduser().resolve()
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
        if not path.is_dir():
            raise SourceAudioSemanticEvidenceError(f"ASR model snapshot does not exist: {path}")
        digest = hashlib.sha256()
        for candidate in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(hashlib.sha256(candidate.read_bytes()).digest())
        return digest.hexdigest()
    except SourceAudioSemanticEvidenceError:
        raise
    except OSError as error:
        raise SourceAudioSemanticEvidenceError(
            f"Unable to hash ASR model snapshot {path}: {error}"
        ) from error


class WhisperTranscriber:
    """Optional local-only Whisper adapter loaded only by the authoring command."""

    def __init__(self, model_directory, *, device="cpu"):
        try:
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

            processor = AutoProcessor.from_pretrained(
                model_directory,
                local_files_only=True,
            )
            model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_directory,
                local_files_only=True,
            )
            model.eval()
            self._pipeline = pipeline(
                "automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                feature_extractor=processor.feature_extractor,
                device=device,
            )
        except Exception as error:
            raise SourceAudioSemanticEvidenceError(
                "Unable to load the local ASR model. Install the optional "
                f"authoring dependencies and provide an offline snapshot: {error}"
            ) from error

    def __call__(self, wav_payload):
        try:
            with wave.open(io.BytesIO(wav_payload), "rb") as source:
                if source.getnchannels() != 1 or source.getsampwidth() != 2:
                    raise SourceAudioSemanticEvidenceError(
                        "Decoded source cue must be mono PCM16 WAV"
                    )
                samples = np.frombuffer(
                    source.readframes(source.getnframes()),
                    dtype="<i2",
                ).astype(np.float32)
                audio = {
                    "array": samples / 32768.0,
                    "sampling_rate": source.getframerate(),
                }
            result = self._pipeline(audio, return_timestamps=True)
            text = result.get("text") if isinstance(result, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise SourceAudioSemanticEvidenceError(
                    "ASR returned no transcript for an exact source cue"
                )
            return text.strip()
        except SourceAudioSemanticEvidenceError:
            raise
        except Exception as error:
            raise SourceAudioSemanticEvidenceError(
                f"Unable to transcribe an exact source cue: {error}"
            ) from error


def publish_source_audio_semantic_evidence(
    story_index,
    bank_index,
    evidence_output,
    story_output,
    model_directory,
    *,
    chapters=(),
    decoder="vgmstream-cli",
    device="cpu",
    transcriber=None,
    resolver=None,
    model_sha256=None,
):
    """Transcribe unknown exact media and publish evidence plus a story successor."""
    source = Path(story_index).expanduser().resolve()
    evidence_destination = Path(evidence_output).expanduser().resolve()
    story_destination = Path(story_output).expanduser().resolve()
    if source in {evidence_destination, story_destination}:
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence must publish new files, not mutate its story input"
        )
    for destination in (evidence_destination, story_destination):
        if destination.exists():
            raise SourceAudioSemanticEvidenceError(
                f"Semantic evidence destination already exists: {destination}"
            )

    document = load_story_index_document(source)
    selected_chapters = {str(chapter).strip() for chapter in chapters if str(chapter).strip()}
    candidates = [
        line.to_record()
        for line in document.records
        if line.to_record().get("source_audio_completeness") == "unknown"
        and (not selected_chapters or line.chapter in selected_chapters)
    ]
    if not candidates:
        raise SourceAudioSemanticEvidenceError(
            "No unknown timed source-audio cues match the requested chapters"
        )

    model_path = Path(model_directory).expanduser().resolve()
    before_model_sha256 = model_sha256 or sha256_control_path(model_path)
    _require_sha256(before_model_sha256, "ASR model SHA-256")
    transcriber = transcriber or WhisperTranscriber(model_path, device=device)
    resolver = resolver or _load_resolver(bank_index)

    entries = {}
    for record in candidates:
        identity = _record_identity(record)
        resolution = _audio_resolution(record)
        if resolution is None:
            raise SourceAudioSemanticEvidenceError(
                f"Unknown source cue has no exact media route: {record['line_id']}"
            )
        try:
            media = resolver.read_single_available_media(resolution)
        except (OSError, StoryAudioResolutionError) as error:
            raise SourceAudioSemanticEvidenceError(str(error)) from error
        if media is None:
            raise SourceAudioSemanticEvidenceError(
                f"Unknown source cue is not bound to one installed WEM: {record['line_id']}"
            )
        media_id, payload = media
        if media_id != identity[1] or hashlib.sha256(payload).hexdigest() != identity[2]:
            raise SourceAudioSemanticEvidenceError(
                f"Timed source media changed for {record['line_id']}"
            )
        wav_payload = _decode_wem(payload, media_id, decoder)
        observed = str(transcriber(wav_payload) or "").strip()
        if not observed:
            raise SourceAudioSemanticEvidenceError(
                f"ASR returned no transcript for {record['line_id']}"
            )
        normalized_expected = normalize_semantic_text(record["text"])
        normalized_observed = normalize_semantic_text(observed)
        verdict = "full" if normalized_observed == normalized_expected else "partial"
        reason = (
            "exact-normalized-asr-transcript" if verdict == "full" else "asr-transcript-mismatch"
        )
        key = (document.metadata.get("language"), identity[2], identity[4])
        entry = entries.get(key)
        if entry is None:
            entry = {
                "locale": key[0],
                "media_id": media_id,
                "media_sha256": identity[2],
                "displayed_text_sha256": identity[3],
                "normalized_displayed_text_sha256": identity[4],
                "observed_transcript": observed,
                "normalized_observed_text_sha256": semantic_text_sha256(observed),
                "verdict": verdict,
                "reason": reason,
                "method": SEMANTIC_EVIDENCE_METHOD,
                "model_sha256": before_model_sha256,
                "source_line_ids": [],
            }
            entries[key] = entry
        elif any(
            entry[field] != value
            for field, value in (
                ("observed_transcript", observed),
                ("verdict", verdict),
                ("reason", reason),
            )
        ):
            raise SourceAudioSemanticEvidenceError(
                "Conflicting transcripts for one semantic source-audio identity"
            )
        entry["source_line_ids"].append(record["line_id"])

    after_model_sha256 = model_sha256 or sha256_control_path(model_path)
    if after_model_sha256 != before_model_sha256:
        raise SourceAudioSemanticEvidenceError("ASR model changed during transcription")
    canonical_entries = []
    for entry in sorted(
        entries.values(),
        key=lambda value: (
            value["locale"],
            value["media_sha256"],
            value["normalized_displayed_text_sha256"],
        ),
    ):
        entry["source_line_ids"] = sorted(set(entry["source_line_ids"]))
        entry["entry_id"] = _canonical_sha256(
            {key: value for key, value in entry.items() if key != "source_line_ids"}
        )
        canonical_entries.append(entry)

    authority = {
        "schema": SEMANTIC_EVIDENCE_SCHEMA,
        "schema_version": SEMANTIC_EVIDENCE_VERSION,
        "locale": document.metadata.get("language"),
        "source_story_index_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "model": {
            "kind": "whisper",
            "snapshot": model_path.name,
            "sha256": before_model_sha256,
            "device": device,
            "decoding": "deterministic_greedy_default",
        },
        "entries": canonical_entries,
    }
    authority["evidence_id"] = _canonical_sha256(authority)
    authority["generated_at"] = datetime.now(timezone.utc).isoformat()
    validate_source_audio_semantic_evidence(authority)
    evidence_destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(evidence_destination, authority, sort_keys=True)
    return annotate_story_index_source_audio_semantics(
        source,
        evidence_destination,
        story_destination,
        chapters=selected_chapters,
    )


def validate_source_audio_semantic_evidence(document):
    if not isinstance(document, dict) or document.get("schema") != SEMANTIC_EVIDENCE_SCHEMA:
        raise SourceAudioSemanticEvidenceError("Unsupported source-audio semantic evidence schema")
    if document.get("schema_version") != SEMANTIC_EVIDENCE_VERSION:
        raise SourceAudioSemanticEvidenceError("Unsupported source-audio semantic evidence version")
    locale = document.get("locale")
    model = document.get("model")
    entries = document.get("entries")
    if not isinstance(locale, str) or not locale.strip():
        raise SourceAudioSemanticEvidenceError("Semantic evidence locale is invalid")
    if (
        not isinstance(model, dict)
        or model.get("kind") != "whisper"
        or model.get("decoding") != "deterministic_greedy_default"
    ):
        raise SourceAudioSemanticEvidenceError("Semantic evidence model is invalid")
    model_sha256 = _require_sha256(model.get("sha256"), "semantic evidence model")
    _require_sha256(
        document.get("source_story_index_sha256"),
        "semantic evidence source story index",
    )
    if not isinstance(entries, list) or not entries:
        raise SourceAudioSemanticEvidenceError("Semantic evidence entries are empty")
    keys = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("locale") != locale:
            raise SourceAudioSemanticEvidenceError("Semantic evidence entry locale changed")
        media_sha256 = _require_sha256(entry.get("media_sha256"), "semantic media")
        normalized_text_sha256 = _require_sha256(
            entry.get("normalized_displayed_text_sha256"),
            "semantic normalized displayed text",
        )
        _require_sha256(entry.get("displayed_text_sha256"), "semantic displayed text")
        if entry.get("model_sha256") != model_sha256:
            raise SourceAudioSemanticEvidenceError("Semantic entry model binding changed")
        observed = entry.get("observed_transcript")
        if not isinstance(observed, str) or not normalize_semantic_text(observed):
            raise SourceAudioSemanticEvidenceError("Semantic transcript is empty")
        if entry.get("normalized_observed_text_sha256") != semantic_text_sha256(observed):
            raise SourceAudioSemanticEvidenceError("Semantic transcript hash changed")
        verdict = entry.get("verdict")
        reason = entry.get("reason")
        expected_reason = (
            "exact-normalized-asr-transcript" if verdict == "full" else "asr-transcript-mismatch"
        )
        if verdict not in {"full", "partial"} or reason != expected_reason:
            raise SourceAudioSemanticEvidenceError("Semantic verdict is invalid")
        if entry.get("method") != SEMANTIC_EVIDENCE_METHOD:
            raise SourceAudioSemanticEvidenceError("Semantic evidence method changed")
        source_line_ids = entry.get("source_line_ids")
        if (
            not isinstance(source_line_ids, list)
            or source_line_ids != sorted(set(source_line_ids))
            or any(not isinstance(line_id, str) or not line_id for line_id in source_line_ids)
        ):
            raise SourceAudioSemanticEvidenceError("Semantic source line IDs are invalid")
        entry_id = entry.get("entry_id")
        expected_entry_id = _canonical_sha256(
            {
                key: value
                for key, value in entry.items()
                if key not in {"entry_id", "source_line_ids"}
            }
        )
        if entry_id != expected_entry_id:
            raise SourceAudioSemanticEvidenceError("Semantic evidence entry ID changed")
        keys.append((locale, media_sha256, normalized_text_sha256))
    if keys != sorted(set(keys)):
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence entries are duplicated or not canonical"
        )
    authority = {
        key: value for key, value in document.items() if key not in {"evidence_id", "generated_at"}
    }
    if document.get("evidence_id") != _canonical_sha256(authority):
        raise SourceAudioSemanticEvidenceError("Semantic evidence ID changed")
    return document


def load_source_audio_semantic_evidence(path):
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceAudioSemanticEvidenceError(
            f"Unable to read semantic evidence {path}: {error}"
        ) from error
    return validate_source_audio_semantic_evidence(document)


def annotate_story_index_source_audio_semantics(
    story_index,
    evidence,
    output,
    *,
    chapters=(),
):
    """Apply exact evidence matches and reject any unresolved selected unknown."""
    source = Path(story_index).expanduser().resolve()
    destination = Path(output).expanduser().resolve()
    if source == destination or destination.exists():
        raise SourceAudioSemanticEvidenceError(
            "Semantic annotation must publish a new story-index destination"
        )
    document = load_story_index_document(source)
    authority = load_source_audio_semantic_evidence(evidence)
    locale = str(document.metadata.get("language") or "").strip()
    if authority["locale"] != locale:
        raise SourceAudioSemanticEvidenceError("Semantic evidence locale differs from story")
    index = {
        (
            entry["locale"],
            entry["media_sha256"],
            entry["normalized_displayed_text_sha256"],
        ): entry
        for entry in authority["entries"]
    }
    selected_chapters = {str(chapter).strip() for chapter in chapters if str(chapter).strip()}
    records = []
    unresolved = []
    applied = 0
    for line in document.records:
        record = line.to_record()
        selected = not selected_chapters or line.chapter in selected_chapters
        if selected and record.get("source_audio_completeness") == "unknown":
            identity = _record_identity(record)
            key = (locale, identity[2], identity[4])
            entry = index.get(key)
            if entry is None:
                unresolved.append(record["line_id"])
            else:
                record.update(
                    source_audio_completeness=entry["verdict"],
                    source_audio_completeness_reason=entry["reason"],
                    source_audio_semantic_evidence_id=authority["evidence_id"],
                    source_audio_semantic_evidence_entry_id=entry["entry_id"],
                )
                applied += 1
        records.append(record)
    if unresolved:
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence does not cover selected unknown source cues: "
            + ", ".join(unresolved)
        )
    if not applied:
        raise SourceAudioSemanticEvidenceError("Semantic evidence matched no unknown cues")
    metadata = dict(document.metadata)
    metadata["source_audio_semantics"] = {
        "evidence_id": authority["evidence_id"],
        "evidence_sha256": hashlib.sha256(
            Path(evidence).expanduser().resolve().read_bytes()
        ).hexdigest(),
        "method": SEMANTIC_EVIDENCE_METHOD,
        "selected_chapters": sorted(selected_chapters),
        "applied_count": applied,
    }
    return write_story_index_document(destination, metadata, records)


def _record_identity(record):
    line_id = str(record.get("line_id") or "").strip()
    media_id = record.get("source_audio_duration_media_id")
    media_sha256 = record.get("source_audio_duration_media_sha256")
    displayed_text_sha256 = record.get("text_sha256")
    normalized_text_sha256 = semantic_text_sha256(record.get("text"))
    if not line_id or isinstance(media_id, bool) or not isinstance(media_id, int):
        raise SourceAudioSemanticEvidenceError("Timed source cue identity is invalid")
    for value, label in (
        (media_sha256, "timed media"),
        (displayed_text_sha256, "displayed text"),
    ):
        _require_sha256(value, label)
    if not normalize_semantic_text(record.get("text")):
        raise SourceAudioSemanticEvidenceError(
            f"Timed source cue has no normalized words: {line_id}"
        )
    return line_id, media_id, media_sha256, displayed_text_sha256, normalized_text_sha256


def _require_sha256(value, label):
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise SourceAudioSemanticEvidenceError(f"{label} SHA-256 is invalid")
    return value


def _load_resolver(bank_index):
    path = Path(bank_index).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceAudioSemanticEvidenceError(
            f"Unable to read bank index {path}: {error}"
        ) from error
    reasons = bank_index_staleness_reasons(document)
    if reasons:
        raise SourceAudioSemanticEvidenceError("Bank index is stale: " + "; ".join(reasons))
    return StoryAudioResolver({}, document)


def _decode_wem(payload, media_id, decoder):
    try:
        with TemporaryDirectory(prefix="r1999-source-semantics-") as directory:
            source = Path(directory) / f"{media_id}.wem"
            output = Path(directory) / f"{media_id}.wav"
            source.write_bytes(payload)
            convert_audio(source, output, decoder=decoder)
            return output.read_bytes()
    except (OSError, AudioConversionError) as error:
        raise SourceAudioSemanticEvidenceError(
            f"Unable to decode exact source media {media_id}: {error}"
        ) from error


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Publish checksum-bound local-ASR evidence and a semantically "
            "classified story-index successor."
        )
    )
    parser.add_argument("--story-index", type=Path, required=True)
    parser.add_argument("--bank-index", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--story-output", type=Path, required=True)
    parser.add_argument("--chapter", action="append", default=[])
    parser.add_argument("--decoder", default="vgmstream-cli")
    parser.add_argument("--device", default="cpu")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        result = publish_source_audio_semantic_evidence(
            options.story_index,
            options.bank_index,
            options.evidence_output,
            options.story_output,
            options.model,
            chapters=options.chapter,
            decoder=options.decoder,
            device=options.device,
        )
    except (
        OSError,
        StoryIndexError,
        SourceAudioSemanticEvidenceError,
        StoryAudioResolutionError,
    ) as error:
        print(error, file=sys.stderr)
        return 1
    semantics = result.metadata["source_audio_semantics"]
    print(f"Applied {semantics['applied_count']} exact semantic decisions; wrote {result.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
