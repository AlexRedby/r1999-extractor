# Reverse: 1999 Extractor

Local extraction and review tools for creating generic story and voice-pack artifacts from an installed copy of Reverse: 1999. This repository contains game-specific discovery, encrypted configuration parsing, Unity story parsing, Wwise indexing, NPC identity mapping, reference selection, and audition tooling.

It does not include game text, audio, decrypted configuration, or generated voice packs. Keep those local and use them only where you have the right to do so.

The repository guard enforces that boundary:

```bash
r1999-repository-guard
```

Real game-derived artifacts live under the platform application-data directory.
Only code, documentation, and small synthetic test fixtures belong in Git.

## Boundary with VNTTS

This project knows the Reverse: 1999 formats. VNTTS does not. The integration boundary is versioned local artifacts:

- `story-index.jsonl`: `vntts.story-index` schema version 1
- `manifest.json`: the existing generic VNTTS character voice manifest

The JSONL file starts with one metadata record followed by line records. Each
line has a stable ID, chapter, sequence, speaker, text, source information, and
optional delivery hints. The metadata record also contains a `collections`
catalog for player-visible main-story chapters, anecdotes, and character
stories. Every catalog entry has a stable game-derived `collection_id`,
localized `title`, generic `kind`, and display `order`; member lines carry the
same `collection_id`. These are additive producer fields in schema version 1,
so existing readers can ignore them while authoring tools avoid reproducing
Reverse: 1999 chapter arithmetic.

The legacy `r1999-generation-queue` command can still emit
`generation-queue.jsonl` using `vntts.voice-generation-queue` schema version 1.
Queue items are pinned to a stable line ID and text hash, so text changes cannot
accidentally reuse stale generated audio. Queue construction is no longer part
of extraction bootstrap and remains available only for compatibility while
generation workflows move to VNTTS.

Compatibility with the released `vntts-artifacts` v0.5.0 APIs is covered by a
synthetic end-to-end fixture. It writes and loads a story index and voice
manifest, preserves Reverse: 1999 source-audio and collection producer fields,
and creates and validates portable relative-path SHA-256 bindings for the story
index, voice manifest, and source audio. Version 0.5.0 provides those binding
helpers, not a complete `vntts.game-pack` document API; the extractor does not
claim or construct an unreleased pack envelope.

## Setup

```bash
uv sync --group dev --extra ui
```

This creates a locked local environment with the optional Qt audition UI. For
headless extraction and core tests only, omit `--extra ui`. Run project commands
through `uv run`, for example `uv run r1999-bootstrap`.

## Extract all story text

For a clean installation, rebuild every required local artifact in dependency
order with one command:

```bash
r1999-bootstrap --game-version installed
```

This discovers the installed configs, story bundle, and English Wwise banks,
then creates the bank index, merged NPC catalog, story index, and source audit.
It stops after source artifacts and does not create a synthesis queue. An
optional local `npc-catalog-overlay.json` preserves manual name corrections and
approved reference decisions without committing them.

Automatic discovery supports the macOS/iOS-container installation layout and common Windows `ResLib` layouts:

```bash
r1999-story-index
```

Or provide the installed resource directory explicitly:

```bash
r1999-story-index --resource-root /path/to/ResLib/iOS --output ./output/story-index.jsonl
```

By default this command keeps English speakable records, strips Unity rich-text
markup, normalizes known speaker aliases, adds previous/next-line context,
classifies the 25 legacy anecdote chapters, adds config-only interactive hero
stories, and resolves every original voice cue against decrypted audio
configuration and the installed Wwise media. It rebuilds an outdated bank index
automatically. The freshness check compares the indexed path, size, and
nanosecond modification time of every `.bnk` file with the current English audio
directory, so newly downloaded, removed, or replaced voice banks cannot be
silently missed. Check it without rebuilding anything with:

```bash
r1999-bank-index --check
```

Every output line has one explicit audio status:

- `installed`: the configured event resolves to embedded or streamed local media;
- `no_audio`: the story has a blank or zero voice cue;
- `configured_unavailable`: a valid route exists but its bank, event, or media is
  not installed;
- `unresolved`: the story references an ID absent from the installed config.

