# TODO

Keep only game-specific extraction and source-asset preparation here. VNTTS
owns speech synthesis, generation jobs, model evaluation, and final game-pack
assembly. Completed work is recorded in git history and stable behavior belongs
in project documentation.

## Adopt graph-derived story exports

- [ ] Deliver the extractor revision to VNTTS and rebuild its story index, live
      sequence plan and dependent preparation manifests together. Keep raw game
      IDs; do not reuse checksums from the old numeric-order export. Verify
      `314001` follows `24 -> 25 (hidden) -> 111 -> 26` and visible neighbors are
      `24 -> 111 -> 26`. Check consumer preloading/replay presentation that still
      sorts `sequence` numerically before accepting the new artifacts.

## Installed graph coverage

- [ ] Resolve or explicitly exclude missing-node chapters before exporting their
      live plans: `203606` has `27 -> missing90 -> 28`; `203607` has
      `44 -> missing46 -> 41`; `203608` has `43 -> missing116 -> 44`; `203610`
      has `8 -> missing49 -> 9`. Debug chapter `107` also references absent
      steps `3..50`. Obtain matching game assets/config evidence; do not invent
      a bypass through absent nodes. Other chapters must remain extractable.
- [ ] Decide how live export should handle raw records outside the game's graph,
      including `100003:45`, `203120:174`, `203609:118,119`, `3056106:1`,
      `3056112:5`, `305629:21`, `305686:1`, `306301:39`, `309516:152..158`
      and `400401:54,55`. Current safe behavior rejects those live chapters;
      archive extraction retains the records. Do not promote them to game starts
      or add edges without an explicit supported manual-anchor contract.

## Core test environment

- [ ] Declare the test dependency needed by source-audio semantics tests: a clean
      `uv sync --locked` environment lacks `scipy`, so seven test cases error
      before testing resampling. Keep optional ASR runtime dependencies separate
      from the story extraction dependency set.

## Source voice coverage

- [ ] Add or explicitly assign references for the remaining 51 patch 3.7 lines
      whose 15 speakers are not covered by the current voice manifest. Do not
      silently render named characters with the narrator voice. Silverwing
      Eagle and eleven other patch speakers now use verified local game audio.
      The exhaustive installed-data blockers and required future identity
      evidence are recorded in `docs/source-voice-coverage.md`.
