import unittest

from r1999extractor.reverse1999_aliases import (
    aliases_for_character,
    canonical_voice_name,
)


class Reverse1999AliasesTest(unittest.TestCase):
    def test_lorentz_butterfly_uses_marguerite_voice_identity(self):
        self.assertEqual(canonical_voice_name("Lorentz Butterfly"), "Marguerite")
        self.assertEqual(aliases_for_character("Marguerite"), ("Lorentz Butterfly",))


if __name__ == "__main__":
    unittest.main()
