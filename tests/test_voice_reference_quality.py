import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from r1999extractor.voice_reference_quality import (
    VoiceReferenceQualityError,
    analyze_voice_reference,
    select_reference_set,
    trim_and_normalize_voice_reference,
    write_quality_report,
)


def write_wav(path, samples, sample_rate=16000):
    pcm = np.clip(samples * 32767, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(pcm.tobytes())


class VoiceReferenceQualityTest(unittest.TestCase):
    def test_scores_clean_voice_length_clip_without_technical_flags(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "clean.wav"
            sample_rate = 16000
            silence = np.zeros(round(sample_rate * 0.2), dtype=np.float32)
            time = np.arange(round(sample_rate * 2.0)) / sample_rate
            tone = (0.25 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
            write_wav(path, np.concatenate((silence, tone, silence)))

            result = analyze_voice_reference(path)

        self.assertEqual(result.duration_seconds, 2.4)
        self.assertEqual(result.technical_flags, ())
        self.assertEqual(result.quality_score, 100)

    def test_flags_short_quiet_silent_and_clipped_audio(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bad.wav"
            sample_rate = 16000
            samples = np.zeros(sample_rate, dtype=np.float32)
            samples[7000:7100] = 1.0
            write_wav(path, samples)

            result = analyze_voice_reference(path)

        self.assertIn("too-short", result.technical_flags)
        self.assertIn("excessive-silence", result.technical_flags)
        self.assertIn("clipping", result.technical_flags)
        self.assertLess(result.quality_score, 50)

    def test_rejects_empty_wav(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "empty.wav"
            write_wav(path, np.array([], dtype=np.float32))

            with self.assertRaisesRegex(VoiceReferenceQualityError, "no audio"):
                analyze_voice_reference(path)

    def test_trims_silence_and_normalizes_peak_atomically(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.wav"
            output = root / "normalized.wav"
            sample_rate = 16000
            silence = np.zeros(sample_rate, dtype=np.float32)
            time = np.arange(sample_rate * 2) / sample_rate
            tone = (0.1 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
            write_wav(source, np.concatenate((silence, tone, silence)))

            trim_and_normalize_voice_reference(source, output)
            result = analyze_voice_reference(output)

        self.assertLess(result.duration_seconds, 2.3)
        self.assertAlmostEqual(result.peak_dbfs, -3.0, delta=0.1)
        self.assertLess(result.silence_ratio, 0.1)

    def test_writes_atomic_review_report(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "clip.wav"
            output = root / "quality.json"
            time = np.arange(32000) / 16000
            write_wav(path, 0.25 * np.sin(2 * np.pi * 220 * time))
            metrics = analyze_voice_reference(path)

            write_quality_report([metrics], output)
            document = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(document["version"], 1)
        self.assertEqual(document["clips"][0]["quality_score"], 100)
        self.assertIn("never approve", document["review_note"])

    def test_selects_reviewed_reference_set(self):
        clips = [
            {
                "speaker_name": "Selone",
                "approved": True,
                "metrics": {"duration_seconds": seconds, "quality_score": 100},
            }
            for seconds in (5.0, 5.5, 6.0, 2.0)
        ]
        selected = select_reference_set(clips, "Selone")

        self.assertEqual(len(selected), 4)
        self.assertEqual(sum(item["metrics"]["duration_seconds"] for item in selected), 18.5)
        self.assertTrue(all(item["approved"] for item in selected))


if __name__ == "__main__":
    unittest.main()
