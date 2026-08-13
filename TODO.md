# TODO

Keep game-specific extraction and generation preparation here. VNTTS should
only consume versioned, game-agnostic artifacts.

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

## Remaining text sources

- [ ] Classify the 25 anecdote chapters and publish separate counts and queue
      groupings.
- [ ] Extract `json_hero_story_plot` interactive character stories, including
      dialogue and narration without direct voice IDs.
- [ ] Search and extract story-like text from events, activities, tutorials,
      battle dialogue, tips, optional branches, mail, and other config/bundle
      sources.

## Ahead-of-time voice generation

- [ ] Build a versioned generation queue from every line whose status is not
      `installed`, grouped by canonical character and story order.
- [ ] Add emotion/delivery annotations using speaker, surrounding dialogue,
      scene context, punctuation, and model-specific prompt adapters.
- [ ] Add resumable bulk generation with provider/model/prompt/seed provenance,
      retries, technical quality checks, manual review, and atomic publication.
- [ ] Emit a generic generated-audio manifest mapping stable line IDs and text
      hashes to generated files, then add exact lookup support in VNTTS.
- [ ] Compare higher-quality offline models for natural emotion control and
      voice consistency before generating the full estimated 95-hour backlog.
