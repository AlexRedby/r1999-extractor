"""Deterministic, bounded comparison of displayed speech and local ASR text."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

BOUNDED_SEMANTIC_EVIDENCE_METHOD = "local-asr-bounded-transcript-v1"
SOURCE_AUDIO_COMPARISON = "english-contractions-bounded-word-errors-v1"
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", flags=re.UNICODE)
_PROTECTED_WORDS = frozenset(
    "no not never neither nor none nobody nothing nowhere without cannot "
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
    "sixty seventy eighty ninety hundred thousand million billion trillion".split()
)
_NEGATIVE_CONTRACTIONS = {
    "can't": "can",
    "won't": "will",
    "shan't": "shall",
    **{
        word + "n't": word
        for word in (
            "are",
            "is",
            "was",
            "were",
            "do",
            "does",
            "did",
            "has",
            "have",
            "had",
            "could",
            "should",
            "would",
            "must",
            "might",
            "need",
            "dare",
        )
    },
}


def normalize_semantic_text(text: object) -> str:
    """Preserve the legacy word normalization used by evidence identity hashes."""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(
        token.casefold().replace("’", "'") for token in WORD_PATTERN.findall(normalized)
    )


def _expand_contraction(word: str) -> list[str]:
    if word == "cannot":
        return ["can", "not"]
    if word in _NEGATIVE_CONTRACTIONS:
        return [_NEGATIVE_CONTRACTIONS[word], "not"]
    if word == "i'm":
        return ["i", "am"]
    if word == "let's":
        return ["let", "us"]
    for suffix, expansion in (("'ve", "have"), ("'re", "are"), ("'ll", "will")):
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if stem in _NEGATIVE_CONTRACTIONS:
                return [_NEGATIVE_CONTRACTIONS[stem], "not", expansion]
            return [stem, expansion]
    return [word]


def _protected_word(word: str) -> bool:
    return (
        word in _PROTECTED_WORDS
        or any(character.isdigit() for character in word)
        or word.endswith(("'s", "'d", "n't"))
    )


def classify_source_audio_text(
    expected: object, observed: object, *, locale: str = "en"
) -> tuple[Literal["full", "partial"], str]:
    """Accept contractions and sparse substitutions, preserving missing speech."""
    expected_words = normalize_semantic_text(expected).split()
    observed_words = normalize_semantic_text(observed).split()
    english = locale.casefold().replace("_", "-").split("-")[0] == "en"
    if english:
        expected_words = [part for word in expected_words for part in _expand_contraction(word)]
        observed_words = [part for word in observed_words for part in _expand_contraction(word)]
    if expected_words and expected_words == observed_words:
        return "full", "matched-normalized-asr-transcript"
    if english and len(expected_words) >= 10 and len(expected_words) == len(observed_words):
        substitutions = [
            (left, right) for left, right in zip(expected_words, observed_words) if left != right
        ]
        # Equal word counts preserve short cues and truncated prefixes; the cap
        # bounds ASR tolerance rather than asserting semantic equivalence.
        if (
            len(substitutions) <= 2
            and len(substitutions) * 5 <= len(expected_words)
            and not any(_protected_word(word) for pair in substitutions for word in pair)
        ):
            return "full", "matched-normalized-asr-transcript"
    return "partial", "asr-transcript-mismatch"
