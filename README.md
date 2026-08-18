# Reverse: 1999 Extractor

Local extraction and source-reference review tools for creating generic story
and voice-pack artifacts from an installed copy of Reverse: 1999. This
repository owns game-specific discovery, encrypted configuration parsing,
Unity story parsing, Wwise indexing, NPC identity mapping, reference selection,
and source audition.

It does not include game text, audio, decrypted configuration, generated
speech, or final voice packs. Keep those local and use them only where you have
the right to do so. The repository guard enforces that boundary:

```bash
r1999-repository-guard
```

Real game-derived artifacts live under the platform application-data
directory. Only code, documentation, and small synthetic fixtures belong in
Git.

## Boundary with VNTTS

This project knows the Reverse: 1999 formats; VNTTS owns generic speech
authoring and playback. Their integration boundary is versioned local
artifacts:

- `story-index.jsonl`: `vntts.story-index` schema version 1;
- `manifest.json`: the generic VNTTS character voice manifest;
- `game-pack.json`: `vntts.game-pack` schema version 1, with portable SHA-256
  bindings for the story index, voice manifest, and every reference WAV.

The JSONL file starts with one metadata record followed by line records. Each
line has a stable ID, chapter, sequence, speaker, text, and source information.
The metadata record also contains a `collections` catalog for player-visible
main-story chapters, anecdotes, and character stories. Every collection has a
stable game-derived ID, localized title, generic kind, and display order.

Compatibility with the released `vntts-artifacts` v0.6.1 APIs is covered by a
synthetic end-to-end fixture. The lossless `StoryIndexDocument` API preserves
Reverse: 1999 source-audio and collection fields while generic authoring code
consumes the shared contract without importing this package.

Generic generation queues, delivery annotations, MOSS generation, review,
publication, and the pregeneration workbench have moved to VNTTS. Their legacy
extractor modules and commands were removed after all three local histories
were imported immutably and VNTTS passed controlled real resume gates. Existing
source directories remain untouched; see
[`docs/legacy-authoring-migration.md`](docs/legacy-authoring-migration.md).

## Setup

```bash
uv sync --group dev
```

This creates the locked headless/source-only environment. It does not install
speech models, a playback runtime, or Qt. Run commands through `uv run`, for
example `uv run r1999-bootstrap`.

Install Qt only for local source-reference audition:

```bash
uv sync --group dev --extra ui
```

`r1999-audition` loads Qt lazily and explains the optional extra when it is
absent. Source extraction remains usable without it.

## Source-only artifact boundary

Source-owned outputs contain game facts only: story structure and text,
speaker identity, original-audio status and provenance, collection metadata,
and reviewed voice references. They do not contain synthesis actions, model or
seed selection, delivery annotations, emotion prompts, or provider adapters.
`r1999-bootstrap` produces the Wwise bank index, NPC/reference catalog, story
index, and source audit, then stops.

Export a portable source delivery after producing a story index and reviewed
voice manifest:

```bash
r1999-source-pack \
  --story-index /path/to/story-index.jsonl \
  --voice-manifest /path/to/voice-pack/manifest.json \
  --game-version 3.7 \
  --output /path/to/reverse1999-3.7-source-pack
```

The output directory must not already exist. It contains copied source
artifacts, copied reference WAVs, and a `game-pack.json` with stable game and
producer identity plus derived checksums. It deliberately has no
`generated_audio` component. See
[`docs/source-game-pack.md`](docs/source-game-pack.md).

## Extract all story text

For a clean installation, rebuild every required local artifact in dependency
order:

```bash
r1999-bootstrap --game-version installed
```

This discovers installed configs, the story bundle, and English Wwise banks,
then creates the bank index, merged NPC catalog, story index, and source audit.
An optional local `npc-catalog-overlay.json` preserves manual name corrections
and approved reference decisions without committing them.

Automatic discovery supports the macOS/iOS-container installation layout and
common Windows `ResLib` layouts:

```bash
r1999-story-index
```

Or provide the installed resource directory explicitly:

```bash
r1999-story-index \
  --resource-root /path/to/ResLib/iOS \
  --output ./output/story-index.jsonl
```

By default, the command keeps English speakable records, strips Unity rich
text, normalizes known aliases, adds adjacent-line context, classifies legacy
anecdotes, includes config-only interactive hero stories, and resolves every
original voice cue against decrypted audio configuration and installed Wwise
media. It rebuilds a stale bank index automatically. Check freshness without
rebuilding:

