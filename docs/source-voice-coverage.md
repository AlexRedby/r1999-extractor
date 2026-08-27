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

## Playable-character voice extraction

`r1999-playable-voice-index` replaces Wiki-only discovery for playable
characters with a game-owned, provenance-bound index. It decrypts the installed
English configs, reads `json_character_voice` for official titles and text,
resolves each voice ID through `json_story_audio_role`, and binds the route to
the installed Wwise bank index. For each installed route it reads the bank once
and records SHA-256 for both the exact bank bytes and every embedded or external
media item. Before binding, it validates every bank entry and nested event/media
route in the index. A bank path must remain below the audio root and its basename
must equal the claimed filename; external media must remain below the resolved
`Media` root. These checks prevent a symlink or incoherent index entry from
binding a trusted label to different bytes.

Paper Heron demonstrates the complete path. The installed patch 3.7 data binds:

- character ID `3141` and 51 English `json_character_voice` rows;
- 39 distinct Wwise events and 41 distinct media items across
  `hero3141_mainvoc.bnk` and `hero3141_vo.bnk`;
- all 51 config rows with `installed` source status;
- 13 `Spring Comes Slowly` character-story records to eight installed media
  items in `hero3141_mainstory.bnk`.

Three of those eight character-story media items are reused by records with
different text hashes. The index reports this as
`character_story_media_text_conflict_count: 3`; those routes remain useful as
speaker-identity evidence but must not be treated as a one-to-one transcript or
selected automatically as a reference clip. The same check is applied to
official playable rows independently by voice ID, Wwise event, and media item.
Paper Heron's official set has zero conflicts in all three bindings.

The previous gap was in extraction, not in the installed playable banks:
`examples/provision_reverse1999_voices.py` downloaded a fixed set of archived
Wiki files, while the story extractor did not parse `json_character_voice`.
Consequently a new playable character such as Paper Heron could have locally
installed voice banks and official text yet still be absent from the Wiki-built
manifest.

The patch 3.4 story is a separate source. Its rows correctly name
`activityvoc_hero3141_3_4_bulaochun_part01`, `part02`, and `part03`, but those
banks are not present in the installed patch 3.7 audio directory. The playable
`mainvoc`, combat `vo`, and reusable `mainstory` banks do not authorize relabeling
those missing activity events as installed story audio.

To build and inspect the exact bindings:

```bash
uv run r1999-playable-voice-index "Paper Heron" \
  --output /path/to/paper-heron-voice-index.json
```

Without `--story-index`, the command chooses the newest local
`story-index*.jsonl`, which makes the default follow the current regenerated
artifact rather than the original fixed filename. An explicit path remains the
reproducible override. Both current `source_audio_id` and legacy
`source_voice_id` story records are accepted; a record with neither field, a
route drift, or a missing artifact fails with a regeneration command instead of
a traceback.

For reference preparation, choose exact `voice_id` rows from that index. The
traditional Wiki trio maps locally to the official `First Encounter`,
`Chitchat I`, and `Chitchat II` rows. Pass their recorded bank and explicit
media IDs to `r1999-voice-import`, then score every resulting WAV with
`r1999-audio-score`. The importer records bank, media ID, source SHA-256, and
reference SHA-256 in the local v2 manifest. Do not use an unreviewed
character-story route whose media is bound to multiple text hashes.

The exact Paper Heron Wiki-equivalent reconciliation is:

| Official title | Voice ID | Media ID | Source SHA-256 | Technical score |
| --- | --- | --- | --- | --- |
| First Encounter | `1314101` | `555568095` | `06619a7a988dbc94dd25076246a3d65fb32c3527ffbf8cd651d9d234c7f6d686` | 80, `too-long` |
| Chitchat I | `1314116` | `354191196` | `ff74e74071734a622c6a779ab16843605489fff54c50a757c683365d5ff8033c` | 100, no technical flags |
| Chitchat II | `1314117` | `856018807` | `f4853e4306cab0d2fcd66f220a07081114bae2801ede6a2fea8ef53bd7e16711` | 80, `too-long` |

All three imported source hashes match the playable-voice index. The two long
clips require deliberate trimming or a different exact playable line before
they are used as compact synthesis references. Technical scoring does not
replace listening for music/SFX, multiple speakers, or speaker identity.

## Exact unknown-speaker policy

Every extracted story line whose display speaker is exactly `???` uses
`Narrator` as its `voice_character`. This is an explicit authoring rule, not an
inference from nearby dialogue. The original display speaker, source-audio
status, media IDs, bank, event, and source voice ID remain unchanged.

