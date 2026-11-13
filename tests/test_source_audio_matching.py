import unittest

from r1999extractor.source_audio_matching import classify_source_audio_text, normalize_semantic_text


class SourceAudioMatchingTest(unittest.TestCase):
    def test_accepts_bounded_asr_errors_and_unambiguous_contractions(self):
        cases = (
            (
                "Madam Hofmann was calm till the end. Why? Was she not afraid?",
                "Mother Mothman was calm till the end. Why? Was she not afraid?",
            ),
            (
                "No. I just read her fear through her trembling fingers ...",
                "No. I just fed her fear to her trembling fingers.",
            ),
            ("I would've gone.", "I would have gone."),
            ("I shouldn't've gone.", "I should not have gone."),
            ("I can't.", "I can not."),
            ("I can't.", "I cannot."),
            ("I'm sure they won't.", "I am sure they will not."),
            ("You’re here; we'll go.", "You are here; we will go."),
            ("Let's go.", "Let us go."),
        )
        for expected, observed in cases:
            with self.subTest(expected=expected, observed=observed):
                self.assertEqual(
                    classify_source_audio_text(expected, observed),
                    ("full", "matched-normalized-asr-transcript"),
                )

    def test_preserves_legacy_hash_normalization_and_locale_boundaries(self):
        self.assertEqual(normalize_semantic_text("Would’ve…"), "would've")
        self.assertEqual(
            classify_source_audio_text("I would've.", "I would have.", locale="en-US")[0], "full"
        )
        self.assertEqual(
            classify_source_audio_text("I would've.", "I would have.", locale="de")[0], "partial"
        )
        self.assertEqual(classify_source_audio_text("Hallo!", "hallo", locale="de")[0], "full")

    def test_rejects_short_different_missing_and_hallucinated_speech(self):
        expected = "We can see the quiet river beside the old house tonight"
        cases = (
            ("Doctor!", "Stop it!"),
            ("Stop it.", "Stop."),
            (expected, "We can see the quiet river beside the old house"),
            (expected, "see the quiet river beside the old house tonight"),
            (expected, expected + " until dawn"),
            (
                "Madam Hofmann was calm till the end. Why? Was she not afraid?",
                "Madam Hofmann was calm till the end.",
            ),
            (expected, "Thank you for watching and please subscribe to our channel today"),
            (expected, "They may see the noisy river beside the old house tonight"),
            ("", ""),
        )
        for displayed, observed in cases:
            with self.subTest(displayed=displayed, observed=observed):
                self.assertEqual(
                    classify_source_audio_text(displayed, observed),
                    ("partial", "asr-transcript-mismatch"),
                )

    def test_rejects_changed_numbers_negation_and_ambiguous_contractions(self):
        cases = (
            ("We will see the old bridge after 12 days of walking", "12", "13"),
            ("We will see the old bridge after two days of walking", "two", "ten"),
            ("We will not see the old bridge after days of walking", "not", "now"),
            ("No we will see the old bridge after days of walking", "No", "So"),
            ("We never see the old bridge after many days of walking", "never", "often"),
            ("He's very sure we will see the old bridge after walking", "He's", "He"),
            ("He'd surely see the old bridge after many days of walking", "He'd", "He"),
        )
        for expected, original, replacement in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_source_audio_text(expected, expected.replace(original, replacement))[
                        0
                    ],
                    "partial",
                )
        self.assertEqual(classify_source_audio_text("He's here.", "He is here.")[0], "partial")
        self.assertEqual(classify_source_audio_text("He'd gone.", "He had gone.")[0], "partial")


if __name__ == "__main__":
    unittest.main()
