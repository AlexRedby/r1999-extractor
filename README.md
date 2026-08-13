# Reverse: 1999 Extractor

Local extraction and review tools for creating generic story and voice-pack artifacts from an installed copy of Reverse: 1999. This repository contains game-specific discovery, encrypted configuration parsing, Unity story parsing, Wwise indexing, NPC identity mapping, reference selection, and audition tooling.

It does not include game text, audio, decrypted configuration, or generated voice packs. Keep those local and use them only where you have the right to do so.

## Boundary with VNTTS

This project knows the Reverse: 1999 formats. VNTTS does not. The integration boundary is versioned local artifacts:

- `story-index.jsonl`: `vntts.story-index` schema version 1
- `manifest.json`: the existing generic VNTTS character voice manifest

The JSONL file starts with one metadata record followed by line records. Each line has a stable ID, chapter, sequence, speaker, text, source information, and optional delivery hints.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e '.[ui]'
```

On Windows, use `.venv\\Scripts\\python` and `.venv\\Scripts\\pip`.

## Extract all story text

Automatic discovery supports the macOS/iOS-container installation layout and common Windows `ResLib` layouts:

```bash
r1999-story-index
```

Or provide the installed resource directory explicitly:

```bash
r1999-story-index --resource-root /path/to/ResLib/iOS --output ./output/story-index.jsonl
```

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
