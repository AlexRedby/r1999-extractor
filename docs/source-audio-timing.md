# Source-audio timing and completeness

`source_audio_status=available` means the configured Wwise event resolves to
installed media. It does not mean that media reads the displayed story text.
Reverse: 1999 Character Stories commonly attach a short acted cue or paraphrase
to a much longer dialogue box.

Run the timing publisher against an existing story index and a fresh installed
bank index:

```console
r1999-source-audio-duration \
  --story-index /path/to/story-index.jsonl \
  --bank-index /path/to/english-bank-index.json \
  --chapter 314601 \
  --output /path/to/story-index-with-source-timing.jsonl
```

The command never overwrites its input. It rejects a stale bank index and only
measures events with one exact installed media identity. For each successful
measurement it records the media ID, SHA-256, decoder version, sample rate,
sample count and raw duration. Multi-media, missing, changed and undecodable
routes stay untimed.

Timing is not semantic completeness. The conservative built-in classifier may
prove `partial` when the WEM is physically too short to contain the displayed
words, but it never infers `full` from plausible duration. Such lines remain
`unknown` until checksum-bound transcript or human evidence proves otherwise.
The chapter `314601` control measured all 15 installed source voices at
0.446-1.422 seconds; ten are duration-proven partial and five remain unknown.
Local Whisper controls also showed short paraphrases such as `Please allow me.`,
`That'll be an extra charge.`, and `It's all too much!` for longer displayed
lines. Consumers must therefore treat `partial` as a cue preceding the full
TTS reading and must not auto-advance `unknown` source audio merely because its
duration is known.

## Semantic evidence for duration-plausible cues

Unknown timed cues can be resolved offline with the optional local authoring
command:

```console
r1999-source-audio-semantics \
  --story-index /path/to/timed-story-index.jsonl \
  --bank-index /path/to/english-bank-index.json \
  --chapter 314601 \
  --model /path/to/pinned-whisper-snapshot \
  --evidence-output /path/to/source-audio-semantic-evidence.json \
  --story-output /path/to/semantic-story-index.jsonl
```

The command never overwrites either destination. It transcribes only
`source_audio_completeness=unknown` records that still resolve to the exact
timed WEM. Reusable evidence identity is locale + exact WEM SHA-256 + normalized
displayed-text SHA-256; source line IDs are diagnostic aliases and never grant
authority. The evidence also binds the complete local model snapshot SHA-256,
the observed transcript and its normalized hash. A normalized exact transcript
marks the cue `full`; every mismatch is conservatively `partial`, including a
likely ASR punctuation, hesitation or wording error. Changed media, displayed
text, evidence, story metadata or model binding fails closed.

The 2026-08-30 chapter `314601` run used the local Whisper `tiny.en` snapshot
`87c7102498dcde7456f24cfd30239ca606ed9063`, whose directory SHA-256 was
`d69d7c69a342b4cf4274fe974559249fdb240d14813cd7d03cb9094955a7240b`.
Two independent runs produced evidence ID
`a02c1f40a26c64d8c716577df84acd811c34ad81592d80d1e672cf7a1f23bad8`:

| Sequence | Displayed text | Observed transcript | Verdict |
| --- | --- | --- | --- |
| 9 | `Stop it.` | `Stop it!` | `full` |
| 22 | `They're not parrots.` | `But not parrots!` | `partial` |
| 33 | `R-Right.` | `Right.` | `partial` |
| 86 | `Where are you off to, then?` | `Where are you off to that?` | `partial` |
| 100 | `I can't believe she'd ... *sigh*` | `How could she do this?` | `partial` |

The evidence JSON SHA-256 is
`392dac838b43ab2c400e2b23e8adc56abd9ed9dc91e4b8dc27437e67cf585145`;
the immutable semantic successor story-index SHA-256 is
`86beb8102454dbeb69ec08e4aa18ab434f045c9d22391064451abfdab725b0d6`.
This authoring evidence is safe to ship for runtime preflight, but Whisper is
not a playback dependency and no player is asked to classify cues.
