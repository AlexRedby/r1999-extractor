# TODO

Keep only game-specific extraction and source-asset preparation here. VNTTS
owns speech synthesis, generation jobs, model evaluation, and final game-pack
assembly. Completed work is recorded in git history and stable behavior belongs
in project documentation.

## Source voice coverage

- [ ] Parse and provenance-bind playable-character voice lines from local game
      banks instead of relying on manually sourced Wiki clips. Start with Paper
      Heron: reconcile the official character/Wiki line list with installed
      `hero3141_mainvoc.bnk`, `hero3141_vo.bnk`, and character-story media; keep
      the unavailable patch 3.4 `activityvoc_hero3141_3_4_bulaochun_part*`
      story events distinct from reusable playable-character references.
- [ ] Add or explicitly assign references for the remaining 51 patch 3.7 lines
      whose 15 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and eleven other patch speakers now use verified local game audio.
