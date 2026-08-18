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

`r1999-story-voice-evidence` implements these as a separate checksum-bound
sidecar. PCM frame activity, zero crossings and spectral flatness are always
available. An explicitly supplied local Whisper model adds transcript,
similarity, word error rate and non-speech markers. An explicitly supplied
local WavLM x-vector model adds within-clip segment consistency and pairwise
similarity only inside the exact character/portrait/bank group. Model-directory
bytes are bound by a tree SHA-256; model loading is offline-only.

The acoustic rules are heuristics, not classifiers of identity. Repetitive
laughter/vocalization or an ASR non-speech marker with no matching lexical
transcript may become an obvious-rejection candidate. Low ASR similarity,
broadband-noise risk, a possible speaker change or an embedding outlier only
changes review priority. Every result retains the `advisory-only` policy, and
none can approve a clip, merge portraits or create a manifest entry.

## Checksum-bound human decisions

The report-driven Qt interface shows the expected transcript, adjacent story
context, portrait, bank, media ID, current technical evidence and missing-source
coverage counts for both the character and exact portrait. It supports Play,
Accept, Reject and Uncertain, keyboard navigation and retained A/B candidates.
Its fixed controls do not move during playback, the table cannot enter cell
editing, and playback uses checksum-verified bytes held in memory.

Decisions belong in a versioned review document that binds the candidate report
SHA-256 and every selected reference SHA-256. Reopening an unchanged report is
idempotent. Changed report or WAV bytes invalidate only affected decisions.
Rejected and uncertain candidates remain explicit evidence; they are never
deleted or silently re-proposed.

`r1999-story-voice-review` validates the entire immutable candidate inventory,
emits stable candidate keys and persists Accept/Reject/Uncertain decisions in
an adjacent `review.json`. The document is atomically written. Legacy v1
reviews deliberately fail closed on any report change. Review v2 carries a
decision to a regenerated report only when the candidate key and exact WAV
SHA-256 remain identical; changed or removed candidates are retained separately
as invalidated evidence. `r1999-story-voice-review-ui` is the interactive view
over the same authority and never edits the candidate report or WAVs.

Coverage counts mean speakable story-index lines for the exact character whose
source audio is not currently available. The portrait count is the strict
subset matching the candidate portrait; neither count promises that variants
may be merged. Context comes from the source row's checksum-bound previous and
next story text.

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
