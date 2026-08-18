"""Conservative local evidence for Character Story voice candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav

from r1999extractor.reverse1999_batch import (
    _cosine_similarity,
    _text_similarity,
    _transcript_flags,
    create_local_speaker_embedder,
    create_local_whisper_transcriber,
)
from r1999extractor.story_voice_review import StoryVoiceReviewError, load_review_session
from r1999extractor.voice_reference_quality import read_pcm_wav

EVIDENCE_SCHEMA = "r1999.story-voice-reference-evidence"
EVIDENCE_VERSION = 1
NONVERBAL_WORDS = frozenset(
    {
        "breath",
        "breathing",
        "cough",
        "cry",
        "crying",
        "gasp",
        "groan",
        "grunt",
        "laugh",
        "laughter",
        "scream",
        "sigh",
        "sob",
        "sobbing",
        "whimper",
        "whimpering",
    }
)
NON_SPEECH_ASR_MARKERS = frozenset({"music", "noise", "applause", "laughter"})


class StoryVoiceEvidenceError(RuntimeError):
    """Candidate evidence could not be computed safely."""


def _tokens(value):
    return re.findall(r"[a-z0-9']+", str(value).casefold())


def _word_error_rate(expected, observed):
    left = _tokens(expected)
    right = _tokens(observed)
    if not left:
        return 0.0 if not right else 1.0
    row = list(range(len(right) + 1))
    for left_index, left_word in enumerate(left, start=1):
        next_row = [left_index]
        for right_index, right_word in enumerate(right, start=1):
            next_row.append(
                min(
                    next_row[-1] + 1,
                    row[right_index] + 1,
                    row[right_index - 1] + (left_word != right_word),
                )
            )
        row = next_row
    return round(row[-1] / len(left), 4)


def _acoustic_evidence(path):
    samples, sample_rate = read_pcm_wav(path)
    if not len(samples):
        raise StoryVoiceEvidenceError(f"Candidate WAV contains no samples: {path}")
    frame_size = max(1, round(sample_rate * 0.025))
    padding = (-len(samples)) % frame_size
    framed = np.pad(samples, (0, padding)).reshape(-1, frame_size)
    rms = np.sqrt(np.mean(np.square(framed), axis=1))
    active = rms > 10 ** (-45 / 20)
    active_frames = framed[active]
    if len(active_frames):
        crossings = np.mean(
            np.not_equal(np.signbit(active_frames[:, 1:]), np.signbit(active_frames[:, :-1]))
        )
        spectrum = np.abs(np.fft.rfft(active_frames, axis=1)) + 1e-9
        flatness = np.exp(np.mean(np.log(spectrum), axis=1)) / np.mean(spectrum, axis=1)
        spectral_flatness = float(np.mean(flatness))
    else:
        crossings = 0.0
        spectral_flatness = 1.0
    return {
        "method": "pcm-frame-heuristics-v1",
        "sample_rate": sample_rate,
        "sample_count": len(samples),
        "speech_activity_ratio": round(float(np.mean(active)), 4),
        "active_zero_crossing_ratio": round(float(crossings), 4),
        "active_spectral_flatness": round(spectral_flatness, 4),
    }


def _nonverbal_expected(transcripts):
    words = _tokens(" ".join(transcripts))
    markers = sorted(set(words) & NONVERBAL_WORDS)
    lexical = [word for word in words if word not in NONVERBAL_WORDS]
    return markers, len(lexical)


def _asr_evidence(candidate, transcript):
    transcript = str(transcript or "").strip()
    flags = _transcript_flags(transcript)
    transcript_tokens = _tokens(transcript)
    marker_words = sorted(set(transcript_tokens) & NON_SPEECH_ASR_MARKERS)
    if transcript_tokens and all(re.fullmatch(r"(?:ha)+", token) for token in transcript_tokens):
        marker_words.append("laughter-vocalization")
    similarities = [_text_similarity(transcript, expected) for expected in candidate.transcripts]
    errors = [_word_error_rate(expected, transcript) for expected in candidate.transcripts]
    return {
        "status": "complete",
        "transcript": transcript,
        "flags": flags,
        "non_speech_markers": marker_words,
        "best_similarity": round(max(similarities, default=0.0), 4),
        "best_word_error_rate": min(errors, default=1.0),
    }


def _content_assessment(candidate, acoustic, asr):
    expected_markers, lexical_words = _nonverbal_expected(candidate.transcripts)
    reasons = []
    classification = "uncertain"
    if asr.get("status") == "complete":
        if asr["non_speech_markers"]:
            classification = "non-speech-risk"
            reasons.append("asr-non-speech-marker")
        elif len(_tokens(asr["transcript"])) >= 2:
            classification = "speech-observed"
        else:
            reasons.append("asr-insufficient-lexical-speech")
    if expected_markers:
        reasons.append("expected-transcript-has-nonverbal-marker")
        if lexical_words <= 1 and classification != "speech-observed":
            classification = "non-speech-risk"
    if acoustic["speech_activity_ratio"] < 0.2:
        reasons.append("low-acoustic-activity")
    if acoustic["active_spectral_flatness"] > 0.65:
        reasons.append("broadband-noise-risk")
    obvious_reject = (
        classification == "non-speech-risk"
        and "asr-non-speech-marker" in reasons
        and (lexical_words <= 1 or float(asr.get("best_similarity", 1.0)) < 0.3)
    )
    return {
        "classification": classification,
        "expected_nonverbal_markers": expected_markers,
        "reasons": reasons,
        "obvious_rejection_candidate": obvious_reject,
        "policy": "advisory-only; human review remains authoritative",
    }


def _segment_speaker_evidence(candidate, speaker_embedder):
    if speaker_embedder is None:
        return {"status": "not-run"}, None
    samples, sample_rate = read_pcm_wav(candidate.reference)
    embedding = speaker_embedder(candidate.reference)
    segment_length = round(sample_rate * 1.25)
    segments = [
        samples[start : start + segment_length]
        for start in range(0, len(samples), segment_length)
        if len(samples[start : start + segment_length]) >= sample_rate
    ][:4]
    if len(segments) < 2:
        return {"status": "insufficient-duration"}, embedding
    segment_embeddings = []
    with TemporaryDirectory(prefix="r1999-speaker-segments-") as directory:
        root = Path(directory)
        for index, segment in enumerate(segments):
            path = root / f"segment-{index}.wav"
            write_pcm16_wav(path, segment, sample_rate)
            segment_embeddings.append(speaker_embedder(path))
    similarities = [
        _cosine_similarity(left, right)
        for index, left in enumerate(segment_embeddings)
        for right in segment_embeddings[index + 1 :]
    ]
    minimum = min(similarities)
    return (
        {
            "status": "complete",
            "segment_count": len(segment_embeddings),
            "minimum_segment_similarity": round(minimum, 4),
            "speaker_count_estimate": 1 if minimum >= 0.62 else "multiple-risk",
            "method": "wavlm-segment-consistency-v1",
        },
        embedding,
    )


def _model_identity(path):
    if path is None:
        return None
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise StoryVoiceEvidenceError(f"Local model directory does not exist: {root}")
    digest = hashlib.sha256()
    files = 0
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        file_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative}\0{file_digest}\n".encode())
        files += 1
    if not files:
        raise StoryVoiceEvidenceError(f"Local model directory has no files: {root}")
    return {"path": str(root), "file_count": files, "tree_sha256": digest.hexdigest()}


def load_story_voice_evidence(report_path, evidence_path=None):
    """Load an optional exact-report evidence sidecar and index it by candidate key."""
    try:
        session = load_review_session(report_path)
    except StoryVoiceReviewError as error:
        raise StoryVoiceEvidenceError(str(error)) from error
    path = (
        session.report_path.with_name("evidence.json")
        if evidence_path is None
        else Path(evidence_path).expanduser().resolve()
    )
    if not path.exists():
        return path, {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoryVoiceEvidenceError(f"Unable to read evidence {path}: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != EVIDENCE_SCHEMA
        or document.get("schema_version") != EVIDENCE_VERSION
    ):
        raise StoryVoiceEvidenceError("Unsupported story voice evidence schema")
    if document.get("candidate_report_sha256") != session.report_sha256:
        raise StoryVoiceEvidenceError("Story voice evidence belongs to a different report")
    values = document.get("candidates")
    if not isinstance(values, list) or len(values) != len(session.candidates):
        raise StoryVoiceEvidenceError("Story voice evidence candidate inventory is incomplete")
    known = {candidate.key: candidate for candidate in session.candidates}
    indexed = {}
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise StoryVoiceEvidenceError(f"Evidence candidate {index} must be an object")
        key = value.get("candidate_key")
        candidate = known.get(key)
        if candidate is None or key in indexed:
            raise StoryVoiceEvidenceError(f"Evidence candidate {index} identity is invalid")
        if value.get("candidate_evidence_sha256") != candidate.evidence_sha256:
            raise StoryVoiceEvidenceError(f"Evidence candidate {index} source evidence changed")
        if value.get("reference_sha256") != candidate.reference_sha256:
            raise StoryVoiceEvidenceError(f"Evidence candidate {index} reference changed")
        if not all(
            isinstance(value.get(field), dict)
            for field in ("acoustic", "asr", "content", "speaker")
        ):
            raise StoryVoiceEvidenceError(f"Evidence candidate {index} analysis is invalid")
        indexed[key] = value
    return path, indexed


def analyze_story_voice_evidence(
    report_path,
    output_path,
    *,
    transcriber=None,
    speaker_embedder=None,
    whisper_model=None,
    speaker_model=None,
):
    """Create an immutable advisory sidecar for one exact candidate report."""
    try:
        session = load_review_session(report_path)
    except StoryVoiceReviewError as error:
        raise StoryVoiceEvidenceError(str(error)) from error
    output_path = Path(output_path).expanduser().resolve()
    if output_path.exists():
        raise StoryVoiceEvidenceError(f"Evidence output already exists: {output_path}")
    entries = []
    embeddings = {}
    for candidate in session.candidates:
        acoustic = _acoustic_evidence(candidate.reference)
        if transcriber is None:
            asr = {"status": "not-run"}
        else:
            try:
                asr = _asr_evidence(candidate, transcriber(candidate.reference))
            except Exception as error:
                asr = {"status": "error", "error": str(error)}
        try:
            speaker, embedding = _segment_speaker_evidence(candidate, speaker_embedder)
        except Exception as error:
            speaker, embedding = {"status": "error", "error": str(error)}, None
        if embedding is not None:
            embeddings[candidate.key] = embedding
        entries.append(
            {
                "candidate_key": candidate.key,
                "candidate_evidence_sha256": candidate.evidence_sha256,
                "reference_sha256": candidate.reference_sha256,
                "character": candidate.character,
                "portrait": candidate.portrait,
                "source_bank": candidate.source_bank,
                "media_id": candidate.media_id,
                "acoustic": acoustic,
                "asr": asr,
                "content": _content_assessment(candidate, acoustic, asr),
                "speaker": speaker,
            }
        )
    by_group = defaultdict(list)
    for candidate in session.candidates:
        if candidate.key in embeddings:
            by_group[(candidate.character, candidate.portrait, candidate.source_bank)].append(
                candidate
            )
    entries_by_key = {entry["candidate_key"]: entry for entry in entries}
    for candidates in by_group.values():
        for candidate in candidates:
            peers = [peer for peer in candidates if peer.key != candidate.key]
            similarities = [
                _cosine_similarity(embeddings[candidate.key], embeddings[peer.key])
                for peer in peers
            ]
            entries_by_key[candidate.key]["speaker"]["group_similarity"] = {
                "peer_count": len(peers),
                "minimum": round(min(similarities), 4) if similarities else None,
                "mean": round(float(np.mean(similarities)), 4) if similarities else None,
                "outlier_risk": bool(similarities and float(np.mean(similarities)) < 0.72),
                "policy": "same exact character/portrait/bank group only; no automatic merge",
            }
    document = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_report": session.report_path.name,
        "candidate_report_sha256": session.report_sha256,
        "models": {
            "whisper": _model_identity(whisper_model),
            "speaker": _model_identity(speaker_model),
        },
        "candidate_count": len(entries),
        "candidates": entries,
        "authority": (
            "Advisory evidence only. It can reject an obvious non-speech failure or "
            "prioritize review; it cannot approve a speaker identity or merge variants."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, document)
    return output_path, document


def create_parser():
    parser = argparse.ArgumentParser(
        description="Compute conservative local evidence for story voice candidates"
    )
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--whisper-model", type=Path)
    parser.add_argument("--speaker-model", type=Path)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    output = options.output or options.report.with_name("evidence.json")
    try:
        transcriber = (
            create_local_whisper_transcriber(options.whisper_model)
            if options.whisper_model is not None
            else None
        )
        speaker_embedder = (
            create_local_speaker_embedder(options.speaker_model)
            if options.speaker_model is not None
            else None
        )
        path, document = analyze_story_voice_evidence(
            options.report,
            output,
            transcriber=transcriber,
            speaker_embedder=speaker_embedder,
            whisper_model=options.whisper_model,
            speaker_model=options.speaker_model,
        )
    except (OSError, StoryVoiceEvidenceError) as error:
        print(error, file=sys.stderr)
        return 1
    rejected = sum(
        entry["content"]["obvious_rejection_candidate"] for entry in document["candidates"]
    )
    print(
        f"Wrote advisory evidence for {document['candidate_count']} candidates to {path}; "
        f"{rejected} obvious non-speech rejection candidates"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
