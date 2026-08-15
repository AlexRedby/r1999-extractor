import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from r1999extractor.bulk_generation import BulkGenerationError, inspect_generated_wav
from r1999extractor.moss_generation import (
    CaptureAudioOutput,
    MossGenerationProvider,
    is_spoken_item,
)


class FakeRegistry:
    def resolve(self, character):
        return object() if character == "Ready" else None


class FakeBackend:
    model_name = "moss-test"
    sample_rate = 24000
    _mlx = None

    def __init__(self, audio_output):
        self.audio_output = audio_output

    def prepare(self, character, text):
        return character, text

    def play(self, prepared):
        del prepared
        with self.audio_output.OutputStream() as stream:
            stream.write(np.full(12000, 0.25, dtype=np.float32))
        return True


class MossGenerationProviderTest(unittest.TestCase):
    def test_writes_provider_audio_as_valid_pcm16_mono_wav(self):
        output = CaptureAudioOutput()
        provider = MossGenerationProvider(FakeBackend(output), FakeRegistry(), output)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            provider.generate(
                {"voice_character": "Ready", "text": "A generated line."},
                path,
                seed=7,
            )
            quality = inspect_generated_wav(path)

        self.assertEqual(quality.sample_rate, 24000)
        self.assertEqual(quality.duration_seconds, 0.5)
        self.assertEqual(output.streams, [])

    def test_rejects_unknown_character_before_generation(self):
        output = CaptureAudioOutput()
        provider = MossGenerationProvider(FakeBackend(output), FakeRegistry(), output)

        with self.assertRaisesRegex(BulkGenerationError, "No MOSS voice reference"):
            provider.generate(
                {"voice_character": "Missing", "text": "A line."},
                Path("unused.wav"),
                seed=0,
            )

    def test_skips_pure_sound_effect_but_keeps_spoken_stage_direction(self):
        self.assertFalse(is_spoken_item({"text": "*chirp-chirp*"}))
        self.assertFalse(is_spoken_item({"text": '"*bang*"'}))
        self.assertTrue(is_spoken_item({"text": "*cough* Who is there?"}))


if __name__ == "__main__":
    unittest.main()
