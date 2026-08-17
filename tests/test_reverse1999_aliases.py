import unittest

from r1999extractor.reverse1999_aliases import (
    aliases_for_character,
    canonical_voice_name,
    voice_character_for_line,
)


class Reverse1999AliasesTest(unittest.TestCase):
    def test_lorentz_butterfly_uses_marguerite_voice_identity(self):
        self.assertEqual(canonical_voice_name("Lorentz Butterfly"), "Marguerite")
        self.assertEqual(aliases_for_character("Marguerite"), ("Lorentz Butterfly",))

    def test_verified_reveal_only_overrides_the_exact_unknown_line(self):
        self.assertEqual(
            voice_character_for_line(
                "reverse1999:hero-story-plot:315407063",
                "???",
                "9cfc6588569f6d8828c59d5c6a5d806a0306295572dca456626d59a4c3e85d7b",
            ),
            "Marguerite",
        )
        self.assertEqual(
            voice_character_for_line(
                "reverse1999:101309:27",
                "???",
                "99267e02276fb116102ba9968ab262a06cbdb89586fa0867b3972a7c86f34eb2",
            ),
            "???",
        )
        self.assertEqual(
            voice_character_for_line(
                "reverse1999:hero-story-plot:315407063",
                "???",
                "0" * 64,
            ),
            "???",
        )


if __name__ == "__main__":
    unittest.main()
