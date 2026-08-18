# Semi-automatic source-reference audition

Source-reference selection should minimize listening without inventing speaker
identity. The extractor owns game evidence and source-audio preparation; VNTTS
owns synthesis previews and final authoring controls.

## Evidence that can be automated

Every candidate starts from one exact story row containing the localized
speaker, portrait, text and voice cue. The cue is resolved through the game
audio configuration to an exact Wwise event, bank and media ID. Bank bytes,
media bytes and the normalized WAV remain checksum-bound.

The extractor can then calculate evidence that ranks or rejects candidates:

1. Existing PCM, clipping, duration, silence and activity-window checks.
2. Speech/non-speech classification so crying-only, breaths and sound effects
   are not proposed as ordinary cloning references.
3. Local ASR alignment against the exact game transcript, including detected
   language, word error rate and missing/extra speech. Low-confidence or
   conflicting transcripts remain manual.
4. Music/SFX and speaker-count evidence. These classifiers may reject obvious
   contamination or prioritize review, but may not silently prove a clean clip.
5. Speaker embeddings grouped by exact character, portrait and bank. They can
   identify outliers and consistent clusters but may not merge portrait groups
   until a human approves one anchor per group.
6. Coverage impact, so review starts with groups that unblock the most queued
   lines rather than scanning every extracted WAV.

## Checksum-bound human decisions

A report-driven review interface should show the expected transcript, adjacent
story context, portrait, bank, media ID, technical/ASR/contamination evidence,
cluster membership and affected line count. It should support Play, Accept,
Reject and Uncertain, keyboard navigation and A/B comparison between groups.

Decisions belong in a versioned review document that binds the candidate report
SHA-256 and every selected reference SHA-256. Reopening an unchanged report is
idempotent. Changed report or WAV bytes invalidate only affected decisions.
Rejected and uncertain candidates remain explicit evidence; they are never
deleted or silently re-proposed.

The first accepted clip in each ambiguous portrait/bank cluster is a human
anchor. After that, strict same-cluster candidates may be automatically ranked,
but importing a new voice identity still requires either an accepted anchor or
an exact previously approved checksum.

## VNTTS handoff

The extractor publishes only selected source references and their provenance.
VNTTS should consume the review document without flattening distinct portrait
variants. For every accepted candidate or cluster it should synthesize the same
short evaluation corpus, then offer a blind reference/result comparison.
Source quality and generated quality are separate gates: a clean source clip
can still produce poor cadence, pronunciation or speaker similarity.

Only after both gates pass should VNTTS create a new config-addressed workspace,
re-run preflight and generate the newly covered queue IDs. Existing immutable
workspaces and review decisions are never rewritten.
