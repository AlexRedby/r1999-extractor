# TODO

Keep only game-specific extraction and source-asset preparation here. VNTTS
owns speech synthesis, generation jobs, model evaluation, and final game-pack
assembly. Completed work is recorded in git history and stable behavior belongs
in project documentation.

## Source voice coverage

- [ ] Listen to and decide the checksum-bound Character Story audition set,
      then import only accepted references for Aderyn, Dobharchú, Mrs. Owen,
      Hotelier, Poacher and Aderyn's Father and re-run VNTTS preflight. The
      source/index/bank/media extraction is complete: 53 candidates across 19
      portrait/bank groups, 12 objective technical passes and two transcript
      conflicts. Preserve Aderyn variants and the anomalous Mrs. Owen group;
      Hotelier has no minimum-duration pass. Do not merge `Poacher I`, `Poacher
      II` or Glyndŵr or treat configured-unavailable audio as installed. Exact
      hashes and blockers are in `docs/source-voice-coverage.md`.

- [ ] Add or explicitly assign references for the remaining 51 patch 3.7 lines
      whose 15 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and eleven other patch speakers now use verified local game audio.
      The exhaustive installed-data blockers and required future identity
      evidence are recorded in `docs/source-voice-coverage.md`.
