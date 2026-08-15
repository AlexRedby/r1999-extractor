# TODO

Keep game-specific extraction and generation preparation here. VNTTS should
only consume versioned, game-agnostic artifacts. Completed work is recorded in
git history and stable behavior belongs in project documentation.

## Ahead-of-time voice generation

- [x] Add a desktop pregeneration workbench for selecting main-story chapters
      or complete anecdotes, starting resumable MOSS jobs, and monitoring live
      and previous generation status without manual queue commands.
- [ ] Add or explicitly assign references for the remaining 54 patch 3.7 lines
      whose 17 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and seven other patch speakers now use verified local game audio.
- [ ] Generate and review the 1,220 `no_audio` lines introduced by patch 3.7
      with the approved `moss-tts-local-transformer-v1.5-mlx` production model:
      Chapter 13 (`On Another's Sorrow`) and Silverwing Eagle's
      `The Eaglet Takes Wing`. Preserve source-audio candidates instead of
      replacing them with generated speech.

## Versioned VNTTS delivery

- [ ] Export a versioned `vntts.game-pack` that binds the story index, voice
      manifest, generated audio, game and extractor versions, and SHA-256
      checksums for VNTTS import and preflight validation.

## Game update compatibility

- [ ] Add `r1999-update-diff` to report new, removed, and changed line IDs,
      schema drift, speaker-mapping changes, unresolved-audio spikes, and the
      generation-queue delta for each game update.
