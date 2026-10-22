# Live sequence plans

`r1999-live-sequence` exports ordered raw Unity story steps for VNTTS
sequence-first live reading. The plan is bound to the SHA-256 of both the exact
story-index bytes and the installed source bundle bytes.

The producer loads each `json_story_step_<chapter>` asset together with its
`json_story_group_<chapter>` asset. Step IDs come from the game and are stable
identities, not playback positions. Each group contains ordered
`[source_step_id, target_step_id, branch_name]` edges. Numeric adjacency and Unity
serialization order do not determine successors.

- A visible line retained by the story index becomes `speech` and binds its
  exact `line_id`.
- Punctuation-only ellipsis text becomes `silent` and is not sent to TTS.
- Empty-text steps and hidden conversation types `None` (0) and `BgEffStack`
  (10) remain technical transitions. Retained text inside these records is not
  exported as dialogue in any locale.
- Visible text omitted from the selected story index becomes a manual `wait`.
- Explicit group edges determine successors. Numeric gaps can be valid ordinary
  transitions; an inserted high-numbered step can return to a lower ID.
- Choices at raw step index 10 retain their explicit targets and manual control.
  Duplicate option targets are collapsed; target `0` exits the sequence.
  Multiple group successors also require manual control.
- A node with no successors is terminal unless unbound visible text or a choice
  still requires manual handling. The highest numeric ID is not necessarily the
  ending.

Event enumeration follows graph branches stably for archive presentation. It
does not select a player's route. Story-index neighbor context follows only
unambiguous corridors and stops at forks, joins and missing raw steps. `sequence`
and `line_id` retain the original game IDs; `story_order` carries archive order.

The exporter rejects missing paired graph assets, malformed or duplicate raw
steps, missing graph endpoints, and indexed lines that cannot bind a visible
raw source event. Live entries come from the game's group roots (or its single
raw step); disconnected records are not promoted to invented starts. A chapter
whose events cannot be reached from those roots is rejected for live export.
Archive extraction can retain such records without inventing context through
missing nodes.

The plan is filtered to chapters represented by the selected story index.
Source and story file identity are checked again before publication. Final
publication delegates to the shared `vntts-artifacts` writer and graph validator.
Existing indexes and plans must be regenerated together: changed ordering,
context or hidden-line filtering changes the exact story-index checksum.

For example, installed chapter `314001` declares `24 -> 25 -> 111 -> 26`.
Step 25 has conversation type `None`, so its retained Chinese text is hidden.
Step 111 is a normal localized dialogue. The visible sequence is therefore
`24 -> 111 -> 26`; the IDs are never renumbered or matched by equal text.
The synthetic regression in `tests/test_story_graph.py` preserves this topology
without depending on installed game files.

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
