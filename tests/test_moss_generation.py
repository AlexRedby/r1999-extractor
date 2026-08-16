import unittest
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from r1999extractor.bulk_generation import BulkGenerationError, inspect_generated_wav
from r1999extractor.moss_generation import (
    MossGenerationProvider,
    analyze_generated_speech,
    is_spoken_item,
    moss_synthesis_text,
)


class FakeRegistry:
    def resolve(self, character):
        return object() if character == "Ready" else None


@dataclass(frozen=True)
class FakeSynthesisRequest:
    voice: str
    text: str
    seed: int
    generation_profile: str
    cache_policy: str


class FakeCompletion(str, Enum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    LIMITED = "limited"


@dataclass(frozen=True)
class FakeSynthesisResult:
    pcm: np.ndarray
    sample_rate: int
    completion: FakeCompletion


class FakeRenderStream:
    def __init__(self, result):
        self.result = result

    def collect(self):
        return self.result


class FakeBackend:
    model_name = "moss-test"
    generation_profile = "production-test"

    def __init__(
        self,
        *,
        pcm=None,
        sample_rate=24000,
        completion=FakeCompletion.COMPLETE,
    ):
        self.pcm = (
            np.full(12000, 0.25, dtype=np.float32)
            if pcm is None
            else np.asarray(pcm, dtype=np.float32)
        )
        self.sample_rate = sample_rate
        self.completion = completion
        self.requests = []
        self.prepare_calls = 0
        self.play_calls = 0

    def prepare(self, character, text):
        del character, text
        self.prepare_calls += 1
        raise AssertionError("pregeneration must not prepare playback")

    def play(self, prepared):
        del prepared
        self.play_calls += 1
        raise AssertionError("pregeneration must not open playback")

    def render(self, request):
        self.requests.append(request)
        return FakeRenderStream(
            FakeSynthesisResult(
                pcm=self.pcm,
                sample_rate=self.sample_rate,
                completion=self.completion,
            )
        )


def make_provider(backend=None):
    backend = backend or FakeBackend()
    return (
        MossGenerationProvider(
            backend,
            FakeRegistry(),
            synthesis_request_factory=FakeSynthesisRequest,
            bypass_cache_policy="bypass",
        ),
        backend,
    )


class MossGenerationProviderTest(unittest.TestCase):
    def test_renders_without_preparing_or_opening_playback(self):
        provider, backend = make_provider()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            provider.generate(
                {"voice_character": "Ready", "text": "I wonder …"},
                path,
                seed=7,
            )
            quality = inspect_generated_wav(path)

        self.assertEqual(quality.sample_rate, 24000)
        self.assertEqual(quality.duration_seconds, 0.5)
        self.assertEqual(backend.prepare_calls, 0)
        self.assertEqual(backend.play_calls, 0)
        self.assertEqual(
            backend.requests,
            [
                FakeSynthesisRequest(
                    voice="Ready",
                    text="I wonder.",
                    seed=7,
                    generation_profile="production-test",
                    cache_policy="bypass",
                )
            ],
        )

    def test_downmixes_stereo_without_flattening_channels_into_time(self):
        provider, _backend = make_provider(
            FakeBackend(
                pcm=np.column_stack(
                    (
                        np.full(12000, 0.2, dtype=np.float32),
                        np.full(12000, 0.4, dtype=np.float32),
                    )
                )
            )
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            provider.generate(
                {"voice_character": "Ready", "text": "A generated line."},
                path,
                seed=7,
            )
            quality = inspect_generated_wav(path)

        self.assertEqual(quality.duration_seconds, 0.5)
        self.assertAlmostEqual(quality.peak, 0.3, places=3)

    def test_rejects_unknown_character_before_generation(self):
        provider, backend = make_provider()

        with self.assertRaisesRegex(BulkGenerationError, "No MOSS voice reference"):
            provider.generate(
                {"voice_character": "Missing", "text": "A line."},
                Path("unused.wav"),
                seed=0,
            )
        self.assertEqual(backend.requests, [])

    def assert_completion_is_not_published(self, completion, message):
        with TemporaryDirectory() as directory:
            provider, backend = make_provider(FakeBackend(completion=completion))
            output = Path(directory) / f"{completion.value}.wav"

            with self.assertRaisesRegex(BulkGenerationError, message):
                provider.generate(
                    {"voice_character": "Ready", "text": "An incomplete line."},
                    output,
                    seed=0,
                )

            self.assertFalse(output.exists())
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(backend.play_calls, 0)

    def test_does_not_publish_limited_render(self):
        self.assert_completion_is_not_published(FakeCompletion.LIMITED, "before EOS")

    def test_does_not_publish_cancelled_render(self):
        self.assert_completion_is_not_published(FakeCompletion.CANCELLED, "cancelled")

    def test_rejects_long_silence_inside_generated_speech(self):
        provider, _backend = make_provider(
            FakeBackend(
                pcm=np.concatenate(
                    (
                        np.full(9600, 0.25, dtype=np.float32),
                        np.zeros(38400, dtype=np.float32),
                        np.full(9600, 0.25, dtype=np.float32),
                    )
                )
            )
        )

        with TemporaryDirectory() as directory:
            output = Path(directory) / "invalid.wav"
            with self.assertRaisesRegex(BulkGenerationError, "internal silence"):
                provider.generate(
                    {"voice_character": "Ready", "text": "A broken line."},
                    output,
                    seed=0,
                )

            self.assertFalse(output.exists())

    def test_reports_leading_trailing_and_internal_silence(self):
        sample_rate = 1000
        quality = analyze_generated_speech(
            np.concatenate(
                (
                    np.zeros(800),
                    np.ones(400),
                    np.zeros(1600),
                    np.ones(400),
                    np.zeros(960),
                )
            ),
            sample_rate,
        )

        self.assertEqual(quality.leading_silence_seconds, 0.8)
        self.assertEqual(quality.trailing_silence_seconds, 0.96)
        self.assertEqual(quality.longest_internal_silence_seconds, 1.6)

    def test_skips_pure_sound_effect_but_keeps_spoken_stage_direction(self):
        self.assertFalse(is_spoken_item({"text": "*chirp-chirp*"}))
        self.assertFalse(is_spoken_item({"text": '"*bang*"'}))
        self.assertTrue(is_spoken_item({"text": "*cough* Who is there?"}))

    def test_normalizes_short_trailing_ellipsis_that_can_cause_runaway_audio(self):
        self.assertEqual(moss_synthesis_text("This ..."), "This.")
        self.assertEqual(moss_synthesis_text("I wonder …"), "I wonder.")
        self.assertEqual(
            moss_synthesis_text("This is a longer hesitant sentence ..."),
            "This is a longer hesitant sentence ...",
        )


if __name__ == "__main__":
    unittest.main()
