import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import normalize_character_name

from r1999extractor.settings import get_local_data_directory
from r1999extractor.versioned_json import VersionedJSONCodec, VersionedJSONError

project_root = Path(__file__).resolve().parents[1]
default_catalog_path = get_local_data_directory() / "reverse1999" / "npc-catalog.json"
default_overlay_path = get_local_data_directory() / "reverse1999" / "npc-catalog-overlay.json"
sha256_pattern = re.compile(r"[0-9a-f]{64}")
npc_id_pattern = re.compile(r"(?:npc|role)(\d{4,})", re.IGNORECASE)
catalog_schema = "r1999.npc-catalog"
catalog_schema_version = 1
overlay_schema = "r1999.npc-catalog-overlay"
overlay_schema_version = 1
overlay_codec = VersionedJSONCodec(
    overlay_schema, overlay_schema_version, "NPC catalog overlay"
)


class Reverse1999CatalogError(RuntimeError):
    pass


normalize_name = normalize_character_name


@dataclass(frozen=True)
class ApprovedReference:
    bank: str
    media_id: int
    source_sha256: str
    reference: str
    reference_sha256: str


@dataclass(frozen=True)
class Reverse1999Npc:
    npc_id: str
    display_name: str
    aliases: tuple[str, ...]
    language: str
    game_versions: tuple[str, ...]
    banks: tuple[str, ...]
    approved_references: tuple[ApprovedReference, ...]


class Reverse1999NpcCatalog:
    def __init__(self, version, game, npcs):
        self.version = version
        self.game = game
        self.npcs = tuple(npcs)
        self._by_id = {npc.npc_id: npc for npc in self.npcs}
        self._by_name = {}
        for npc in self.npcs:
            for name in (npc.display_name, *npc.aliases):
                normalized = normalize_name(name)
                previous = self._by_name.get(normalized)
                if previous is not None and previous != npc:
                    raise Reverse1999CatalogError(
                        f"NPC name or alias {name!r} is used more than once"
                    )
                self._by_name[normalized] = npc

    def resolve(self, name):
        return self._by_name.get(normalize_name(name))

    def get(self, npc_id):
        return self._by_id.get(str(npc_id))

    def validate_reference_files(self, root=project_root / "data"):
        root = Path(root).expanduser().resolve()
        for npc in self.npcs:
            for approved in npc.approved_references:
                reference = root / approved.reference
                if not reference.is_file():
                    raise Reverse1999CatalogError(
                        f"Approved reference does not exist: {reference}"
                    )
                checksum = sha256_file(reference)
                if checksum != approved.reference_sha256:
                    raise Reverse1999CatalogError(
                        f"Approved reference checksum does not match: {reference}"
                    )
        return True

    @classmethod
    def load(cls, path=default_catalog_path):
        path = Path(path).expanduser().resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise Reverse1999CatalogError(
                f"NPC catalog does not exist: {path}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise Reverse1999CatalogError(
                f"Unable to read NPC catalog {path}: {error}"
            ) from error
        return cls.from_dict(document)

    @classmethod
    def from_dict(cls, document):
        if not isinstance(document, dict):
            raise Reverse1999CatalogError("NPC catalog must be a JSON object")
        version = document.get("version")
        game = document.get("game")
        entries = document.get("npcs")
        if not isinstance(version, int) or version <= 0:
            raise Reverse1999CatalogError("NPC catalog requires a positive version")
        if not isinstance(game, str) or not game.strip():
            raise Reverse1999CatalogError("NPC catalog requires a game name")
        if not isinstance(entries, list):
            raise Reverse1999CatalogError("NPC catalog requires an NPC list")

        npcs = [cls._parse_npc(entry, index) for index, entry in enumerate(entries)]
        npc_ids = [npc.npc_id for npc in npcs]
        if len(npc_ids) != len(set(npc_ids)):
            raise Reverse1999CatalogError("NPC IDs must be unique")
        return cls(version, game.strip(), npcs)

    @staticmethod
    def _parse_npc(entry, index):
        if not isinstance(entry, dict):
            raise Reverse1999CatalogError(f"NPC entry {index} must be an object")
        npc_id = entry.get("id")
        display_name = entry.get("display_name")
        aliases = entry.get("aliases", [])
        language = entry.get("language")
        game_versions = entry.get("game_versions")
        banks = entry.get("banks")
        references = entry.get("approved_references", [])
        if not isinstance(npc_id, str) or not npc_id.strip():
            raise Reverse1999CatalogError(f"NPC entry {index} requires an ID")
        if not isinstance(display_name, str) or not display_name.strip():
            raise Reverse1999CatalogError(f"NPC entry {index} requires a display name")
        for label, values in (
            ("aliases", aliases),
            ("game versions", game_versions),
            ("banks", banks),
        ):
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise Reverse1999CatalogError(
                    f"NPC entry {index} {label} must be non-empty strings"
                )
        if not isinstance(language, str) or not language.strip():
            raise Reverse1999CatalogError(f"NPC entry {index} requires a language")
        if not game_versions:
            raise Reverse1999CatalogError(f"NPC entry {index} requires a game version")
        if not banks:
            raise Reverse1999CatalogError(
                f"NPC entry {index} requires at least one bank"
            )
        if not isinstance(references, list):
            raise Reverse1999CatalogError(
                f"NPC entry {index} approved references must be a list"
            )
        approved_references = tuple(
            Reverse1999NpcCatalog._parse_reference(reference, index, banks)
            for reference in references
        )
        return Reverse1999Npc(
            npc_id=npc_id.strip(),
            display_name=display_name.strip(),
            aliases=tuple(alias.strip() for alias in aliases),
            language=language.strip(),
            game_versions=tuple(version.strip() for version in game_versions),
            banks=tuple(bank.strip() for bank in banks),
            approved_references=approved_references,
        )

    @staticmethod
    def _parse_reference(entry, npc_index, banks):
        if not isinstance(entry, dict):
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} approved reference must be an object"
            )
        bank = entry.get("bank")
        media_id = entry.get("media_id")
        source_sha256 = entry.get("source_sha256")
        reference = entry.get("reference")
        reference_sha256 = entry.get("reference_sha256")
        if bank not in banks:
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} reference bank is not in its bank list"
            )
        if not isinstance(media_id, int) or media_id <= 0:
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} reference requires a media ID"
            )
        for label, value in (
            ("source checksum", source_sha256),
            ("reference checksum", reference_sha256),
        ):
            if not isinstance(value, str) or not sha256_pattern.fullmatch(value):
                raise Reverse1999CatalogError(
                    f"NPC entry {npc_index} reference has an invalid {label}"
                )
        if not isinstance(reference, str) or not reference.strip():
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} reference requires a path"
            )
        return ApprovedReference(
            bank=bank,
            media_id=media_id,
            source_sha256=source_sha256,
            reference=reference.strip(),
            reference_sha256=reference_sha256,
        )


