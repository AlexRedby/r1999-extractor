"""Checksum-bound duration measurement for exact installed Wwise media."""

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.story_index import (
    StoryIndexError,
    load_story_index_document,
    write_story_index_document,
)

from r1999extractor.reverse1999_index import bank_index_staleness_reasons
from r1999extractor.story_audio import (
    AudioResolution,
    StoryAudioResolutionError,
    StoryAudioResolver,
)
from r1999extractor.wwise import AudioConversionError, resolve_decoder


@dataclass(frozen=True)
class SourceAudioTiming:
    duration_seconds: float
    media_id: int
    media_sha256: str
    sample_rate: int
    sample_count: int
    decoder_version: str


class SourceAudioDurationProbe:
    """Measure only events bound to one exact installed media object."""

    def __init__(self, resolver, *, decoder="vgmstream-cli", runner=subprocess.run):
        self.resolver = resolver
        self.decoder = resolve_decoder(decoder)
        self.runner = runner
        self._cache = {}

    def probe(self, resolution):
        try:
            media = self.resolver.read_single_available_media(resolution)
        except (OSError, StoryAudioResolutionError):
            return None
        if media is None:
            return None
        media_id, payload = media
        digest = hashlib.sha256(payload).hexdigest()
        if digest in self._cache:
            cached = self._cache[digest]
            return SourceAudioTiming(
                cached.duration_seconds,
                media_id,
                digest,
                cached.sample_rate,
                cached.sample_count,
                cached.decoder_version,
            )
        try:
            with TemporaryDirectory(prefix="r1999-source-duration-") as temporary_directory:
                source = Path(temporary_directory) / f"{media_id}.wem"
                source.write_bytes(payload)
                result = self.runner(
                    [self.decoder, "-i", "-I", str(source)],
                    capture_output=True,
                    text=True,
                )
            if result.returncode:
                return None
            document = json.loads(result.stdout)
            sample_rate = _positive_int(document.get("sampleRate"))
            sample_count = _positive_int(
                document.get("playSamples", document.get("numberOfSamples"))
            )
            decoder_version = str(document.get("version") or "").strip()
            if sample_rate is None or sample_count is None or not decoder_version:
                return None
            duration = sample_count / sample_rate
            if not math.isfinite(duration) or not 0 < duration <= 600:
                return None
        except (OSError, TypeError, ValueError, json.JSONDecodeError, AudioConversionError):
            return None
        timing = SourceAudioTiming(
            round(duration, 6),
            media_id,
            digest,
            sample_rate,
            sample_count,
            decoder_version,
        )
        self._cache[digest] = timing
        return timing


def classify_source_audio_completeness(text, timing):
    """Prove only clearly impossible full readings; never infer `full`."""
    words = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*", str(text))
    characters = sum(len(word) for word in words)
    if not words:
        return "unknown", "no-spoken-text-evidence"
    minimum_plausible_seconds = max(len(words) / 6.0, characters / 24.0)
    if timing.duration_seconds + 0.05 < minimum_plausible_seconds:
        return "partial", "duration-too-short-for-displayed-text"
    return "unknown", "duration-plausible-but-semantic-coverage-unverified"


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def annotate_story_index_source_audio_durations(
    story_index,
    bank_index,
    output,
    *,
    chapters=(),
    decoder="vgmstream-cli",
    probe=None,
):
    """Publish a new story index with verified timing on selected source voices."""
    source = Path(story_index).expanduser().resolve()
    destination = Path(output).expanduser().resolve()
    if source == destination:
        raise StoryAudioResolutionError(
            "Source-audio timing must publish a new story index, not mutate its input"
        )
    document = load_story_index_document(source)
    selected_chapters = {str(chapter).strip() for chapter in chapters if str(chapter).strip()}
    if probe is None:
        bank_index_path = Path(bank_index).expanduser().resolve()
        try:
            bank_document = json.loads(bank_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoryAudioResolutionError(
                f"Unable to read bank index {bank_index_path}: {error}"
            ) from error
        reasons = bank_index_staleness_reasons(bank_document)
        if reasons:
            raise StoryAudioResolutionError("Bank index is stale: " + "; ".join(reasons))
        probe = SourceAudioDurationProbe(
            StoryAudioResolver({}, bank_document),
            decoder=decoder,
        )

    records = []
    eligible = 0
    measured = 0
    for line in document.records:
        record = line.to_record()
        if (
            line.source_audio_status != "available"
            or selected_chapters
            and line.chapter not in selected_chapters
        ):
            records.append(record)
            continue
        eligible += 1
        resolution = _audio_resolution(record)
        timing = probe.probe(resolution) if resolution is not None else None
        if timing is not None:
            measured += 1
            completeness, completeness_reason = classify_source_audio_completeness(
                line.text,
                timing,
            )
            record.update(
                source_audio_duration_seconds=timing.duration_seconds,
                source_audio_duration_media_id=timing.media_id,
                source_audio_duration_media_sha256=timing.media_sha256,
                source_audio_duration_sample_rate=timing.sample_rate,
                source_audio_duration_sample_count=timing.sample_count,
                source_audio_duration_decoder=timing.decoder_version,
                source_audio_completeness=completeness,
                source_audio_completeness_reason=completeness_reason,
            )
        records.append(record)

    metadata = dict(document.metadata)
    metadata["source_audio_completion"] = "verified-media-duration-seconds"
    metadata["source_audio_timing"] = {
        "method": "exact-wem-decoder-sample-count",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selected_chapters": sorted(selected_chapters),
        "eligible_count": eligible,
        "measured_count": measured,
        "untimed_count": eligible - measured,
    }
    return write_story_index_document(destination, metadata, records)


def _audio_resolution(record):
    media_ids = _media_ids(record.get("source_media_ids"))
    available_media_ids = _media_ids(record.get("available_media_ids"))
    bank = str(record.get("source_bank") or "").strip() or None
    if media_ids is None or available_media_ids is None or bank is None:
        return None
    return AudioResolution(
        "installed",
        "resolved_local_media",
        audio_id=str(record.get("source_audio_id") or "").strip() or None,
        event=str(record.get("source_event") or "").strip() or None,
        bank=bank,
        media_ids=media_ids,
        available_media_ids=available_media_ids,
    )


def _media_ids(value):
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        return None
    return tuple(value)


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Publish a story index whose original game-audio completion times "
            "are bound to exact installed WEM checksums."
        )
    )
    parser.add_argument("--story-index", type=Path, required=True)
    parser.add_argument("--bank-index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chapter", action="append", default=[])
    parser.add_argument("--decoder", default="vgmstream-cli")
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    try:
        result = annotate_story_index_source_audio_durations(
            options.story_index,
            options.bank_index,
            options.output,
            chapters=options.chapter,
            decoder=options.decoder,
        )
    except (OSError, StoryAudioResolutionError, StoryIndexError) as error:
        print(error, file=sys.stderr)
        return 1
    timing = result.metadata["source_audio_timing"]
    print(
        f"Measured {timing['measured_count']}/{timing['eligible_count']} exact source "
        f"voices; wrote {result.path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
