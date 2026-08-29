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
