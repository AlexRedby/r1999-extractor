import argparse
import json
import re
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


class CaptureStream:
    def __init__(self):
        self.chunks = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def write(self, chunk):
        self.chunks.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
        return False

    def abort(self):
        return None


class CaptureAudioOutput:
    def __init__(self):
        self.streams = []

    def OutputStream(self, **options):
        del options
        stream = CaptureStream()
        self.streams.append(stream)
        return stream

    def stop(self):
        return None


class MossGenerationProvider:
    provider = "vntts"

    def __init__(self, backend, registry, audio_output):
        self.backend = backend
        self.registry = registry
        self.audio_output = audio_output
        self.model = backend.model_name

    def generate(self, item, output, *, seed):
        character = item["voice_character"]
        if character != "Narrator" and self.registry.resolve(character) is None:
            raise BulkGenerationError(f"No MOSS voice reference for {character!r}")
        stream_count = len(self.audio_output.streams)
        try:
            mlx = getattr(self.backend, "_mlx", None)
            if mlx is not None:
                mlx.random.seed(seed)
            prepared = self.backend.prepare(character, item["text"])
            if not self.backend.play(prepared):
                raise BulkGenerationError(f"MOSS generation was cancelled for {character!r}")
            streams = self.audio_output.streams[stream_count:]
            chunks = [chunk for stream in streams for chunk in stream.chunks]
            if not chunks:
                raise BulkGenerationError(f"MOSS generated no audio for {character!r}")
            audio = np.concatenate(chunks)
            write_pcm16_wav(output, audio, self.backend.sample_rate)
        except BulkGenerationError:
            raise
        except Exception as error:
            raise BulkGenerationError(
                f"MOSS generation failed for {character!r}: {error}"
            ) from error
        finally:
            del self.audio_output.streams[stream_count:]

    def stop(self):
        stop = getattr(self.backend, "stop", None)
        if callable(stop):
            stop()


def create_provider(manifest, narrator_character, model_name=None):
    try:
        from vntts.speech_backend import MossTTSVoiceRouterBackend
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
    audio_output = CaptureAudioOutput()
    backend = MossTTSVoiceRouterBackend(
        registry,
        narrator_reference=narrator_voice.references[0],
        model_name=model_name,
        audio_output=audio_output,
    )
    return MossGenerationProvider(backend, registry, audio_output)


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
