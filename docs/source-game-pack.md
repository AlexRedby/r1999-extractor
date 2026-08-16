# Source game-pack delivery

`r1999-source-pack` exports the extractor-owned boundary through the released
`vntts-artifacts` v0.6.1 `vntts.game-pack` schema version 1. The delivery is a
new, portable directory with this shape:

```text
game-pack.json
story-index.jsonl
voice/
  manifest.json
  references/*.wav
```

The exact reference paths come from the input voice manifest and may use nested
directories. They must be safe POSIX-relative paths, must name existing WAV
files, and remain relative to `voice/manifest.json`. The exporter never
overwrites an existing output directory. It stages and fully validates the
delivery before making that directory visible.

The shared `write_game_pack` API derives SHA-256 bindings for the story index,
voice manifest, and every referenced WAV. `load_game_pack` verifies those
checksums, rejects paths that leave the delivery directory, loads both component
contracts, and ensures that declared WAVs exactly match manifest references.
This makes the whole directory relocatable without changing the manifest.

Producer provenance is represented by:

- `game.id`: stable ID `reverse1999`
- `game.version`: exact value supplied with `--game-version`
- `producers[0].name`: `reverse1999-extractor`
- `producers[0].version`: installed extractor package version
- `created_at`: timezone-aware export timestamp

Source-audio availability, Wwise bank/media provenance, speaker mappings, and
collection metadata remain fields of the copied story index. The delivery does
not copy arbitrary original game audio. It copies only reviewed WAV references
declared by the voice manifest.

The extractor intentionally omits the optional `generated_audio` component.
Synthesis requests, generated speech, cache policy, and final authored packs
belong to VNTTS rather than the game extractor.

Validate a delivery and consume its lossless story authoring data with the
public shared API:

```python
from vntts_artifacts import StoryIndexDocument, voice_generation_action
from vntts_artifacts.game_pack import load_game_pack

pack = load_game_pack("/path/to/source-pack/game-pack.json")
assert pack.game_id == "reverse1999"
assert pack.generated_audio is None

story = StoryIndexDocument.load(pack.story_index.path)
for collection in story.collections:
    for record in story.records_for_collection(collection.collection_id, speakable_only=True):
        action = voice_generation_action(
            record.source_audio_status,
            unknown_action="resolve_audio",
        )
        # `None` means verified source audio is available and no synthesis item
        # should be created.
```

`StoryIndexDocument`, its lossless record/collection types, and canonical
source-audio queue policy were added in the immutable `vntts-artifacts` v0.6.1
release. The wire schemas remain version 1, so this is an additive shared API
upgrade rather than a source-pack format change.

The synthetic compatibility and regression suite additionally moves a completed
pack and reloads it, checks provenance and component omission, detects a mutated
WAV checksum, rejects unsafe reference paths, and verifies non-destructive
existing-output behavior.
