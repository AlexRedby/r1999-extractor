# Live sequence plans

`r1999-live-sequence` exports ordered raw Unity story steps for VNTTS
sequence-first live reading. The plan is bound to the SHA-256 of both the exact
story-index bytes and the installed source bundle bytes.

The producer reads every raw step and sorts the existing records by their
declared sequence. Unity serialization order is not authoritative: some assets
store almost the entire chapter in reverse. The producer never invents records
or key presses for numeric sequence gaps:

- a line retained by the story index becomes `speech` and binds its exact
  `line_id`;
- punctuation-only ellipsis text such as `...` becomes an automatic `silent`
  event and is not sent to TTS;
- an empty-text step becomes a passive `transition`, because background,
  camera, audio, effect and title-card timing belongs to the game;
- visible text omitted from the selected story index becomes a manual `wait`,
  so VNTTS cannot speak or skip an unbound producer guess;
- only an existing `sequence + 1` record is a linear successor. A numeric gap
  becomes a manual synchronization boundary, never a jump to the next larger
  number;
- explicit choice records at raw step index 10 replace the linear successor
  with their target sequence IDs. Duplicate targets are collapsed for runtime
  control, target `0` exits the sequence, and one stable entry anchor is exposed
  for every disconnected branch segment or loop;
- the last event is terminal unless it contains unbound visible text that still
  requires manual handling.

The exporter rejects malformed or duplicate raw steps and any indexed line that
cannot be rebound to its raw source event. It filters the bundle to chapters
that actually occur in the selected story index. Source and story file identity
are checked again immediately before publication. Final publication delegates
to the shared `vntts-artifacts` v0.7.0 writer, so extractor output and game-pack
consumers use one graph validator rather than parallel wire implementations.

Generate a complete plan for every Unity story chapter represented by an index:

```bash
r1999-live-sequence \
  --story-index /path/to/story-index.jsonl \
  --output /path/to/live-sequence-plan.json
```

Limit a diagnostic run to one chapter with a repeatable `--chapter` option:

```bash
r1999-live-sequence \
  --story-index /path/to/story-index.jsonl \
  --chapter 314601 \
  --output /path/to/live-sequence-plan.json
```

The 2026-08-29 validation against installed patch 3.7 data and the published
Character Story index produced 104 events for chapter `314601`: 89 bound speech
events, three automatic silent ellipses, eleven passive transitions and one
terminal transition. The VNTTS schema v1 consumer accepted the document and all
89 story line bindings.

A second full installed-corpus validation covered all 2,228 Unity story assets.
The selected story index intersected 2,075 chapters and produced 152,989 events:
125,875 bound speech, 3,205 silent, 21,143 passive/terminal transitions, 19
standalone no-text choices and 2,747 manual waits. Choice records attached to a
speech event remain `speech` with manual control and explicit branch successors.
The VNTTS consumer accepted the complete graph; 1,060 chapters expose multiple
entry anchors for disconnected branch segments or sequence gaps.