The rule applies equally to Unity story, config-only hero story, and structured
config sources. It does not affect a named unknown role, a missing speaker, or
any other label. In the 2026-08-17 installed index it routes 3,012 exact `???`
records: 2,085 with installed source audio, 459 with configured-but-unavailable
audio, and 468 with no source audio. Within the patch 3.7 coverage scope, both
`???` lines are therefore covered by the configured narrator voice, reducing
the uncovered set from 53 lines / 16 labels to 51 lines / 15 labels.

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

The 2026-08-17 exhaustive installed-data recheck found no further safe local
assignment:

- Intern I/II/III, Lab Assistant I/II, Armed Mercenary I/II, and
  Meteorological Observer I/II have no portrait, voice ID, event, or bank on any
  of their scoped records.
- `A Youthful Chirp`, `"Bird"`, and `Rat` have distinct portraits but no other
  record for those portraits supplies a voice ID or bank.
- Bechan and Glyndŵr have same-name/same-portrait records elsewhere, but all
  configured routes point to absent banks (`prologuechapter_13_part04` and
  `activitystory_beiai3_7_xiaoruiannong_sfx`).
- The uncovered Manus Believer portrait `400101.png` is also used by multiple
  incompatible Manus labels and banks; the generic display name is not an
  exact identity anchor.

Therefore the locally executable coverage result remains 51 lines / 15 voice
labels. This is an evidence boundary, not permission to synthesize those named
characters with Narrator or to borrow a merely similar generic role.

## Character Story reference recovery candidates

The current `The You That's Meant To Be` pregeneration workspace is a separate
scope from the 51-line patch coverage result above. Its read-only 2026-08-18
preflight has 237 lines blocked by nine missing manifest roles. A full
story-index audit proves that six of those roles already have installed
same-speaker dialogue elsewhere in the same Character Story:

| Role | Blocked lines | Installed source bank evidence |
| --- | ---: | --- |
| Aderyn | 113 | `activityvoc_story_hero3146_beiai.bnk` and `activityvoc_story_npcnoname326_beiai.bnk` |
| Dobharchú | 50 | `activityvoc_story_npcnoname323_beiai.bnk` |
| Mrs. Owen | 34 | `activityvoc_story_npcnoname322_beiai.bnk` |
| Hotelier | 12 | `activityvoc_story_npcnoname327_beiai.bnk` |
| Poacher | 4 | `activityvoc_story_npcnoname325_beiai.bnk` |
| Aderyn's Father | 1 | `activityvoc_story_npcnoname324_beiai.bnk` |

These candidates can potentially cover 214 of the 237 blocked lines, but they
are not yet manifest assignments. Each selected clip still needs exact
bank/media checksum binding, technical scoring and listening. Aderyn has both
`hero3146` and `npcnoname326` routes across distinct portrait groups; preserve
those groups until auditioning establishes whether one reference set can
truthfully represent every variant.

`r1999-story-voice-candidates` implements the safe preparation boundary for
this recovery. It selects only exact requested `voice_character` records whose
story source status is `available`, validates line/text identity, reads each
indexed bank once, verifies the fresh event route against `source_media_ids`,
and decodes the exact embedded media bytes from that snapshot. Candidate WAVs
are grouped by character, portrait and bank so Aderyn variants cannot be
silently collapsed. The report retains every source line, bank/media/reference
SHA-256, transcript conflict, technical metric and an explicit manual-content
review requirement. It refuses to replace an existing candidate directory and
does not write `manifest.json`.

Run the six-role preparation with:

```bash
uv run r1999-story-voice-candidates \
  --role Aderyn --role Dobharchú --role "Mrs. Owen" \
  --role Hotelier --role Poacher --role "Aderyn's Father" \
  --output /path/to/character-story-reference-candidates
```

The real 2026-08-18 run from clean extractor commit `16ca725` produced 53
candidate WAVs across 19 exact portrait/bank groups and 57 source records. The
portable report SHA-256 is
`8d91db80f32abecdeee7582250573ae103eaf6572b48b18cd815938fd3e2a9c1`;
the path-plus-file-digest inventory SHA-256 is
`ee5e4e1c27b0e1a6bb19f3e07979b05324e1e429ac12dad017d7619fb5b79258`
for 54 files. All 53 reference paths and hashes revalidated. Twelve candidates
pass the objective gates and two expose reused-media transcript conflicts.

