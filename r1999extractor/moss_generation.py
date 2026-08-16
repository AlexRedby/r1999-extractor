import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

from r1999extractor.bulk_generation import (
    BulkGenerationError,
    default_output,
    load_generation_queue,
    run_bulk_generation,
)
from r1999extractor.cli import cli_error
from r1999extractor.settings import get_local_data_directory

default_queue = get_local_data_directory() / "reverse1999" / "generation-queue.jsonl"
pure_sound_effect_pattern = re.compile(r'^\s*["“”]?\*[^*]+\*["“”]?[.!?]?\s*$')
short_trailing_ellipsis_pattern = re.compile(
    r"^\s*(?P<spoken>[\w'’]+(?:\s+[\w'’]+)?)\s*(?:\.{3}|…)\s*$"
)
silence_dbfs = -45.0
silence_frame_ms = 80
max_leading_silence_seconds = 0.8
max_trailing_silence_seconds = 0.8
max_internal_silence_seconds = 1.2
max_silence_ratio = 0.5


@dataclass(frozen=True)
class GeneratedSpeechQuality:
    silence_ratio: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    longest_internal_silence_seconds: float


class MossGenerationProvider:
    provider = "vntts"

    def __init__(
        self,
        backend,
        registry,
        *,
        synthesis_request_factory,
        bypass_cache_policy,
    ):
        self.backend = backend
        self.registry = registry
        self.synthesis_request_factory = synthesis_request_factory
        self.bypass_cache_policy = bypass_cache_policy
        self.model = backend.model_name

    def generate(self, item, output, *, seed):
        character = item["voice_character"]
        if character != "Narrator" and self.registry.resolve(character) is None:
            raise BulkGenerationError(f"No MOSS voice reference for {character!r}")
        try:
            request = self.synthesis_request_factory(
                voice=character,
                text=moss_synthesis_text(item["text"]),
                seed=seed,
                generation_profile=getattr(self.backend, "generation_profile", "stable"),
                cache_policy=self.bypass_cache_policy,
            )
            rendered = self.backend.render(request).collect()
            completion = getattr(rendered.completion, "value", rendered.completion)
            if completion == "cancelled":
                raise BulkGenerationError(f"MOSS generation was cancelled for {character!r}")
            if completion == "limited":
                raise BulkGenerationError(
                    f"MOSS generation for {character!r} hit the text-length audio limit before EOS"
                )
            if completion != "complete":
                raise BulkGenerationError(
                    f"MOSS generation for {character!r} returned unknown completion {completion!r}"
                )
            audio = normalize_rendered_audio(rendered.pcm)
            validate_generated_speech(audio, rendered.sample_rate, character=character)
            write_pcm16_wav(output, audio, rendered.sample_rate)
        except BulkGenerationError:
            raise
        except Exception as error:
            raise BulkGenerationError(
                f"MOSS generation failed for {character!r}: {error}"
            ) from error

    def stop(self):
        stop = getattr(self.backend, "stop", None)
        if callable(stop):
            stop()


def create_provider(manifest, narrator_character, model_name=None):
    try:
        from vntts.speech_backend import MossTTSVoiceRouterBackend
        from vntts.synthesis import SynthesisCachePolicy, SynthesisRequest
        from vntts.voices import CharacterVoiceRegistry
    except ImportError as error:
        raise BulkGenerationError(
            "VNTTS is required for MOSS generation. Run this command with the "
            "VisualNovelTextToSpeach project Python environment."
        ) from error

    registry = CharacterVoiceRegistry.from_file(manifest)
    narrator_voice = registry.resolve(narrator_character)
    if narrator_voice is None or not narrator_voice.references:
        raise BulkGenerationError(
            f"Narrator voice {narrator_character!r} has no reference recording"
        )
    backend = MossTTSVoiceRouterBackend(
        registry,
        narrator_reference=narrator_voice.references[0],
        model_name=model_name,
    )
    return MossGenerationProvider(
        backend,
        registry,
        synthesis_request_factory=SynthesisRequest,
        bypass_cache_policy=SynthesisCachePolicy.BYPASS,
    )


def available_queue_characters(queue, registry):
    _metadata, items = load_generation_queue(queue)
    available = {"Narrator"}
    missing = set()
    for item in items:
        character = item["voice_character"]
        if character == "Narrator" or registry.resolve(character) is not None:
            available.add(character)
        else:
            missing.add(character)
    return available, missing


def is_spoken_item(item):
    text = str(item.get("text") or "")
    return pure_sound_effect_pattern.fullmatch(text) is None


