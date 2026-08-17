# Legacy authoring migration

Generic speech authoring is owned by VNTTS. The extractor no longer installs
or contains generation-queue construction, deterministic delivery annotation,
bulk provider execution, MOSS generation, generated-audio review/publication,
or the Qt pregeneration workbench.

## Preservation evidence

Before removal, VNTTS imported all three known extractor histories into
content-addressed, immutable application-data directories:

- `legacy-12888f0d08ffe96b5be29f7b`: 592 queue items and 338 approved results;
- `legacy-395a5e5eec0327a3a793b66d`: 592 queue items, 197 generated pending-review
  results, and 141 failures;
- `legacy-14d28505d16f4729c363c2de`: 1,220 Patch 3.7 queue items and 680 generated
  pending-review results at import time.

Import validates the legacy queue, state, generated-audio manifest, job
document, and copied WAV inventory by schema and SHA-256. It copies through a
private staging directory, atomically publishes without overwriting, and is
idempotent for an identical source. The extractor-owned source directories are
not modified or deleted.

VNTTS then passed two complementary controlled real-data gates:

- a Patch 3.7 selection resumed successfully into a separate workspace and
  produced one finite 48 kHz mono pending-review WAV on attempt 1/seed 0 while
  every other state record and both immutable source inputs stayed unchanged;
- a selected `legacy-395a5e5eec0327a3a793b66d` failure advanced cumulative
  attempt/seed state, reached its bounded MOSS limit, published no partial WAV,
  left unrelated records and source hashes unchanged, and cleared its lease.

The newer history still needs a successful synthesis of another
preflight-ready natural line. That is an operational model/content acceptance
task in VNTTS, not a dependency on extractor authoring code: successful output
publication is already covered by the Patch resume, while the newer-history
gate proved preservation, continuation, and failure cleanup on that exact
history. Therefore it does not block removal of the extractor implementation.

The production-model decision is preserved with the migration evidence. On
2026-08-15, the owner approved `moss-tts-local-transformer-v1.5-mlx` after all
45 blind trials were completed. MOSS recorded 16 wins, no losses, and one tie
across 17 reviewed comparisons, a 97.06% preference rate. This is perceptual
acceptance only for the scoped patch 3.7 work; it does not approve any generated
line or final pack.

## Current workflow

Use `vntts-pregenerate` for queue planning, delivery policy, generation,
review, publication, workspaces, and non-destructive legacy import. VNTTS reads
the versioned story index and voice manifest through `vntts-artifacts`; it does
not import extractor modules or interpret Reverse: 1999 IDs.

Historical local job directories remain preservation sources only. Do not run
or recreate extractor authoring commands against them. Inspect or re-import
them through VNTTS, and resume only in a separate content-addressed workspace.

The completed benchmark and blind-listening migrations remain documented in
[`legacy-benchmark-migration.md`](legacy-benchmark-migration.md) and
[`legacy-listening-migration.md`](legacy-listening-migration.md).
