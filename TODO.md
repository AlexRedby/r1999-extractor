# TODO

Keep game-specific extraction and generation preparation here. VNTTS should
only consume versioned, game-agnostic artifacts. Completed work is recorded in
git history and stable behavior belongs in project documentation.

## Ahead-of-time voice generation

- [ ] Complete the human listening scores for the locally installed candidate
      models and select the production model before generating the full
      estimated backlog. This is an explicit perceptual acceptance gate, not an
      automatically inferred quality score.

## Versioned VNTTS delivery

- [ ] Export a versioned `vntts.game-pack` that binds the story index, voice
      manifest, generated audio, game and extractor versions, and SHA-256
      checksums for VNTTS import and preflight validation.

## Game update compatibility

- [ ] Add `r1999-update-diff` to report new, removed, and changed line IDs,
      schema drift, speaker-mapping changes, unresolved-audio spikes, and the
      generation-queue delta for each game update.
