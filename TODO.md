# TODO

Keep only game-specific extraction and source-asset preparation here. VNTTS
owns speech synthesis, generation jobs, model evaluation, and final game-pack
assembly. Completed work is recorded in git history and stable behavior belongs
in project documentation.

## Source voice coverage

- [ ] Recover and import exact Character Story references for Aderyn,
      Dobharchú, Mrs. Owen, Hotelier, Poacher and Aderyn's Father. Bind selected
      clips to installed bank/media SHA-256 values, score and listen to them,
      and preserve Aderyn's `hero3146` versus `npcnoname326` portrait variants
      until their identity is verified. Re-run the VNTTS voice preflight; these
      candidates can potentially cover 214 of the current 237 blocked lines.
      Re-audit `Poacher I`, `Poacher II` and Glyndŵr, but do not merge named
      roles or treat configured-unavailable audio as installed. Evidence and
      exact current counts are in `docs/source-voice-coverage.md`.

- [ ] Add or explicitly assign references for the remaining 51 patch 3.7 lines
      whose 15 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and eleven other patch speakers now use verified local game audio.
      The exhaustive installed-data blockers and required future identity
      evidence are recorded in `docs/source-voice-coverage.md`.