def _localized(language, key, fallback=""):
    value = language.get(key) if isinstance(key, str) else None
    if not isinstance(value, str) or not value.strip():
        value = fallback
    return value.strip() if isinstance(value, str) else ""


def _load_overlay(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        return overlay_codec.new(npcs=[])
    try:
        document = overlay_codec.load(path)
    except VersionedJSONError as error:
        raise Reverse1999CatalogError(str(error)) from error
    if not isinstance(document.get("npcs"), list):
        raise Reverse1999CatalogError("NPC catalog overlay requires an NPC list")
    return document


def build_catalog_document(language, tables, bank_index, *, overlay=None, game_version="installed"):
    """Build reproducible NPC metadata from installed configs and Wwise bank names."""
    names = {}
    for row in tables.get("json_character", []):
        if not isinstance(row, list) or len(row) < 2:
            continue
        npc_id = str(row[0]).strip()
        fallback = row[24] if len(row) > 24 and isinstance(row[24], str) else ""
        display_name = _localized(language, row[1], fallback)
        if npc_id and display_name:
            names[npc_id] = display_name

    evidence_layouts = (
        ("json_tip_dialog", 4, None),
        ("json_guide_step", 6, None),
        ("json_dialog_step", 5, 4),
        ("json_battle_dialog", 6, None),
        ("json_activity163_dialog", 5, 3),
        ("json_activity206_dialogue", 2, 3),
        ("json_sodache_dialog", 5, 4),
    )
    for table, id_index, name_index in evidence_layouts:
        for row in tables.get(table, []):
            if not isinstance(row, list) or len(row) <= id_index:
                continue
            npc_id = str(row[id_index]).strip()
            if not npc_id or npc_id == "0":
                continue
            display_name = ""
            if name_index is not None and len(row) > name_index:
                display_name = _localized(language, row[name_index])
            if display_name:
                names.setdefault(npc_id, display_name)

    banks_by_id = defaultdict(set)
    for bank in bank_index.get("banks", []):
        filename = bank.get("filename") if isinstance(bank, dict) else None
        if not isinstance(filename, str):
            continue
        for npc_id in npc_id_pattern.findall(filename):
            banks_by_id[npc_id].add(filename)
    for table, rows in tables.items():
        if not (table.startswith("json_story_audio") or table in {"json_role_audio", "json_story_role_audio"}):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            route = f"{row[1]} {row[2]}"
            for npc_id in npc_id_pattern.findall(route):
                bank = str(row[2]).strip()
                if bank:
                    filename = bank if bank.casefold().endswith(".bnk") else f"{bank}.bnk"
                    banks_by_id[npc_id].add(filename)

    overlay_document = overlay or {
        "schema": overlay_schema,
        "schema_version": overlay_schema_version,
        "npcs": [],
    }
    overlay_entries = {}
    for entry in overlay_document.get("npcs", []):
        if not isinstance(entry, dict) or not str(entry.get("id", "")).strip():
            raise Reverse1999CatalogError("NPC catalog overlay entries require an ID")
        overlay_entries[str(entry["id"]).strip()] = entry

    npc_ids = sorted(set(names) | set(banks_by_id) | set(overlay_entries))
    npcs = []
    for npc_id in npc_ids:
        override = overlay_entries.get(npc_id, {})
        references = override.get("approved_references", [])
        banks = set(banks_by_id.get(npc_id, ()))
        for reference in references if isinstance(references, list) else ():
            if isinstance(reference, dict) and isinstance(reference.get("bank"), str):
                banks.add(reference["bank"].strip())
        display_name = str(override.get("display_name") or names.get(npc_id) or f"NPC {npc_id}").strip()
        if not banks:
            continue
        npcs.append(
            {
                "id": npc_id,
                "display_name": display_name,
                "aliases": list(override.get("aliases", [])),
                "language": str(override.get("language") or "en"),
                "game_versions": [str(game_version)],
                "banks": sorted(banks),
                "approved_references": references,
            }
        )
    document = {
        "schema": catalog_schema,
        "schema_version": catalog_schema_version,
        "version": catalog_schema_version,
        "game": "Reverse: 1999",
        "npcs": npcs,
    }
    Reverse1999NpcCatalog.from_dict(document)
    return document


def migrate_catalog_to_overlay(document):
    catalog = Reverse1999NpcCatalog.from_dict(document)
    return {
        "schema": overlay_schema,
        "schema_version": overlay_schema_version,
        "npcs": [
            {
                "id": npc.npc_id,
                "display_name": npc.display_name,
                "aliases": list(npc.aliases),
                "language": npc.language,
                "approved_references": [
                    {
                        "bank": reference.bank,
                        "media_id": reference.media_id,
                        "source_sha256": reference.source_sha256,
                        "reference": reference.reference,
                        "reference_sha256": reference.reference_sha256,
                    }
                    for reference in npc.approved_references
                ],
            }
            for npc in catalog.npcs
        ],
    }


def write_catalog(document, path=default_catalog_path):
    path = Path(path).expanduser().resolve()
    atomic_write_json(path, document, sort_keys=True)
    return path


def create_parser():
    parser = argparse.ArgumentParser(
        description="Build, migrate, or validate the local Reverse: 1999 NPC catalog."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build the catalog from the installed game")
    build.add_argument("--config-directory", type=Path)
    build.add_argument("--bank-index", type=Path)
    build.add_argument("--overlay", type=Path, default=default_overlay_path)
    build.add_argument("--game-version", default="installed")
    build.add_argument("--output", type=Path, default=default_catalog_path)
    validate = subparsers.add_parser("validate", help="Validate catalog reference files")
    validate.add_argument("--catalog", type=Path, default=default_catalog_path)
    validate.add_argument(
        "--reference-root",
        type=Path,
        default=get_local_data_directory(),
        help="Directory used as the base for catalog reference paths.",
    )
    migrate = subparsers.add_parser(
        "migrate-overlay", help="Migrate an old combined catalog into a local review overlay"
    )
    migrate.add_argument("catalog", type=Path)
    migrate.add_argument("--output", type=Path, default=default_overlay_path)
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        if arguments.command == "migrate-overlay":
            document = json.loads(arguments.catalog.read_text(encoding="utf-8"))
            output = write_catalog(migrate_catalog_to_overlay(document), arguments.output)
            print(f"Migrated local review overlay to {output}")
            return 0
        if arguments.command == "build":
            from r1999extractor.reverse1999_config import (
                find_game_config_directory,
                load_config_directory,
            )
            from r1999extractor.reverse1999_index import default_output as default_bank_index

            config_directory = arguments.config_directory or find_game_config_directory()
            if config_directory is None:
                raise Reverse1999CatalogError(
                    "Unable to find installed game configs; pass --config-directory"
                )
            bank_index_path = arguments.bank_index or default_bank_index
            bank_index = json.loads(Path(bank_index_path).read_text(encoding="utf-8"))
            language, tables = load_config_directory(config_directory)
            document = build_catalog_document(
                language,
                tables,
                bank_index,
                overlay=_load_overlay(arguments.overlay),
                game_version=arguments.game_version,
            )
            output = write_catalog(document, arguments.output)
            print(f"Built {len(document['npcs'])} local NPC entries in {output}")
            return 0
        catalog = Reverse1999NpcCatalog.load(arguments.catalog)
        catalog.validate_reference_files(arguments.reference_root)
    except Reverse1999CatalogError as error:
        print(error, file=sys.stderr)
        return 1
    reference_count = sum(len(npc.approved_references) for npc in catalog.npcs)
    suffix = "" if reference_count == 1 else "s"
    print(f"Validated {reference_count} approved reference{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