The result is deliberately not manifest-ready without listening. Aderyn spans
12 portrait/bank groups, only six of which contain an objectively passing
candidate. Hotelier has five exact clips but none reaches the minimum duration.
This is the expected boundary for a one-off role in a mostly unvoiced Character
Story. Do not search other game versions, public recordings, reused portraits
or generic NPC banks; a five-clip same-bank composite is a VNTTS quality
experiment, not new extractor identity evidence.
One quoted Mrs. Owen record uses portrait `637913.png` and routes through
`activityvoc_story_npcnoname323_beiai.bnk`, so it must not be merged with the
ordinary `637901.png` / `npcnoname322` group without content review. The
audition set lives in the ignored voice-pack data directory; an earlier report
with non-portable staging paths is preserved separately as superseded evidence.

The first user listening pass selected Aderyn's Father media `209566863`,
Dobharchú media `951691760` and Poacher media `289048377`. Exact re-import from
the original banks reproduced the audition report's source and normalized WAV
hashes. The shorter Dobharchú media `875779076` remains a clean reserve. Those
three manifest entries cover 55 lines in the current VNTTS Character Story
queue. Aderyn media `369040295` and `172299031` are rejected because they
contain only crying. Aderyn is Rhiannon in childhood, but source identity and
life-stage remain separate from the adult synthesis voice. The reviewer
accepted media `477089679` from portrait `533706` as the child-voice anchor
(normalized WAV SHA-256
`49a0a42bc2cbac573ab0a0518e54edfb8c59709f76feb64f5cc41e7fd99e42b8`).
That decision applies to the exact portrait/bank group only; it does not merge
the remaining `hero3146` and `npcnoname326` age/portrait variants or authorize
the adult Rhiannon voice for them. Mrs. Owen media `599773947` is not approved:
the voice sounded usable, but its speech was not intelligible enough for a
cloning reference.

The later expanded checksum-bound review completed all seven selected cluster
cards. Six were accepted: both Dobharchú groups, Poacher, Aderyn's Father,
adult Aderyn/Rhiannon media `792349907`, and the already scoped child
Aderyn/Rhiannon media `477089679`. This did not merge the rejected crying clips
or unrelated portrait identities. A complete exact-bank scan then recovered
the stronger 3.172-second Mrs. Owen medium `562400954`; the user accepted its
speaker identity and the separate generated-quality card. Its decoded WAV
SHA-256 is
`82e3125fbc195951006817ccd13d507b40c4d2311c2f17ebc7a37f2505e7e22b`.
VNTTS published the resulting immutable binding for 34 exact Mrs. Owen queue
IDs without rewriting the earlier `needs_sample` decision.

Hotelier's complete exact bank contains exactly five short clips totaling
5.009 seconds, none of which passes the single-reference duration gate. A
checksum-ledgered same-bank composite was evaluated and did not establish an
acceptable cloning reference. Because Hotelier is a one-off role in a mostly
unvoiced Character Story, this is the completed extractor evidence boundary:
do not search other versions, public recordings, reused portraits, or generic
NPC banks. VNTTS uses an explicit Hotelier-only Narrator fallback instead of
inventing character identity evidence.

For the completed Aderyn, Mrs. Owen and Hotelier audition, a read-only scan of
all 7,018 installed iOS bundles recovered 11 of the 12 exact portrait sprites:
all nine Aderyn identities (`314601`, `314617`, `314619`, `314622`, `314623`,
`314625`, `533704`, `533705`, `533706`) and both Mrs. Owen identities (`637901`,
`637913`). Hotelier `505401` is absent from the installed Sprite inventory and
must display the missing-asset placeholder. The review UI accepts an explicit
portrait directory, renders the selected exact PNG, and rechecks its snapshot
before persisting a decision; portrait pixels remain supporting identity
evidence and never replace the report's checksum-bound candidate key.
The 11 recovered PNGs were copied without replacement to
`reverse1999/story-voice-portraits/character-story-20260818` in extractor
app-data. Their sorted `filename + NUL + file SHA-256 + newline` inventory
SHA-256 is
`d1e35cd761f7f1d9060a88bae09e9f663de7ecf4766530e070a90dba8094495a`.

No exact installed same-speaker route was found for `Poacher I` (13 blocked
lines) or `Poacher II` (nine). Do not merge either numbered role with the
separate `Poacher` identity merely because that role has one installed clip.
Glyndŵr's one blocked line has only configured-unavailable routes in the
current installation. Those 23 lines remain candidates for an explicit
authoring fallback after this source audit, not for an inferred game voice.

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
