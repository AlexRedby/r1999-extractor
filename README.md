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

The JSONL file starts with one metadata record followed by line records. Each line has a stable ID, chapter, sequence, speaker, text, source information, and optional delivery hints.

The extractor also emits `generation-queue.jsonl` using
`vntts.voice-generation-queue` schema version 1. Queue items are pinned to a
stable line ID and text hash, so text changes cannot accidentally reuse stale
generated audio.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e '.[ui]'
```

On Windows, use `.venv\\Scripts\\python` and `.venv\\Scripts\\pip`.

## Extract all story text

For a clean installation, rebuild every required local artifact in dependency
order with one command:

```bash
r1999-bootstrap --game-version installed
```

This discovers the installed configs, story bundle, and English Wwise banks,
then creates the bank index, merged NPC catalog, story index, source audit, and
generation queue. An optional local `npc-catalog-overlay.json` preserves manual
name corrections and approved reference decisions without committing them.

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
automatically.

Every output line has one explicit audio status:

- `installed`: the configured event resolves to embedded or streamed local media;
- `no_audio`: the story has a blank or zero voice cue;
- `configured_unavailable`: a valid route exists but its bank, event, or media is
  not installed;
- `unresolved`: the story references an ID absent from the installed config.

Use `--include-non-speakable` for a preservation-oriented export containing
localized test, placeholder, and non-English records. Use
`--skip-audio-resolution` only when a text-only index is sufficient.

## Build the pregeneration queue

```bash
r1999-generation-queue
```

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

Compare any set of local models on the same emotion-stratified sample:

```bash
r1999-benchmark --models /path/to/local-models.json
```

The report collects technical success and provides a common manual scoring
rubric for emotion, voice consistency, naturalness, and pronunciation. Model
configuration and generated samples stay local.

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
python -m unittest discover -s tests
```
