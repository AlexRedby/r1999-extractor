import unittest

from r1999extractor.reverse1999_aliases import (
    aliases_for_character,
    canonical_voice_name,
    voice_character_for_speaker,
)


class Reverse1999AliasesTest(unittest.TestCase):
    def test_lorentz_butterfly_uses_marguerite_voice_identity(self):
        self.assertEqual(canonical_voice_name("Lorentz Butterfly"), "Marguerite")
        self.assertEqual(aliases_for_character("Marguerite"), ("Lorentz Butterfly",))

    def test_exact_unknown_display_speaker_uses_narrator_voice(self):
        self.assertEqual(voice_character_for_speaker("???"), "Narrator")
        self.assertEqual(voice_character_for_speaker("Unknown Researcher"), "Unknown Researcher")
        self.assertEqual(voice_character_for_speaker(" ??? "), " ??? ")


if __name__ == "__main__":
    unittest.main()
