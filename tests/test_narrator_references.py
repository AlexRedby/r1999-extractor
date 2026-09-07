import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor.narrator_references import prepare_narrator_references
from r1999extractor.reverse1999_index import build_bank_index
from r1999extractor.reverse1999_voice_import import ImportedReference
from r1999extractor.story_audio import wwise_event_id
from r1999extractor.story_voice_candidates import StoryVoiceCandidateError
from tests.test_playable_voice import synthetic_bank
from tests.test_story_voice_candidates import story_line, write_story


class NarratorReferencesTest(unittest.TestCase):
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
                self.assertTrue(manifest.parent.name.startswith("narrator-spoken-v1-"))
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
