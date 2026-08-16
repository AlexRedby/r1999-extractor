# Patch 3.7 source voice coverage

Patch 3.7 coverage is measured against the source-only story index and the
configured v2 voice manifest. The scoped lines are speakable `story` and
`hero_story_plot` records in Chapter 13 (`101301` through `101341`) and
Silverwing Eagle's anecdote (`315401` through `315408`) whose extractor audio
status is `no_audio`.

A named line is covered only when its normalized `voice_character` matches a
manifest character or explicit alias with at least one reference. `Narrator`
is evaluated separately through the configured narrator character. A missing
named character is never assigned the narrator voice implicitly.

## Verified Paravyan references

The 2026-08-16 audit reduced uncovered content from 54 lines / 17 speakers to
53 lines / 16 speakers by adding an explicit Paravyan manifest entry. The
identity evidence is game-derived rather than inferred from a similar voice:

- the uncovered line is `reverse1999:101302:38` in Chapter 13, Episode 2;
- fifteen other Paravyan records in the same extracted story index have exact
  installed source-audio bindings;
- three records in that same episode bind Paravyan to
  `plotvoc_npc526301chapter13.bnk` media IDs `907578120`, `988924850`, and
  `750345238`;
- the installed bank index identifies that file as NPC `526301`, Chapter 13;
- all three decoded, trimmed, and normalized WAV references scored 100 with no
  technical flags in `r1999-audio-score`.

The imported source/reference checksum pairs are:

| Media ID | Source SHA-256 | Reference SHA-256 |
| --- | --- | --- |
| `907578120` | `bd9962e50c6e5cd4356c355349f3eb626bdbf0fe47cf7517cd178ac23eb931ad` | `948719b302c910c2f60a236b1d4700a4a0041bcd9ae77d7d3551fd93200af103` |
| `988924850` | `eaa8818c1aadc4ce609b95a49dbf06e7afc2c4afefe17e67c672668d57eaa49d` | `cd5fc7fd164f802685c3e03aace3b3aad55e3e5e3a5083bf7d42cbe5e5cbb2e5` |
| `750345238` | `1c2562f4c5992a6ea90b69af6d0af2f98745fc43de71089f111a77fe3eaa0dd9` | `2557e00ba74ac9d978f7c29a1cf86281a558a9992bb22bfb9220757ed6f6002e` |

The voice manifest and reference WAVs are local ignored source artifacts; Git
stores the reproducible evidence and procedure, not extracted game audio.

## Remaining uncovered labels

| Voice character | Lines | Available evidence |
| --- | ---: | --- |
| Intern II | 17 | Blank game voice IDs; no exact installed same-speaker audio |
| Lab Assistant I | 6 | Blank game voice IDs; no exact installed same-speaker audio |
| Armed Mercenary I | 4 | Blank game voice IDs; no exact installed same-speaker audio |
| Intern I | 4 | Blank game voice IDs; no exact installed same-speaker audio |
| Lab Assistant II | 4 | Blank game voice IDs; no exact installed same-speaker audio |
| A Youthful Chirp | 3 | Blank game voice IDs; no exact installed same-speaker audio |
| Meteorological Observer I | 3 | Blank game voice IDs; no exact installed same-speaker audio |
| `???` | 2 | Generic label is shared by many unrelated voices; not an identity |
| Armed Mercenary II | 2 | Blank game voice IDs; no exact installed same-speaker audio |
| Intern III | 2 | Blank game voice IDs; no exact installed same-speaker audio |
| `"Bird"` | 1 | Blank game voice ID; no exact installed same-speaker audio |
| Bechan | 1 | Other records name an unavailable bank; no installed reference |
| Glyndŵr | 1 | Other records name an unavailable bank; no installed reference |
| Manus Believer | 1 | Same display label occurs with incompatible NPC banks/portraits |
| Meteorological Observer II | 1 | Blank game voice ID; no exact installed same-speaker audio |
| Rat | 1 | Blank game voice ID; no exact installed same-speaker audio |

These labels remain deliberately unassigned. A future assignment requires a
game voice ID, an unambiguous portrait/NPC-to-bank mapping, or separately
reviewed local game dialogue proving the speaker identity. Reusing one generic
role for another or selecting a merely similar voice is not sufficient.

## Reproduction procedure

1. Load the current story-index JSONL and select the source kinds, chapter
   ranges, `no_audio` status, and speakable lines described above.
2. Load the configured v2 voice manifest and compare normalized characters and
   aliases; report missing named characters and line counts.
3. For a candidate, require exact game-derived speaker evidence from the story
   index, bank index, or NPC catalog before decoding audio.
4. Import only explicit media IDs with `r1999-voice-import` into the local voice
   pack. This records bank names, media IDs, source hashes, and reference hashes.
5. Run `r1999-audio-score` on every imported WAV and reject clips with technical
   flags. Confirm the recorded reference SHA-256 values against the actual files.
6. Re-run the coverage calculation. Update TODO counts only after the manifest
   round-trip and checksum checks succeed.
