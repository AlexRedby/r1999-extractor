from r1999extractor.reverse1999_catalog import normalize_name

voice_aliases = {
    "Brimley": ("Slouch Hat",),
    "Marguerite": ("Lorentz Butterfly",),
}

# These overrides are deliberately keyed by the full extracted line ID. Generic
# labels such as "???" are shared by unrelated speakers and must never become
# global aliases. The surrounding installed story rows provide an explicit
# identity reveal for each entry recorded here.
verified_line_voice_overrides = {
    "reverse1999:hero-story-plot:315407063": (
        "???",
        "9cfc6588569f6d8828c59d5c6a5d806a0306295572dca456626d59a4c3e85d7b",
        "Marguerite",
    ),
}


def aliases_for_character(character):
    normalized = normalize_name(character)
    for canonical_name, aliases in voice_aliases.items():
        if normalize_name(canonical_name) == normalized:
            return aliases
    return ()


def canonical_voice_name(value):
    normalized = normalize_name(value)
    for canonical_name, aliases in voice_aliases.items():
        known_names = (canonical_name, *aliases)
        if any(normalize_name(name) == normalized for name in known_names):
            return canonical_name
    return None


def voice_character_for_line(line_id, speaker, text_sha256=None):
    override = verified_line_voice_overrides.get(line_id)
    if override is not None:
        expected_speaker, expected_text_sha256, character = override
        if speaker == expected_speaker and text_sha256 == expected_text_sha256:
            return character
    return canonical_voice_name(speaker) or speaker
