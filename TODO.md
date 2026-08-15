# TODO

Keep game-specific extraction and generation preparation here. VNTTS should
only consume versioned, game-agnostic artifacts. Completed work is recorded in
git history and stable behavior belongs in project documentation.

## Ahead-of-time voice generation

- [ ] Generate and review the full estimated audio backlog with the approved
      `moss-tts-local-transformer-v1.5-mlx` production model.

## Versioned VNTTS delivery

- [ ] Export a versioned `vntts.game-pack` that binds the story index, voice
      manifest, generated audio, game and extractor versions, and SHA-256
      checksums for VNTTS import and preflight validation.

## Game update compatibility

- [ ] Add `r1999-update-diff` to report new, removed, and changed line IDs,
      schema drift, speaker-mapping changes, unresolved-audio spikes, and the
      generation-queue delta for each game update.