```bash
r1999-bank-index --check
```

Every output line has one explicit audio status:

- `installed`: the configured event resolves to local media;
- `no_audio`: the story has a blank or zero voice cue;
- `configured_unavailable`: a route exists but its bank, event, or media is
  not installed;
- `unresolved`: the story references an ID absent from installed config.

Use `--include-non-speakable` for a preservation-oriented export containing
localized test, placeholder, and non-English records. Use
`--skip-audio-resolution` only for a text-only index.

## Compare extracted game updates

```bash
r1999-update-diff old/story-index.jsonl new/story-index.jsonl \
  --output update-diff.json
```

The versioned report lists new, removed, and changed stable line IDs; schema
drift; speaker and canonical voice changes; unresolved source-audio increases;
and changes in source-only synthesis eligibility. It never constructs a
generation queue.

Audit every story-like configuration table and see which explicit schema owns
its localized text:

```bash
r1999-source-audit
```

## Existing voice-reference workflow

```bash
r1999-batch scan
r1999-audition
```

The batch, catalog, Wwise, quality-scoring, and audition commands preserve the
game-specific source-reference workflow. Patch coverage counts, accepted
game-derived identity evidence, checksums, and the procedure for refusing
ambiguous assignments are in
[`docs/source-voice-coverage.md`](docs/source-voice-coverage.md).

Playable-character voice text can be bound directly to installed game audio,
without downloading Wiki clips:

```bash
uv run r1999-playable-voice-index "Paper Heron" \
  --output /path/to/paper-heron-voice-index.json
```

The index records official English text, source voice IDs, Wwise events, banks,
media IDs, and SHA-256 identities for the bank and exact source media bytes.
When `--story-index` is omitted, the command selects the newest local
`story-index*.jsonl`; pass an explicit path to reproduce an older extraction.
See [`docs/source-voice-coverage.md`](docs/source-voice-coverage.md) for the
selection and import procedure.

For named Character Story roles that have silent target rows but installed
same-speaker dialogue elsewhere, build a non-authoritative audition set first:

```bash
uv run r1999-story-voice-candidates \
  --role Aderyn --role Dobharchú --role "Mrs. Owen" \
  --role Hotelier --role Poacher --role "Aderyn's Father" \
  --output /path/to/character-story-reference-candidates
```

The command snapshots and revalidates the story index, bank index and exact
bank bytes; verifies every Wwise event-to-media route; decodes only those media
payloads; and records source/reference SHA-256 plus technical metrics. Output is
an audition set, not a voice manifest: every candidate remains marked for
manual speaker, music/SFX and multiple-speaker review. Existing output is never
replaced.

The semi-automatic review contract, including transcript alignment,
non-speech/contamination evidence, portrait-aware clustering and checksum-bound
human decisions, is documented in
[`docs/reference-audition-automation.md`](docs/reference-audition-automation.md).

Record or inspect checksum-bound decisions without editing the immutable
candidate report:

```bash
uv run r1999-story-voice-review /path/to/candidates/report.json
uv run r1999-story-voice-review /path/to/candidates/report.json \
  --candidate-key SHA256_KEY --decision uncertain \
  --notes "Different portrait voice; keep separate"
```

The adjacent `review.json` binds the exact report and reference hashes. A
changed WAV fails closed. Review v2 carries unchanged candidate decisions to a
regenerated report by exact candidate/reference identity and archives changed
candidates as invalidated evidence.

For the report-driven Qt workflow, install the UI extra and open the same
immutable report:

```bash
uv sync --extra ui
uv run r1999-story-voice-review-ui /path/to/candidates/report.json
```

The fixed action row supports Play, Accept, Reject, Uncertain and previous/next
pending navigation. `Space` plays, `Ctrl+Enter` accepts, `Ctrl+Backspace`
rejects, `Ctrl+Shift+Enter` marks uncertain, and `Alt+Left` / `Alt+Right`
navigate pending candidates. The table is read-only, so decision shortcuts do
not start cell editing. A/B slots can retain and replay two candidates while
the active filters change.

If extraction artifacts were created while these tools lived in VNTTS, copy
them into the extractor application-data directory without deleting or
overwriting the originals:

```bash
r1999-migrate-vntts-data --dry-run
r1999-migrate-vntts-data
```

## Tests

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests
uv run r1999-repository-guard
```

Without the `ui` extra, Qt audition tests are skipped. CI installs the extra
and runs those UI tests separately.