Use `--include-non-speakable` for a preservation-oriented export containing
localized test, placeholder, and non-English records. Use
`--skip-audio-resolution` only when a text-only index is sufficient.

## Compare extracted game updates

Compare two story indexes after installing or extracting a game update:

```bash
r1999-update-diff old/story-index.jsonl new/story-index.jsonl --output update-diff.json
```

The command writes stable `r1999.update-diff` schema version 1 JSON and prints a
concise summary. The report lists new, removed, and changed stable line IDs;
declared and field-shape schema drift; speaker and canonical voice mapping
changes; explicit `unresolved` source-audio increases; and changes in source-only
synthesis eligibility. A line is eligible when it is speakable and does not
have installed or canonically available original audio. The comparison never
imports or constructs a generation queue.

## Build a legacy pregeneration queue explicitly

```bash
r1999-generation-queue
```

This compatibility command is intentionally separate from `r1999-bootstrap`.
Use it only for existing extractor-owned generation workflows until their jobs
are importable by VNTTS.

The command reads the story index and includes every speakable line without
installed source audio. It groups records by canonical voice character and then
story order. Each record carries one explicit action:

- `generate`: the source definitively has no audio;
- `prefer_source_audio`: a configured source route exists but is not installed;
- `manual_review`: the source cue could not be resolved;
- `resolve_audio`: the story index was built without audio resolution.

Against the currently verified local installation, the expanded index contains
147,973 lines. This includes 18,844 schema-declared structured records from 32
event, activity, tutorial, battle, branch, room, mail, and side-mode tables. The
queue contains 97,893 non-installed lines: 84,079 ready to generate, 13,811
that prefer recoverable source audio, and 3 requiring review.

Every queue item includes deterministic emotion evidence, delivery controls,
and generic, Chatterbox, CosyVoice, and Fish Speech prompt adapters. These are
starting instructions for generation and remain reviewable rather than being
treated as ground truth.

Audit every story-like configuration table and see which explicit schema owns
its localized text:

```bash
r1999-source-audit
```

## Generate voices resumably

Provider adapters are ordinary local commands. They receive a JSON request and
must write a mono PCM16 WAV to the requested output path:

```bash
r1999-generate generate \
  --provider local \
  --model my-model \
  --provider-command 'python /path/to/adapter.py --request {request} --output {output}'
```

Generation is resumable and records provider, model, prompt, seed, attempts,
technical WAV quality, and failures after every item. New files remain outside
the runtime manifest until explicitly approved:

```bash
r1999-generate review '<queue-id>' approved
```

The published `vntts.generated-audio` manifest maps the stable line ID plus
current text SHA-256 to a verified local WAV. VNTTS uses it only on an exact
match and falls back to live TTS for stale, missing, rejected, or modified
audio.

Limit a queue to voiceless content introduced by patch 3.7: Chapter 13,
`On Another's Sorrow`, and Silverwing Eagle's `The Eaglet Takes Wing`:

```bash
r1999-generation-queue \
  --source-kind story \
  --source-kind hero_story_plot \
  --chapter-range 101301:101341 \
  --chapter-range 315401:315408 \
  --audio-status no_audio \
  --output /path/to/patch-3.7-voiceless-queue.jsonl
```

Generate the covered voices with one persistent MOSS process. The command is
resumable and skips queue characters that do not yet have a manifest reference,
as well as captions that contain only a nonverbal sound effect such as
`*chirp*` or `*bang*`:

```bash
../VisualNovelTextToSpeach/.venv/bin/python -m r1999extractor.moss_generation \
  --queue /path/to/patch-3.7-voiceless-queue.jsonl \
  --voice-manifest ../VisualNovelTextToSpeach/data/reverse1999-voices/manifest.json \
  --narrator-character Matilda \
  --output /path/to/patch-3.7-generated-audio
```

The VNTTS MOSS runtime must be installed first with
`uv sync --project ../VisualNovelTextToSpeach/backends/moss-tts`. Narration uses
the explicitly selected manifest character reference; it never falls back to
an unconfigured voice.

### Voice pregeneration UI

Open the desktop workbench to generate voices without manually building queues
or entering chapter ranges:

```bash
uv run r1999-pregenerate
```

