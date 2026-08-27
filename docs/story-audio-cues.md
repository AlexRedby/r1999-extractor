# Story audio cue provenance

Reverse: 1999 story steps may declare ordered audio cues beside, but separate
from, the dialogue payload. `r1999-story-index` preserves those declarations in
the additive `story_audio_cues` field on each line record. They are never used
as `source_audio_id`, which remains the source voice route for the spoken line.

Each cue record contains:

- `cue_index`, preserving source order and repeated IDs;
- `source_audio_id`;
- six positional parameters named for their source-array positions rather than
  guessed semantics;
- `audio_status`, `audio_reason`, `source_audio_status`, `source_event`,
  `source_bank`, `source_media_ids`, and `available_media_ids` from the same
  Wwise registry and installed-bank index used for source voices.

The English patch 3.7 bundle contains 64,779 cue records. Every observed cue is
a seven-field list with integer fields at positions 0, 1, 3 and 6, numeric
localized lists at positions 2 and 5, and a numeric scalar at position 4. A
non-empty row with another shape is rejected instead of being silently dropped.
The positional fields are provenance only: the extractor does not claim, for
example, that codes 1 and 2 mean play and stop.

After the normal English/speakable filtering and config-only source merge, the
regenerated patch 3.7 story index contains 38,939 cues on 26,586 lines: 282
installed, 38,654 configured but unavailable, and 3 unresolved. Metadata records
these counts as `story_audio_cue_count` and `story_audio_cue_status_counts`.

## Inline-marker census

The current Character Story generation queue has ten lines containing an
inline non-verbal marker. Source voice audio is absent for all ten. The story
cue evidence is narrower than the translated marker text:

- `reverse1999:314602:47`, `*bang*`, declares audio ID `501787` twice. It
  resolves to event `play_activitystorysfx_wangshi_k_door`; the configured bank
  is not installed.
- `reverse1999:314602:20`, `*pop*`, includes audio ID `501237`, event
  `play_activitystorysfx_shiji_vortex`, alongside other cues. The bank is not
  installed, and the extractor does not select one cue as the exact `pop`.
- `reverse1999:314607:55`, `*bang*`, includes IDs `501304` and `501282`, whose
  configured events describe earth energy and seismic audio. Their bank is not
  installed.
- `reverse1999:314607:66`, `*buzzzzz*`, includes ID `37500226`, event
  `play_activitystorysfx_beiai_hunting`. Its bank is not installed.
- `reverse1999:314607:83`, `*gasp*`, includes IDs `25500117` and `37500224`,
  configured as stream/water-flow events. Neither is asserted to be a human
  gasp, and neither bank is installed.
- `reverse1999:314607:84`, `*gurgle*`; `reverse1999:314606:39`, `Tsk!`;
  `reverse1999:314604:71`, `*whimper*`; and `reverse1999:314608:48`, `*yelp*`
  declare no story-step audio cue.

Repeated `*bang*` lines account for the remaining marker occurrences. This
evidence lets VNTTS distinguish an exact installed game event from an adjacent
or unavailable cue. It does not authorize substituting ambient water, a door,
or another nearby effect for a translated vocal marker.

## Consumer contract

Consumers should treat `story_audio_cues` as lossless producer-owned extension
data. An exact game effect is reusable only when its cue identity is appropriate
for the requested event and its resolved media is installed and checksum-bound.
Configured-but-unavailable cues remain discovery evidence. Empty cue lists are
an explicit absence of story-step cue provenance, not proof that the spoken
marker should be pronounced literally.
