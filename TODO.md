# TODO

Keep only game-specific extraction and source-asset preparation here. VNTTS
owns speech synthesis, generation jobs, model evaluation, and final game-pack
assembly. Completed work is recorded in git history and stable behavior belongs
in project documentation.

## Source voice coverage

- [ ] Finish the checksum-bound Character Story audition for Aderyn, Mrs. Owen
      and Hotelier. Aderyn's Father, Dobharchú and Poacher have been imported
      from exact user-selected media into the VNTTS voice manifest. Preserve
      Aderyn's `hero3146` and `npcnoname326` portrait variants; two crying-only
      candidates are rejected. Add a fail-closed `--include-all-bank-media`
      audit mode only for a requested role whose exact source bank maps to one
      role/portrait identity; record unrouted media origin and Wwise event IDs,
      and keep every added candidate manual-review-only. Use it to publish the
      user-accepted Mrs. Owen media `562400954` without inventing a story-row
      transcript, and to inventory Hotelier's complete five-clip bank. Hotelier
      is an expected one-off Character Story gap: do not search other versions,
      public recordings, reused portraits or generic NPC banks. Do not merge
      `Poacher I`, `Poacher II` or Glyndŵr or treat configured-unavailable audio
      as installed. Exact hashes and blockers are in
      `docs/source-voice-coverage.md`.

- [ ] Add or explicitly assign references for the remaining 51 patch 3.7 lines
      whose 15 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and eleven other patch speakers now use verified local game audio.
      The exhaustive installed-data blockers and required future identity
      evidence are recorded in `docs/source-voice-coverage.md`.