def moss_synthesis_text(text):
    text = str(text or "")
    match = short_trailing_ellipsis_pattern.fullmatch(text)
    if match is None:
        return text
    return match.group("spoken") + "."


def normalize_rendered_audio(audio):
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 2:
        if samples.shape[1] not in {1, 2}:
            raise BulkGenerationError(f"Unsupported MOSS channel count: {samples.shape[1]}")
        samples = np.mean(samples, axis=1, dtype=np.float32)
    elif samples.ndim != 1:
        raise BulkGenerationError(f"Unsupported MOSS audio shape: {samples.shape}")
    return samples


def analyze_generated_speech(audio, sample_rate):
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if sample_rate <= 0 or samples.size == 0:
        raise BulkGenerationError("MOSS generated empty audio")
    frame_samples = max(1, round(sample_rate * silence_frame_ms / 1000))
    frame_rms = np.asarray(
        [
            np.sqrt(np.mean(samples[start : start + frame_samples] ** 2))
            for start in range(0, samples.size, frame_samples)
        ]
    )
    silent = frame_rms <= 10 ** (silence_dbfs / 20.0)
    active_indices = np.flatnonzero(~silent)
    if not len(active_indices):
        duration = samples.size / sample_rate
        return GeneratedSpeechQuality(1.0, duration, duration, 0.0)

    first_active = int(active_indices[0])
    last_active = int(active_indices[-1])
    leading_frames = first_active
    trailing_frames = len(silent) - last_active - 1
    longest_internal_frames = 0
    current_internal_frames = 0
    for is_silent in silent[first_active + 1 : last_active]:
        if is_silent:
            current_internal_frames += 1
            longest_internal_frames = max(
                longest_internal_frames,
                current_internal_frames,
            )
        else:
            current_internal_frames = 0
    frame_seconds = frame_samples / sample_rate
    return GeneratedSpeechQuality(
        silence_ratio=round(float(np.mean(silent)), 4),
        leading_silence_seconds=round(leading_frames * frame_seconds, 3),
        trailing_silence_seconds=round(trailing_frames * frame_seconds, 3),
        longest_internal_silence_seconds=round(
            longest_internal_frames * frame_seconds,
            3,
        ),
    )


def validate_generated_speech(audio, sample_rate, *, character="Narrator"):
    quality = analyze_generated_speech(audio, sample_rate)
    failures = []
    if quality.leading_silence_seconds > max_leading_silence_seconds:
        failures.append(f"{quality.leading_silence_seconds:.2f}s leading silence")
    if quality.trailing_silence_seconds > max_trailing_silence_seconds:
        failures.append(f"{quality.trailing_silence_seconds:.2f}s trailing silence")
    if quality.longest_internal_silence_seconds > max_internal_silence_seconds:
        failures.append(f"{quality.longest_internal_silence_seconds:.2f}s internal silence")
    if quality.silence_ratio > max_silence_ratio:
        failures.append(f"{quality.silence_ratio:.0%} silent frames")
    if failures:
        raise BulkGenerationError(
            f"MOSS output for {character!r} failed speech quality: " + ", ".join(failures)
        )
    return quality


def create_parser():
    parser = argparse.ArgumentParser(
        description="Generate a resumable Reverse: 1999 queue with one persistent MOSS process."
    )
    parser.add_argument("--queue", type=Path, default=default_queue)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--voice-manifest", type=Path, required=True)
    parser.add_argument("--narrator-character", default="Matilda")
    parser.add_argument("--model")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def main(arguments=None):
    options = create_parser().parse_args(arguments)
    provider = None
    try:
        provider = create_provider(
            options.voice_manifest.expanduser().resolve(),
            options.narrator_character,
            options.model,
        )
        available, missing = available_queue_characters(options.queue, provider.registry)
        result = run_bulk_generation(
            options.queue,
            options.output,
            provider,
            limit=options.limit,
            retries=options.retries,
            include_characters=available,
            item_filter=is_spoken_item,
            seed=options.seed,
        )
    except (BulkGenerationError, OSError, json.JSONDecodeError) as error:
        return cli_error(error)
    finally:
        if provider is not None:
            provider.stop()
    missing_text = ", ".join(sorted(missing, key=str.casefold)) or "none"
    print(
        f"Generated {result['generated']} item(s), {result['failed']} failed, "
        f"{result['skipped_characters']} skipped without references, "
        f"{result['skipped_items']} pure sound effects skipped. "
        f"Missing voices: {missing_text}. State: {result['state']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
