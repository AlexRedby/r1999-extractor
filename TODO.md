# TODO

Keep only game-specific extraction and source-asset preparation here. VNTTS
owns speech synthesis, generation jobs, model evaluation, and final game-pack
assembly. Completed work is recorded in git history and stable behavior belongs
in project documentation.

## Source voice coverage

- [ ] Add or explicitly assign references for the remaining 54 patch 3.7 lines
      whose 17 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and seven other patch speakers now use verified local game audio.

## Narrow the extractor boundary

- [ ] Keep only source facts in extracted artifacts: speaker identity, story
      structure, original-audio status and reason, source provenance, and
      reviewed voice references. Remove model prompts and generation actions
      from extractor-owned output.
- [ ] After VNTTS can import existing jobs, remove generic bulk generation,
      MOSS generation, model benchmark/listening, delivery annotation, and
      pregeneration workbench modules and their command entry points from this
      package.
- [ ] Provide a non-destructive migration or compatibility message for the old
      `r1999-generate`, `r1999-benchmark`, `r1999-listen`, and
      `r1999-pregenerate` commands until existing local jobs are discoverable in
      VNTTS.
- [ ] Verify that a headless extractor installation no longer requires a VNTTS
      Python environment, speech model runtime, or playback abstraction; retain
      Qt only as an optional dependency for source-reference audition.

## Versioned VNTTS delivery

- [ ] Export the versioned source artifacts and producer provenance required by
      the shared contract, including game and extractor versions, story index,
      voice manifest and references, source-audio facts, and SHA-256 checksums.
