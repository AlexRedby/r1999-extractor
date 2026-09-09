import hashlib
import io
import json
import unittest
import wave
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.narrator_references import (
    NarratorReferenceSession,
    list_narrator_references,
    prepare_narrator_references,
)
from r1999extractor.reverse1999_index import build_bank_index
from r1999extractor.reverse1999_voice_import import ImportedReference
from r1999extractor.story_audio import wwise_event_id
from r1999extractor.story_voice_candidates import StoryVoiceCandidateError, snapshot_bank
from tests.test_playable_voice import synthetic_bank
from tests.test_story_voice_candidates import story_line, write_story


class NarratorReferencesTest(unittest.TestCase):
    def test_prefetch_reference_decodes_full_external_wav_and_versions_cache(self):
        def wav_bytes(seconds):
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x10" * round(seconds * 24000))
            return output.getvalue()

        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            story, index, _lines = self.fixture(root)
            (root / "en/mianvoc_hero3032.bnk").write_bytes(
                synthetic_bank(
                    10, wav_bytes(0.077), wwise_event_id("play_hero_line"), stream_type=1
                )
            )
            build_bank_index(root / "en", output=index)
            full = root / "Media/10.wem"
            full.parent.mkdir()
            full.write_bytes(wav_bytes(3))
            calls = []

            def decoder_runner(args, **_kwargs):
                calls.append(args)
                Path(args[3]).write_bytes(Path(args[4]).read_bytes())
                return CompletedProcess(args, 0, "", "")

            session = NarratorReferenceSession(story, index, "Centurion", root / "candidates")
            with patch("r1999extractor.wwise.resolve_decoder", return_value="test-decoder"):
                manifest = session.prepare(decoder="test-decoder", runner=decoder_runner)
                self.assertEqual(
                    session.prepare(decoder="test-decoder", runner=decoder_runner), manifest
                )
                self.assertEqual(len(calls), 1)
                metadata = json.loads(manifest.read_text())["voices"][0]["vntts.narrator_reference"]
                self.assertEqual(metadata["source_kind"], "external")
                self.assertEqual(
                    metadata["source_sha256"], hashlib.sha256(full.read_bytes()).hexdigest()
                )
                self.assertEqual(metadata["decoded_duration_seconds"], 3)
                self.assertEqual(metadata["reference_duration_seconds"], 3)
                with wave.open(str(manifest.parent / "references/1.wav")) as wav:
                    self.assertEqual(wav.getnframes() / wav.getframerate(), 3)
                with patch("r1999extractor.narrator_references.REFERENCE_DECODE_VERSION", 3):
                    changed = session.prepare(decoder="test-decoder", runner=decoder_runner)
                self.assertNotEqual(changed, manifest)
                self.assertEqual(len(calls), 2)
                full.unlink()
                with self.assertRaisesRegex(StoryVoiceCandidateError, "no longer available"):
                    session.prepare(decoder="test-decoder", runner=decoder_runner)

    def test_session_reuses_catalog_bank_and_verified_disk_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            story, index, _lines = self.fixture(root)

            def decode_one(payload, output, media_id, _decoder, *, bank):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"decoded")
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(payload).hexdigest(),
                    hashlib.sha256(b"decoded").hexdigest(),
                    bank,
                )

            with (
                patch(
                    "r1999extractor.narrator_references.list_narrator_references",
                    wraps=list_narrator_references,
                ) as listing,
                patch(
                    "r1999extractor.narrator_references.snapshot_bank", wraps=snapshot_bank
                ) as snapshot,
                patch(
                    "r1999extractor.narrator_references.decode_reference_data",
                    side_effect=decode_one,
                ) as decode,
                patch(
                    "r1999extractor.narrator_references.StoryAudioResolver.read_single_available_media"
                ) as reread,
            ):
                session = NarratorReferenceSession(story, index, "Centurion", root / "candidates")
                manifest = session.prepare(decoder="decoder")
                session.prepare(decoder="decoder")
                listing.assert_called_once()
                snapshot.assert_called_once()
                decode.assert_called_once()
                reread.assert_not_called()
                # Reopening revalidates source data but reuses verified decoded audio.
                reopened = NarratorReferenceSession(story, index, "Centurion", root / "candidates")
                self.assertEqual(reopened.prepare(decoder="decoder"), manifest)
                decode.assert_called_once()
                wav = manifest.parent / "references/1.wav"
                wav.write_bytes(b"corrupt")
                reopened.prepare(decoder="decoder")
                self.assertEqual(decode.call_count, 2)
                self.assertEqual(wav.read_bytes(), b"decoded")
                manifest.write_text('{"version":2,"voices":[{"vntts.narrator_reference":null}]}')
                reopened.prepare(decoder="decoder")
                self.assertEqual(decode.call_count, 3)
                bank = root / "en/mianvoc_hero3032.bnk"
                bank.write_bytes(bank.read_bytes() + b"changed")
                with self.assertRaisesRegex(StoryVoiceCandidateError, "bank changed"):
                    session.prepare(decoder="decoder")
                with story.open("a") as stream:
                    stream.write("\n")
                with self.assertRaisesRegex(StoryVoiceCandidateError, "catalog changed"):
                    reopened.prepare(decoder="decoder")

    def test_lists_all_speech_without_decoding_and_prepares_only_selected_line(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            story, index, lines = self.fixture(root)
            for number in range(4, 8):
                bank = f"mianvoc_hero3032_{number}.bnk"
                (root / "en" / bank).write_bytes(
                    synthetic_bank(
                        10, f"speech {number}".encode(), wwise_event_id("play_hero_line")
                    )
                )
                line = story_line(
                    number, "This is another spoken reference with enough words for narration."
                )
                line.update(
                    line_id=f"playable-voice:3032:{number}:0",
                    speaker="Centurion",
                    voice_character="Centurion",
                    source_bank=bank,
                )
                lines.append(line)
            write_story(story, lines)
            build_bank_index(root / "en", output=index)
            with (
                patch("r1999extractor.narrator_references.resolve_decoder") as decoder,
                patch("r1999extractor.narrator_references.snapshot_bank") as snapshot,
                patch("r1999extractor.narrator_references.decode_reference_data") as decode,
            ):
                candidates = list_narrator_references(story, "Centurion")
                self.assertEqual(len(candidates), 5)
                self.assertEqual(candidates[0].line_id, lines[0]["line_id"])
                decoder.assert_not_called()
                snapshot.assert_not_called()
                decode.assert_not_called()
                with self.assertRaisesRegex(StoryVoiceCandidateError, "no longer available"):
                    prepare_narrator_references(story, index, "Centurion", root, line_id="missing")
                decode.assert_not_called()

            def decode_one(payload, output, media_id, _decoder, *, bank):
                self.assertEqual(payload, b"speech 7")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"decoded")
                return ImportedReference(
                    output,
                    media_id,
                    hashlib.sha256(payload).hexdigest(),
                    hashlib.sha256(b"decoded").hexdigest(),
                    bank,
                )

            with (
                patch("r1999extractor.narrator_references.resolve_decoder"),
                patch(
                    "r1999extractor.narrator_references.decode_reference_data",
                    side_effect=decode_one,
                ) as decode,
            ):
                manifest = prepare_narrator_references(
                    story, index, "Centurion", root, line_id="playable-voice:3032:7:0"
                )
                decode.assert_called_once()
                voices = json.loads(manifest.read_text())["voices"]
                self.assertEqual(len(voices), 1)
                self.assertEqual(
                    voices[0]["vntts.narrator_reference"]["line_id"], "playable-voice:3032:7:0"
                )

    def fixture(self, root, *, external=False):
        audio = root / "en"
        audio.mkdir()
        lines = []
        for sequence, bank, text, payload in (
            (
                1,
                "mianvoc_hero3032",
                "Life is a gamble. When are you going to make a bet?",
                b"speech",
            ),
            (
                2,
                "hero3032_skill",
                "This lengthy combat shout is not a calm narrator reference.",
                b"shout",
            ),
            (3, "hero3032_mainstory", "Ha ha ha ha ha ha!", b"laugh" * 10000),
        ):
            (audio / f"{bank}.bnk").write_bytes(
                synthetic_bank(
                    8 if external else 10,
                    payload,
                    wwise_event_id("play_hero_line"),
                    routed_media_id=10,
                )
            )
            line = story_line(sequence, text)
            line.update(
                line_id=f"playable-voice:3032:{sequence}:0" if sequence < 3 else "story:3",
                speaker="Centurion",
                voice_character="Centurion",
                source_bank=f"{bank}.bnk",
                collection_title="First Encounter" if sequence == 1 else "Combat",
            )
            lines.append(line)
        if external:
            (root / "Media").mkdir()
            (root / "Media/10.wem").write_bytes(b"speech")
        _, index = build_bank_index(audio, output=root / "banks.json")
        story = write_story(root / "narrator-index.jsonl", lines)
        (root / "narrator-banks.json").write_text('{"Centurion":"hero3032_mainstory.bnk"}')
        return story, index, lines

    def test_playable_speech_beats_larger_story_effects_and_combat_with_transcript(self):
        for external in (False, True):
            with self.subTest(external=external), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                story, index, _lines = self.fixture(root, external=external)

                def decode(payload, output, media_id, _decoder, *, bank):
                    self.assertEqual(payload, b"speech")
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"decoded speech")
                    return ImportedReference(
                        output,
                        media_id,
                        hashlib.sha256(payload).hexdigest(),
                        hashlib.sha256(output.read_bytes()).hexdigest(),
                        bank,
                    )

                with (
                    patch(
                        "r1999extractor.narrator_references.resolve_decoder", return_value="decoder"
                    ),
                    patch(
                        "r1999extractor.narrator_references.decode_reference_data",
                        side_effect=decode,
                    ) as render,
                ):
                    manifest = prepare_narrator_references(
                        story, index, "Centurion", root / "candidates"
                    )
                render.assert_called_once()
                self.assertTrue(manifest.parent.name.startswith("narrator-spoken-v2-"))
                voices = json.loads(manifest.read_text())["voices"]
                self.assertEqual(len(voices), 1)
                evidence = voices[0]["vntts.narrator_reference"]
                self.assertEqual(evidence["bank"], "mianvoc_hero3032.bnk")
                self.assertEqual(evidence["title"], "First Encounter")
                self.assertIn("Life is a gamble", evidence["text"])

    def test_missing_playable_speech_and_conflicting_transcripts_fail_without_decoding(self):
        for case in ("only story", "only combat", "conflicting text", "changed route"):
            with self.subTest(case=case), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                story, index, lines = self.fixture(root)
                if case == "only story":
                    lines = lines[2:]
                elif case == "only combat":
                    lines = lines[1:]
                elif case == "conflicting text":
                    duplicate = dict(
                        lines[0], line_id="playable-voice:3032:4:0", text="Different words"
                    )
                    duplicate["text_sha256"] = hashlib.sha256(
                        duplicate["text"].encode()
                    ).hexdigest()
                    lines.append(duplicate)
                else:
                    lines[0]["source_media_ids"] = [11]
                write_story(story, lines)
                with patch("r1999extractor.narrator_references.decode_reference_data") as decode:
                    with self.assertRaises(StoryVoiceCandidateError):
                        prepare_narrator_references(story, index, "Centurion", root / "candidates")
                    decode.assert_not_called()
