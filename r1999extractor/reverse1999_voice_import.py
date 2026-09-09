import argparse
import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.text_utils import slugify
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    upsert_voice_manifest_entry,
    write_voice_manifest,
)

from r1999extractor.cli import cli_error
from r1999extractor.reverse1999_aliases import aliases_for_character
from r1999extractor.reverse1999_catalog import (
    Reverse1999CatalogError,
    Reverse1999NpcCatalog,
    default_catalog_path,
)
from r1999extractor.reverse1999_config import game_resource_roots
from r1999extractor.settings import get_local_data_directory
from r1999extractor.voice_reference_quality import trim_and_normalize_voice_reference
from r1999extractor.wwise import (
    AudioConversionError,
    EmbeddedMedia,
    WwiseBankError,
    convert_audio,
    inspect_bank,
    read_embedded_media,
    resolve_decoder,
)

project_root = Path(__file__).resolve().parents[1]
default_output = get_local_data_directory() / "voice-packs" / "reverse1999"
REFERENCE_DECODE_VERSION = 2


class GameVoiceImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportedReference:
    path: Path
    media_id: int
    source_sha256: str
    reference_sha256: str
    bank: str | None = None
    decoded_duration_seconds: float | None = None
    reference_duration_seconds: float | None = None


def is_scene_audio_bank(bank):
    """Return whether a bank is a scene-audio container, not a speaker bank."""
    stem = Path(bank).stem.casefold()
    return stem.startswith("activityvoc_story_") or stem.startswith("plotvoc_story_")


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Import clean Reverse: 1999 story voice clips from a locally "
            "installed game's Wwise bank into a VNTTS voice manifest."
        )
    )
    parser.add_argument("character", help="Speaker name shown in game dialogue.")
    parser.add_argument(
        "--bank",
        type=Path,
        help=(
            "Explicit English .bnk file. Required for characters not yet in "
            "the built-in story-bank map."
        ),
    )
    parser.add_argument(
        "--game-audio-directory",
        type=Path,
        help="Directory containing the installed game's English .bnk files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help="Existing or new VNTTS voice-pack directory.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=default_catalog_path,
        help="Versioned Reverse: 1999 NPC catalog.",
    )
    parser.add_argument(
        "--references",
        type=int,
        default=3,
        help="Maximum clean clips to import from the bank.",
    )
    parser.add_argument(
        "--media-id",
        type=int,
        action="append",
        dest="media_ids",
        help=(
            "Reviewed embedded media ID to import. Repeat for multiple clips; "
            "when omitted, the largest clips are selected."
        ),
    )
    parser.add_argument(
        "--decoder",
        default="vgmstream-cli",
        help="Path or command name for vgmstream-cli.",
    )
    return parser


def find_game_audio_directory(home=None, environment=None):
    for root in game_resource_roots(home, environment):
        for candidate in sorted((root / "audios").glob("*/en")):
            if candidate.is_dir() and any(candidate.glob("*.bnk")):
                return candidate.resolve()
    return None


def resolve_bank(
    character,
    bank=None,
    game_audio_directory=None,
    catalog_path=default_catalog_path,
):
    if bank is not None:
        bank = Path(bank).expanduser().resolve()
    else:
        try:
            npc = Reverse1999NpcCatalog.load(catalog_path).resolve(character)
        except Reverse1999CatalogError as error:
            raise GameVoiceImportError(str(error)) from error
        if npc is None:
            raise GameVoiceImportError(
                f"No cataloged story bank for {character!r}; pass --bank explicitly"
            )
        filename = npc.banks[0]
        game_audio_directory = game_audio_directory or find_game_audio_directory()
        if game_audio_directory is None:
            raise GameVoiceImportError(
                "Unable to find Reverse: 1999 game audio; pass --game-audio-directory"
            )
        # Imported here because the indexer also uses this module's discovery.
        from r1999extractor.reverse1999_index import discover_bank_files

        bank = next(
            (
                path
                for path in discover_bank_files(game_audio_directory)
                if path.name.casefold() == filename.casefold()
            ),
            Path(game_audio_directory).expanduser().resolve() / filename,
        )

    if not bank.is_file():
        raise GameVoiceImportError(f"Voice bank does not exist: {bank}")
    return bank


