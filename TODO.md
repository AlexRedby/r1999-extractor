# TODO

Keep game-specific extraction and generation preparation here. VNTTS should
only consume versioned, game-agnostic artifacts.

## Repository boundary

- [x] Keep decrypted configs, extracted story text/audio, indexes, generation
      queues, generated voices, and voice packs in the platform-local data
      directory rather than the Git worktree.
- [x] Replace the checked-in `data/reverse1999-npc-catalog.json` with a
      versioned catalog generated from the installed game. Store manual identity
      corrections and approved-reference decisions in a separate local overlay,
      migrate the existing decisions, validate the merged result, and only then
      remove the game-derived catalog from Git.
- [x] Add a repository guard that rejects extracted text, audio, decrypted
      configs, generated indexes/queues, and unexpected large binaries while
      continuing to allow small synthetic test fixtures.
- [x] Document and test a clean-machine workflow that rebuilds every required
      local artifact from an installed game plus the optional local review
      overlay, without downloading or committing game content.

## Completed foundation

- [x] Move config, Unity, Wwise, catalog, alias, review, audition, and import
      tooling out of VNTTS into this repository.
- [x] Extract `json_story_step_*` English speakable text into
      `vntts.story-index` JSONL with stable IDs, text hashes, story context,
      speaker aliases, portrait/timing metadata, and narration classification.
- [x] Resolve source-audio status through normalized cue IDs, config event/bank
      rows, Wwise FNV-1 event IDs, event routes, and actual embedded or streamed
      media availability.
      - Verified installed-game inventory: 125,875 English speakable lines;
        48,975 with local audio, 63,419 definite no-audio, 13,478 configured but
        unavailable locally, and 3 unresolved config IDs.
- [x] Preserve old VNTTS indexes, reviews, and voice packs through a non-
      destructive migration command.

## Shared artifact contract

- [x] Configure an `origin` remote and push this repository before another
      project starts depending on its published artifacts or packages.
- [x] Extract a narrow shared artifact package with VNTTS for the story-index
      schema, voice-manifest v2 schema, integrity checks, and atomic publication
      helpers. Keep game extraction, provider integration, and unrelated utility
      code local to this repository.
- [x] Replace the duplicated `atomic_io.py`, `file_integrity.py`, and relevant
      artifact naming helpers with imports from the shared package after both
      producer and consumer compatibility tests pass.

## Text-source coverage

- [x] Classify the 25 anecdote chapters and publish separate counts and queue
      groupings.
- [x] Extract `json_hero_story_plot` interactive character stories, including
      dialogue and narration without direct voice IDs.
      - Current installed configs add 3,254 speakable records across the
        populated groups: 1,818 dialogue and 1,436 narration records.
- [x] Search and extract story-like text from events, activities, tutorials,
      battle dialogue, tips, optional branches, mail, and other config/bundle
      sources. Keep this schema-driven: table names alone are not sufficient to
      distinguish spoken text from UI copy, objectives, and descriptions.
      - Current installed configs add 18,844 records across 32 explicit table
        schemas; 1,105 resolve to installed audio, 333 preserve optional source
        routes, and 17,406 have no source voice cue.

## Ahead-of-time voice generation

- [x] Build a versioned generation queue from every line whose status is not
      `installed`, grouped by canonical character and story order.
      - `no_audio` is ready to generate, `configured_unavailable` preserves a
        preference for source audio, and `unresolved` remains a manual-review
        item instead of being silently synthesized.
- [x] Add emotion/delivery annotations using speaker, surrounding dialogue,
      scene context, punctuation, and model-specific prompt adapters.
- [x] Add resumable bulk generation with provider/model/prompt/seed provenance,
      retries, technical quality checks, manual review, and atomic publication.
- [x] Emit a generic generated-audio manifest mapping stable line IDs and text
      hashes to generated files, then add exact lookup support in VNTTS.
- [x] Add a reproducible, emotion-stratified local-model benchmark with shared
      technical metrics and a manual listening rubric.
- [ ] Complete the human listening scores for the locally installed candidate
      models and select the production model before generating the full
      estimated backlog. This is an explicit perceptual acceptance gate, not an
      automatically inferred quality score.
