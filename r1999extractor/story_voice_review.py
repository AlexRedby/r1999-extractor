"""Checksum-bound review decisions for prepared Character Story references."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from r1999extractor.story_voice_candidates import REPORT_SCHEMA, REPORT_VERSION

REVIEW_SCHEMA = "r1999.story-voice-reference-review"
REVIEW_VERSION = 2
LEGACY_REVIEW_VERSION = 1
REVIEW_DECISIONS = frozenset({"accept", "reject", "uncertain"})


class StoryVoiceReviewError(RuntimeError):
    """A candidate report or its checksum-bound review is invalid."""


@dataclass(frozen=True)
class ReviewCandidate:
    key: str
    character: str
    portrait: str | None
    source_bank: str
    media_id: int
    reference: Path
    reference_relative: str
    reference_sha256: str
    technical_pass: bool
    transcript_conflict: bool
    recommended: bool
    transcripts: tuple[str, ...]
    line_ids: tuple[str, ...]
    duration_seconds: float | None
    quality_score: int | None
    technical_flags: tuple[str, ...]
    contexts: tuple[tuple[str | None, str | None], ...]
    collection_titles: tuple[str | None, ...]
    affected_character_line_count: int | None
    affected_portrait_line_count: int | None
    evidence_sha256: str


@dataclass(frozen=True)
class ReviewSession:
    report_path: Path
    report_sha256: str
    review_path: Path
    candidates: tuple[ReviewCandidate, ...]
    decisions: dict[str, dict]
    invalidated_decisions: tuple[dict, ...] = ()

    @property
    def pending_count(self):
        return len(self.candidates) - len(self.decisions)


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise StoryVoiceReviewError(f"{label} must be non-empty text")
    return value.strip()


def _sha256_text(value, label):
    value = _required_text(value, label)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise StoryVoiceReviewError(f"{label} must be lowercase SHA-256")
    return value


def _candidate_key(character, portrait, bank, media_id, reference_sha256):
    identity = json.dumps(
        [character, portrait, bank, media_id, reference_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _read_json_snapshot(path, label):
    path = Path(path).expanduser().resolve()
    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoryVoiceReviewError(f"Unable to read {label} {path}: {error}") from error
    if not isinstance(document, dict):
        raise StoryVoiceReviewError(f"{label.title()} must be a JSON object")
    return path, payload, document


def _recommended_identities(report):
    identities = set()
    groups = report.get("groups")
    if not isinstance(groups, list):
        raise StoryVoiceReviewError("Candidate report groups must be a list")
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise StoryVoiceReviewError(f"Candidate group {index} must be an object")
        media_ids = group.get("recommended_media_ids_for_audition")
        if not isinstance(media_ids, list) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in media_ids
        ):
            raise StoryVoiceReviewError(f"Candidate group {index} recommendations are invalid")
        for media_id in media_ids:
            identities.add(
                (
                    _required_text(group.get("character"), f"group {index} character"),
                    group.get("portrait"),
                    _required_text(group.get("source_bank"), f"group {index} bank"),
                    media_id,
                )
            )
    return identities


def _load_candidates(report_path, report):
    recommended = _recommended_identities(report)
    values = report.get("candidates")
    if not isinstance(values, list) or not values:
        raise StoryVoiceReviewError("Candidate report contains no candidates")
    candidates = []
    seen = set()
    root = report_path.parent.resolve()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise StoryVoiceReviewError(f"Candidate {index} must be an object")
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        character = _required_text(value.get("character"), f"candidate {index} character")
        portrait = value.get("portrait")
        if portrait is not None and (not isinstance(portrait, str) or not portrait.strip()):
            raise StoryVoiceReviewError(f"Candidate {index} portrait is invalid")
        bank = _required_text(value.get("source_bank"), f"candidate {index} bank")
        media_id = value.get("media_id")
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 0:
            raise StoryVoiceReviewError(f"Candidate {index} media ID is invalid")
        reference_relative = _required_text(value.get("reference"), f"candidate {index} reference")
        relative = PurePosixPath(reference_relative)
        if relative.is_absolute() or ".." in relative.parts or "\\" in reference_relative:
            raise StoryVoiceReviewError(f"Candidate {index} reference is not contained")
        reference = (root / Path(*relative.parts)).resolve()
        try:
            reference.relative_to(root)
        except ValueError as error:
            raise StoryVoiceReviewError(
                f"Candidate {index} reference escapes its report"
            ) from error
        if reference.is_symlink() or not reference.is_file():
            raise StoryVoiceReviewError(f"Candidate {index} reference is missing or unsafe")
        reference_sha256 = _sha256_text(
            value.get("reference_sha256"), f"candidate {index} reference hash"
        )
        if sha256_file(reference) != reference_sha256:
            raise StoryVoiceReviewError(f"Candidate {index} reference checksum changed")
        source_lines = value.get("source_lines")
        if not isinstance(source_lines, list) or not source_lines:
            raise StoryVoiceReviewError(f"Candidate {index} source lines are missing")
        transcripts = tuple(
            _required_text(line.get("text"), f"candidate {index} transcript")
            for line in source_lines
            if isinstance(line, dict)
        )
        if len(transcripts) != len(source_lines):
            raise StoryVoiceReviewError(f"Candidate {index} source line is invalid")
        line_ids = tuple(
            str(line.get("line_id") or "").strip()
            for line in source_lines
            if isinstance(line, dict)
        )
        contexts = tuple(
            (
                str(line.get("previous_text") or "").strip() or None,
                str(line.get("next_text") or "").strip() or None,
            )
            for line in source_lines
        )
        collection_titles = tuple(
            str(line.get("collection_title") or "").strip() or None for line in source_lines
        )
        affected_character_line_count = value.get("affected_character_line_count")
        affected_portrait_line_count = value.get("affected_portrait_line_count")
        for count, label in (
            (affected_character_line_count, "affected character line count"),
            (affected_portrait_line_count, "affected portrait line count"),
        ):
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                raise StoryVoiceReviewError(f"Candidate {index} {label} is invalid")
        metrics = value.get("metrics")
        if metrics is not None and not isinstance(metrics, dict):
            raise StoryVoiceReviewError(f"Candidate {index} metrics must be an object")
        metrics = metrics or {}
        duration_seconds = metrics.get("duration_seconds")
        if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
            duration_seconds = None
        quality_score = metrics.get("quality_score")
        if not isinstance(quality_score, int) or isinstance(quality_score, bool):
            quality_score = None
        technical_flags = metrics.get("technical_flags", [])
        if not isinstance(technical_flags, list) or any(
            not isinstance(flag, str) or not flag.strip() for flag in technical_flags
        ):
            raise StoryVoiceReviewError(f"Candidate {index} technical flags are invalid")
        key = _candidate_key(character, portrait, bank, media_id, reference_sha256)
        if key in seen:
            raise StoryVoiceReviewError(f"Duplicate candidate identity: {key}")
        seen.add(key)
        candidates.append(
            ReviewCandidate(
                key=key,
                character=character,
                portrait=portrait,
                source_bank=bank,
                media_id=media_id,
                reference=reference,
                reference_relative=reference_relative,
                reference_sha256=reference_sha256,
                technical_pass=value.get("technical_pass") is True,
                transcript_conflict=value.get("transcript_conflict") is True,
                recommended=(character, portrait, bank, media_id) in recommended,
                transcripts=transcripts,
                line_ids=line_ids,
                duration_seconds=(
                    float(duration_seconds) if duration_seconds is not None else None
                ),
                quality_score=quality_score,
                technical_flags=tuple(flag.strip() for flag in technical_flags),
                contexts=contexts,
                collection_titles=collection_titles,
                affected_character_line_count=affected_character_line_count,
                affected_portrait_line_count=affected_portrait_line_count,
                evidence_sha256=evidence_sha256,
            )
        )
    return tuple(candidates)


def load_review_session(report_path, review_path=None):
    report_path, report_payload, report = _read_json_snapshot(report_path, "candidate report")
    if report.get("schema") != REPORT_SCHEMA or report.get("schema_version") != REPORT_VERSION:
        raise StoryVoiceReviewError("Unsupported candidate report schema")
    candidates = _load_candidates(report_path, report)
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    review_path = (
        report_path.with_name("review.json")
        if review_path is None
        else Path(review_path).expanduser().resolve()
    )
    decisions = {}
    invalidated_decisions = []
    if review_path.exists():
        _path, _payload, review = _read_json_snapshot(review_path, "candidate review")
        version = review.get("schema_version")
        if review.get("schema") != REVIEW_SCHEMA or version not in {
            LEGACY_REVIEW_VERSION,
            REVIEW_VERSION,
        }:
            raise StoryVoiceReviewError("Unsupported candidate review schema")
        _sha256_text(review.get("candidate_report_sha256"), "candidate review report hash")
        if (
            version == LEGACY_REVIEW_VERSION
            and review.get("candidate_report_sha256") != report_sha256
        ):
            raise StoryVoiceReviewError("Candidate report changed since this review was recorded")
        known = {candidate.key: candidate for candidate in candidates}
        values = review.get("decisions")
        if not isinstance(values, list):
            raise StoryVoiceReviewError("Candidate review decisions must be a list")
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise StoryVoiceReviewError(f"Review decision {index} must be an object")
            key = _required_text(value.get("candidate_key"), f"decision {index} key")
            candidate = known.get(key)
            if key in decisions:
                raise StoryVoiceReviewError(f"Review decision {index} candidate is duplicated")
            if value.get("decision") not in REVIEW_DECISIONS:
                raise StoryVoiceReviewError(f"Review decision {index} value is invalid")
            reference_sha256 = _sha256_text(
                value.get("reference_sha256"), f"decision {index} reference hash"
            )
            if candidate is None:
                if version == LEGACY_REVIEW_VERSION:
                    raise StoryVoiceReviewError(f"Review decision {index} candidate is invalid")
                invalidated_decisions.append(value)
                continue
            if reference_sha256 != candidate.reference_sha256:
                raise StoryVoiceReviewError(f"Review decision {index} reference changed")
            if version == REVIEW_VERSION:
                evidence_sha256 = _sha256_text(
                    value.get("candidate_evidence_sha256"),
                    f"decision {index} evidence hash",
                )
                if evidence_sha256 != candidate.evidence_sha256:
                    invalidated_decisions.append(value)
                    continue
            decisions[key] = value
        archived = review.get("invalidated_decisions", [])
        if version == REVIEW_VERSION:
            if not isinstance(archived, list) or any(
                not isinstance(value, dict) for value in archived
            ):
                raise StoryVoiceReviewError("Invalidated review decisions must be a list")
            for index, value in enumerate(archived):
                _required_text(value.get("candidate_key"), f"invalidated decision {index} key")
                _sha256_text(
                    value.get("reference_sha256"),
                    f"invalidated decision {index} reference hash",
                )
                if value.get("decision") not in REVIEW_DECISIONS:
                    raise StoryVoiceReviewError(
                        f"Invalidated review decision {index} value is invalid"
                    )
            invalidated_decisions.extend(archived)
    return ReviewSession(
        report_path=report_path,
        report_sha256=report_sha256,
        review_path=review_path,
        candidates=candidates,
        decisions=decisions,
        invalidated_decisions=tuple(invalidated_decisions),
    )


def record_review_decision(report_path, candidate_key, decision, *, notes="", review_path=None):
    if decision not in REVIEW_DECISIONS:
        raise StoryVoiceReviewError(
            f"Decision must be one of: {', '.join(sorted(REVIEW_DECISIONS))}"
        )
    if not isinstance(notes, str):
        raise StoryVoiceReviewError("Decision notes must be text")
    session = load_review_session(report_path, review_path)
    candidate = next((item for item in session.candidates if item.key == candidate_key), None)
    if candidate is None:
        raise StoryVoiceReviewError(f"Unknown candidate key: {candidate_key}")
    decisions = dict(session.decisions)
    decisions[candidate.key] = {
        "candidate_key": candidate.key,
        "character": candidate.character,
        "portrait": candidate.portrait,
        "source_bank": candidate.source_bank,
        "media_id": candidate.media_id,
        "reference": candidate.reference_relative,
        "reference_sha256": candidate.reference_sha256,
        "candidate_evidence_sha256": candidate.evidence_sha256,
        "decision": decision,
        "notes": notes.strip(),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    if sha256_file(session.report_path) != session.report_sha256:
        raise StoryVoiceReviewError("Candidate report changed before the decision was saved")
    if sha256_file(candidate.reference) != candidate.reference_sha256:
        raise StoryVoiceReviewError("Candidate reference changed before the decision was saved")
    current_decisions = []
    for key in sorted(decisions):
        current = next(item for item in session.candidates if item.key == key)
        current_decisions.append(
            {
                **decisions[key],
                "candidate_evidence_sha256": current.evidence_sha256,
            }
        )
    document = {
        "schema": REVIEW_SCHEMA,
        "schema_version": REVIEW_VERSION,
        "candidate_report": session.report_path.name,
        "candidate_report_sha256": session.report_sha256,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "decisions": current_decisions,
        "invalidated_decisions": list(session.invalidated_decisions),
    }
    atomic_write_json(session.review_path, document)
    return load_review_session(session.report_path, session.review_path)


def create_parser():
    parser = argparse.ArgumentParser(description="Review checksum-bound story voice candidates")
    parser.add_argument("report", type=Path)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--candidate-key")
    parser.add_argument("--decision", choices=sorted(REVIEW_DECISIONS))
    parser.add_argument("--notes", default="")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        if bool(options.candidate_key) != bool(options.decision):
            raise StoryVoiceReviewError("--candidate-key and --decision must be supplied together")
        if options.decision:
            session = record_review_decision(
                options.report,
                options.candidate_key,
                options.decision,
                notes=options.notes,
                review_path=options.review,
            )
        else:
            session = load_review_session(options.report, options.review)
    except StoryVoiceReviewError as error:
        print(error, file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "report_sha256": session.report_sha256,
                "candidate_count": len(session.candidates),
                "decision_count": len(session.decisions),
                "pending_count": session.pending_count,
                "candidates": [
                    {
                        "candidate_key": candidate.key,
                        "character": candidate.character,
                        "portrait": candidate.portrait,
                        "source_bank": candidate.source_bank,
                        "media_id": candidate.media_id,
                        "recommended": candidate.recommended,
                        "technical_pass": candidate.technical_pass,
                        "transcript_conflict": candidate.transcript_conflict,
                        "transcripts": candidate.transcripts,
                        "decision": session.decisions.get(candidate.key, {}).get("decision"),
                    }
                    for candidate in session.candidates
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