def decode_references(
    bank,
    output_directory,
    character,
    reference_count,
    decoder,
    *,
    media_ids=None,
):
    if reference_count <= 0:
        raise GameVoiceImportError("--references must be positive")
    if not media_ids and is_scene_audio_bank(bank):
        raise GameVoiceImportError(
            f"Scene-audio bank {Path(bank).name} may contain TV, radio, crowd, or "
            "unrelated voices; pass explicitly reviewed --media-id values"
        )
    media = read_full_bank_media(bank, media_ids)
    if media_ids:
        selected = media
    else:
        selected = sorted(media, key=lambda entry: entry.size, reverse=True)[:reference_count]
    if not selected:
        raise GameVoiceImportError(f"Voice bank contains no embedded media: {bank}")

    references_directory = output_directory / "references"
    references_directory.mkdir(parents=True, exist_ok=True)
    slug = slugify(character, fallback="character")
    decoded = []
    for index, item in enumerate(selected, start=1):
        output = references_directory / f"{slug}-game-{index:02d}.wav"
        decoded.append(
            decode_reference_data(
                item.data,
                output,
                item.media_id,
                decoder,
                bank=bank.name,
            )
        )
    return decoded


def read_full_bank_media(bank, media_ids=None):
    """Return complete WEM payloads, substituting external files for prefetches."""
    bank = Path(bank).expanduser().resolve()
    from r1999extractor.reverse1999_index import index_version
    from r1999extractor.story_audio import (
        AudioResolution,
        StoryAudioResolutionError,
        StoryAudioResolver,
    )

    try:
        summary = inspect_bank(bank)
        try:
            media = read_embedded_media(bank)
        except WwiseBankError:
            media = ()
        by_id = {entry.media_id: entry for entry in media}
        streamed_media_ids = {
            media_id for route in summary.event_routes for media_id in route.streamed_media_ids
        }
        routed_media_ids = {
            media_id for route in summary.event_routes for media_id in route.media_ids
        }
        available_ids = tuple(dict.fromkeys((*by_id, *routed_media_ids)))
        selected_ids = available_ids if media_ids is None else tuple(dict.fromkeys(media_ids))
        missing = [media_id for media_id in selected_ids if media_id not in available_ids]
        if missing:
            joined = ", ".join(str(media_id) for media_id in missing)
            raise GameVoiceImportError(
                f"Voice bank {bank.name} does not contain media ID(s): {joined}"
            )
        resolver = StoryAudioResolver(
            {},
            {
                "version": index_version,
                "game_audio_directory": str(bank.parent),
                "banks": [
                    {
                        "path": bank.name,
                        "filename": bank.name,
                        "embedded_media_ids": list(summary.media_ids),
                    }
                ],
            },
        )
        resolution = AudioResolution(
            "installed",
            "legacy_explicit_media",
            bank=bank.name,
            media_ids=selected_ids,
            available_media_ids=selected_ids,
            streamed_media_ids=tuple(
                media_id for media_id in selected_ids if media_id in streamed_media_ids
            ),
        )
    except (WwiseBankError, StoryAudioResolutionError) as error:
        raise GameVoiceImportError(f"Unable to inspect voice bank {bank.name}: {error}") from error

    full_media = []
    embedded_media = {entry.media_id: entry.data for entry in media}
    for media_id in selected_ids:
        try:
            payload = resolver.read_media(
                resolution,
                media_id,
                embedded_media=embedded_media,
            )
        except StoryAudioResolutionError as error:
            raise GameVoiceImportError(
                f"Unable to read full media {media_id} from {bank.name}: {error}"
            ) from error
        full_media.append(EmbeddedMedia(media_id, payload))
    return full_media