The workbench discovers the newest local story index, groups main-story assets
into chapters, groups both anecdote formats into complete named stories, and
shows the number of voiceless lines before generation. Select any combination
of chapters and anecdotes and press **Generate selected stories**. Each launch
creates a separate resumable local job under the application-data directory.

The status panel remains usable while MOSS runs and shows generated, pending,
failed, missing-reference, and skipped sound-effect counts. Previous jobs can
be selected and resumed from the same window. Source paths and the narrator
voice are prefilled for the standard sibling VNTTS checkout. Narrator voice is
chosen from a searchable list of every referenced character in the selected
voice manifest; **Play reference** auditions the exact prompt clip MOSS will use
before generation. The selected character is stored in the resumable job, so
resuming that job preserves the same narrator choice. Previous jobs show their
saved narrator directly in the status table. Source paths remain editable for
other installations, and changing the manifest reloads the list.

Generation state includes the line, speaker, phase, attempt number, start time,
and previous retry error before each blocking provider call. The workbench uses
that state to keep a live elapsed timer visible during slow MOSS attempts and
shows the current line instead of relying on counters that may not change while
an already-failed line is retried. Progress reports processed outcomes together
with their generated and failed breakdown. Job status distinguishes generation
owned by the current window, a live external process, and an interrupted stale
job. Starting or resuming a job clears any exit code retained from its previous
run.

Pregeneration treats generated WAVs as reviewable artifacts rather than live
playback cache entries. Each attempt bypasses the VNTTS generated-speech cache,
renders typed PCM directly without opening an audio output stream, and uses
successive seeds for retries so a failed waveform is not reproduced. An attempt
is rejected before publication when MOSS is cancelled, reaches its text-derived
audio limit without EOS, has leading or trailing silence over 0.8 seconds, has
an internal silent span over 1.2 seconds, or has more than half silent audio
frames. Rejected attempts remain resumable and never replace an already reviewed
artifact.

Compare any set of local models on the same emotion-stratified sample:

```bash
r1999-benchmark --models /path/to/local-models.json
```

Create a blind same-text A/B listening session after generation, then reopen the
UI at any time to resume it:

```bash
r1999-listen start --benchmark /path/to/model-benchmark/benchmark-report.json
r1999-listen ui
r1999-listen status
r1999-listen report
```

Existing same-text per-model reports can be reused without regenerating audio:

```bash
r1999-listen start-reports --reports /path/to/model-a.json /path/to/model-b.json
r1999-listen ui
```

The workbench randomizes trial and A/B order, exposes only neutral audio aliases,
and saves progress after each simple choice: A, B, or no preference. It avoids
subjective numeric scales and specialist labels such as timbre or accent. The
model key is stored separately in `.blind-key.json`; the aggregate preference
report is unblinded and still requires an explicit human production-model
decision. Model configuration and generated samples stay local.

### Production model decision

On 2026-08-15, the project owner approved
`moss-tts-local-transformer-v1.5-mlx` as the production model after completing
all 45 blind preference trials. MOSS ranked first with 16 wins, no losses, and
one tie across 17 reviewed comparisons (97.06% preference rate). This explicit
decision satisfies the perceptual acceptance gate for scoped generation of
voiceless patch 3.7 main-story and Silverwing Eagle anecdote lines.

Configure VNTTS with the generated story index and the voice manifest produced by the import/review tools. Extracted artifacts are deliberately ignored by Git.

If you used the extraction tools while they were part of VNTTS, copy the local
indexes, review state, and Reverse: 1999 voice pack into this project's data
directory without deleting or overwriting the originals:

```bash
r1999-migrate-vntts-data --dry-run
r1999-migrate-vntts-data
```

## Existing voice workflow

```bash
r1999-batch scan
r1999-audition
```

Run `--help` on each command for paths and workflow options. The batch, catalog, Wwise, quality-scoring, and audition commands were moved intact from VNTTS so existing local voice-review work can continue here.

## Tests

```bash
uv run ruff format --check .
uv run ruff check .
uv run python -m unittest discover -s tests
uv run r1999-repository-guard
```

Without the `ui` extra, the three Qt audition tests are skipped while all core
tests still run. CI installs the extra and runs those UI tests in a separate job.
