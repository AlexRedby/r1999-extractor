# TODO

Keep game-specific extraction and generation preparation here. VNTTS should
only consume versioned, game-agnostic artifacts. Completed work is recorded in
git history and stable behavior belongs in project documentation.

## Ahead-of-time voice generation

- [ ] Complete the human listening scores for the locally installed candidate
      models and select the production model before generating the full
      estimated backlog. This is an explicit perceptual acceptance gate, not an
      automatically inferred quality score.
