# Legacy model-benchmark migration

VNTTS owns generic voice-model benchmarking through `vntts-benchmark-models`.
The extractor no longer installs `r1999-benchmark` or accepts its arbitrary
external `{provider, model, command}` adapter configuration.

The ownership audit found no external-command model configuration anywhere in
the configured extractor application-data tree. It also found no
`r1999.voice-model-benchmark` document, `benchmark-report.json`,
`benchmark-queue.jsonl`, or benchmark generation state. Removing the command
therefore does not strand a resumable benchmark or a provider configuration.

The existing model-benchmark directory contains historical ad hoc reports and
audio plus the completed blind-listening session. Those files are retained in
place as local evidence; removal of the extractor command does not rewrite,
import, or delete them. The completed session and its selected neutral audio
aliases use the separate checksum-preserving procedure in
[`legacy-listening-migration.md`](legacy-listening-migration.md).

New comparisons use a strict VNTTS model-variant document. Each entry names a
stable model ID and typed backend, with optional model and generation profile:

```json
[
  {
    "model_id": "moss/stable",
    "backend": "moss-tts",
    "model": "/path/to/local/model",
    "generation_profile": "stable"
  },
  {
    "model_id": "pocket/default",
    "backend": "pocket-tts",
    "generation_profile": "default"
  }
]
```

Run every variant over one exact shared queue sample:

```bash
cd /path/to/VisualNovelTextToSpeach
uv run vntts-benchmark-models \
  --queue /path/to/generation-queue.jsonl \
  --models /path/to/model-variants.json \
  --manifest /path/to/voice-manifest.json \
  --sample-size 24 \
  --seed 0 \
  --output /path/to/model-benchmark
```

VNTTS preserves stable sample, line, text-hash, voice, seed, profile, PCM, and
SHA-256 identity in its corpus and per-model reports. It renders through
`SynthesisRequest` with cache bypass and never opens a playback device. Limited
or cancelled renders fail instead of publishing partial benchmark audio.
Schema-less historical reports are not silently promoted into this strict
contract; they remain untouched local artifacts unless explicitly regenerated.