def decode_reference_data(data, output, media_id, decoder, *, bank=None, runner=None):
    """Decode one already-snapshotted Wwise media payload into a reference WAV."""
    if not isinstance(data, bytes) or not data:
        raise GameVoiceImportError(f"Media {media_id} contains no bytes")
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="r1999-game-voice-") as temporary_directory:
        temporary_directory = Path(temporary_directory)
        source = temporary_directory / f"{media_id}.wem"
        source.write_bytes(data)
        decoded_output = temporary_directory / f"{media_id}.wav"
        convert_audio(
            source,
            decoded_output,
            decoder=decoder,
            overwrite=True,
            **({"runner": runner} if runner is not None else {}),
        )
        with wave.open(str(decoded_output), "rb") as wav:
            decoded_duration = wav.getnframes() / wav.getframerate()
        trim_and_normalize_voice_reference(decoded_output, output)
        with wave.open(str(output), "rb") as wav:
            reference_duration = wav.getnframes() / wav.getframerate()
    return ImportedReference(
        path=output,
        media_id=media_id,
        source_sha256=hashlib.sha256(data).hexdigest(),
        reference_sha256=sha256_file(output),
        bank=bank,
        decoded_duration_seconds=decoded_duration,
        reference_duration_seconds=reference_duration,
    )


def update_manifest(output_directory, character, references, source_bank):
    manifest_path = output_directory / "manifest.json"
    if not manifest_path.is_file():
        manifest = {"version": 2, "reference_count": len(references), "voices": []}
    else:
        try:
            manifest, _entries = load_voice_manifest(manifest_path)
        except VoiceManifestError as error:
            raise GameVoiceImportError(f"Invalid voice manifest: {error}") from error
    reference_paths = [
        reference.path if isinstance(reference, ImportedReference) else Path(reference)
        for reference in references
    ]
    entry = {
        "character": character.strip(),
        "speaker": f"reverse-1999-{slugify(character, fallback='character')}-game-v1",
        "references": [
            reference.relative_to(output_directory).as_posix() for reference in reference_paths
        ],
        "aliases": list(aliases_for_character(character)),
        "sources": [
            f"local-game-bank:{name}"
            for name in sorted(
                {
                    reference.bank or source_bank.name
                    for reference in references
                    if isinstance(reference, ImportedReference)
                }
                or {source_bank.name}
            )
        ],
    }
    imported = [reference for reference in references if isinstance(reference, ImportedReference)]
    if imported:
        entry["reference_metadata"] = [
            {
                "bank": reference.bank or source_bank.name,
                "media_id": reference.media_id,
                "source_sha256": reference.source_sha256,
                "reference_sha256": reference.reference_sha256,
            }
            for reference in imported
        ]
    try:
        manifest = upsert_voice_manifest_entry(manifest, entry)
        write_voice_manifest(manifest_path, manifest)
    except VoiceManifestError as error:
        raise GameVoiceImportError(f"Invalid voice manifest: {error}") from error
    return manifest_path


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        bank = resolve_bank(
            arguments.character,
            arguments.bank,
            arguments.game_audio_directory,
            arguments.catalog,
        )
        decoder = resolve_decoder(arguments.decoder)
        output_directory = arguments.output.expanduser().resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        references = decode_references(
            bank,
            output_directory,
            arguments.character,
            arguments.references,
            decoder,
            media_ids=arguments.media_ids,
        )
        manifest = update_manifest(
            output_directory,
            arguments.character,
            references,
            bank,
        )
    except (GameVoiceImportError, WwiseBankError, AudioConversionError) as error:
        return cli_error(error)

    print(f"Imported {len(references)} clean references for {arguments.character} into {manifest}")
    return 0
