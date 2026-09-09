import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from vntts_artifacts.atomic_io import atomic_write_json

from r1999extractor.reverse1999_catalog import Reverse1999NpcCatalog, default_catalog_path
from r1999extractor.settings import get_local_data_directory

config_header_size = 48
config_key = b"@_#*&Reverse2806" + b" " * 16
config_iv = b"!_#@2022_Skyfly)"
index_version = 1
default_output = get_local_data_directory() / "reverse1999" / "dialogue-index.json"
_MAX_DISCOVERY_LOG_PATHS = 20


class Reverse1999ConfigError(RuntimeError):
    pass


def _log_paths(logger, label, paths):
    if logger is None or not logger.isEnabledFor(logging.INFO):
        return
    paths = tuple(paths)
    for path in paths[:_MAX_DISCOVERY_LOG_PATHS]:
        logger.info("%s: %s (exists=%s)", label, path, path.is_dir())
    if len(paths) > _MAX_DISCOVERY_LOG_PATHS:
        logger.info("%s: %d additional paths omitted", label, len(paths) - _MAX_DISCOVERY_LOG_PATHS)


@dataclass(frozen=True)
class CharacterIdentity:
    character_id: str
    display_name: str
    language_key: str


@dataclass(frozen=True)
class DialogueEvidence:
    speaker_id: str
    speaker_name: str | None
    chapter: str
    sequence: int
    language_key: str
    text: str
    source_table: str


def decrypt_config_data(data):
    if len(data) <= config_header_size:
        raise Reverse1999ConfigError("Encrypted config is missing its payload")
    ciphertext = data[config_header_size:]
    if len(ciphertext) % 16:
        raise Reverse1999ConfigError("Encrypted config payload is not aligned to an AES block")
    try:
        decryptor = Cipher(algorithms.AES(config_key), modes.CBC(config_iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as error:
        raise Reverse1999ConfigError(
            "Encrypted config has invalid padding or uses an unsupported format"
        ) from error


def load_encrypted_json(path):
    path = Path(path).expanduser().resolve()
    try:
        data = decrypt_config_data(path.read_bytes())
        return json.loads(data.decode("utf-8"))
    except FileNotFoundError as error:
        raise Reverse1999ConfigError(f"Config does not exist: {path}") from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Reverse1999ConfigError(f"Unable to read config {path}: {error}") from error


def parse_language_document(document):
    if not isinstance(document, list) or len(document) != 2 or not isinstance(document[1], list):
        raise Reverse1999ConfigError("Language config has an unsupported structure")
    language = {}
    for row in document[1]:
        if (
            isinstance(row, list)
            and len(row) >= 2
            and isinstance(row[0], str)
            and isinstance(row[1], str)
        ):
            language[row[0]] = row[1]
    return language


def parse_data_document(document):
    if not isinstance(document, dict):
        raise Reverse1999ConfigError("Data config must contain a JSON object")
    tables = {}
    for table_name, encoded_table in document.items():
        if not isinstance(table_name, str) or not isinstance(encoded_table, str):
            continue
        try:
            table = json.loads(encoded_table)
        except json.JSONDecodeError as error:
            raise Reverse1999ConfigError(
                f"Table {table_name} is not valid JSON: {error}"
            ) from error
        if not isinstance(table, list) or len(table) != 2 or not isinstance(table[1], list):
            # A few internal metadata entries use a different schema. They are
            # unrelated to dialogue and can safely be ignored.
            continue
        tables[table_name] = table[1]
    return tables


def packaged_macos_resource_roots(home=None):
    home = Path.home() if home is None else Path(home)
    return tuple(
        root
        for applications in (home / "Applications", Path("/Applications"))
        for root in (
            applications / "Reverse: 1999.app/Wrapper/Reverse1999.app/Data/Raw/iOS",
            applications / "Reverse: 1999.app/Data/Raw/iOS",
        )
        if root.is_dir()
    )


def _windows_registry_install_locations(logger=None):
    """Return Steam roots and official-game locations recorded by Windows."""
    if sys.platform != "win32":
        return (), ()
    try:
        import winreg
    except ImportError:
        return (), ()

    def value(key, name):
        try:
            candidate, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return candidate.strip() if isinstance(candidate, str) and candidate.strip() else None

    def location(candidate):
        path = Path(candidate)
        return path if path.is_absolute() else None

    def open_key(hive, path):
        try:
            return winreg.OpenKey(hive, path)
        except OSError:
            return None

    steam_roots = []
    for hive, path in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
    ):
        key = open_key(hive, path)
        if key is None:
            continue
        with key:
            for name in ("SteamPath", "InstallPath"):
                if candidate := value(key, name):
                    if path := location(candidate):
                        steam_roots.append(path)

    game_locations = []
    uninstall_paths = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for path in uninstall_paths:
            parent = open_key(hive, path)
            if parent is None:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        subkey = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    key = open_key(hive, f"{path}\\{subkey}")
                    if key is None:
                        continue
                    with key:
                        display_name = value(key, "DisplayName")
                        if (
                            not display_name
                            or "".join(
                                character
                                for character in display_name.casefold()
                                if character.isalnum()
                            )
                            != "reverse1999"
                        ):
                            continue
                        if candidate := value(key, "InstallLocation"):
                            if candidate := location(candidate):
                                game_locations.append(candidate)
                        if candidate := value(key, "DisplayIcon"):
                            icon_path, separator, icon_index = candidate.rpartition(",")
                            if separator and icon_index.strip().lstrip("-").isdigit():
                                candidate = icon_path
                            if candidate := location(candidate.strip(' "')):
                                game_locations.append(candidate.parent)

    if logger is not None and logger.isEnabledFor(logging.INFO):
        _log_paths(logger, "Windows registry Steam root", steam_roots)
        _log_paths(logger, "Windows registry game location", game_locations)
    return tuple(steam_roots), tuple(game_locations)


def game_resource_roots(home=None, environment=None, *, logger=None, include_missing=False):
    """Return known installed platform roots without scanning whole drives."""
    home = Path.home() if home is None else Path(home)
    environment = os.environ if environment is None else environment
    candidates = list((home / "Library" / "Containers").glob("*/Data/Documents/ResLib/iOS"))
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(Path(local_app_data).glob("**/ResLib/*"))
    candidates.extend((home / "AppData" / "LocalLow").glob("**/ResLib/*"))

    program_files = tuple(
        Path(value)
        for name in ("ProgramFiles(x86)", "PROGRAMFILES(X86)", "ProgramFiles")
        if (value := environment.get(name))
    )
    streaming_roots = [
        root / "reverse1999_global" / "Reverse1999en" / "reverse1999_Data" / "StreamingAssets"
        for root in program_files
    ]
    steam_roots = [root / "Steam" for root in program_files]
    if environment.get("STEAM_PATH"):
        steam_roots.insert(0, Path(environment["STEAM_PATH"]))
    registry_steam_roots, registry_game_locations = _windows_registry_install_locations(logger)
    steam_roots.extend(registry_steam_roots)
    streaming_roots.extend(
        root / "reverse1999_Data" / "StreamingAssets"
        for location in registry_game_locations
        for root in (location, location / "Reverse1999en")
    )
    for steam_root in steam_roots:
        if logger is not None and logger.isEnabledFor(logging.INFO):
            logger.info("Steam root probe: %s (exists=%s)", steam_root, steam_root.is_dir())
        libraries = [steam_root]
        library_folders = steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            library_text = library_folders.read_text(encoding="utf-8")
        except OSError as error:
            if logger is not None and logger.isEnabledFor(logging.INFO):
                logger.info(
                    "Steam library list unavailable: %s (%s)", library_folders, type(error).__name__
                )
        else:
            discovered_libraries = tuple(
                Path(value.replace("\\\\", "\\"))
                for value in re.findall(r'"path"\s+"([^"]+)"', library_text)
            )
            libraries.extend(discovered_libraries)
            if logger is not None and logger.isEnabledFor(logging.INFO):
                logger.info(
                    "Steam library list read: %s (parsed_libraries=%d)",
                    library_folders,
                    len(discovered_libraries),
                )
        for index, library in enumerate(libraries):
            manifest = library / "steamapps" / "appmanifest_3092660.acf"
            log_manifest = (
                logger is not None
                and logger.isEnabledFor(logging.INFO)
                and index < _MAX_DISCOVERY_LOG_PATHS
            )
            if log_manifest:
                logger.info("Steam manifest probe: %s (exists=%s)", manifest, manifest.is_file())
            try:
                manifest_text = manifest.read_text(encoding="utf-8")
            except OSError as error:
                if log_manifest:
                    logger.info(
                        "Steam manifest unavailable: %s (%s)", manifest, type(error).__name__
                    )
                continue
            match = re.search(r'"installdir"\s+"([^"]+)"', manifest_text)
            if match:
                if logger is not None and logger.isEnabledFor(logging.INFO):
                    logger.info("Steam manifest recognized: %s", manifest)
                streaming_roots.append(
                    library
                    / "steamapps"
                    / "common"
                    / match.group(1)
                    / "reverse1999_Data"
                    / "StreamingAssets"
                )
        if (
            logger is not None
            and logger.isEnabledFor(logging.INFO)
            and len(libraries) > _MAX_DISCOVERY_LOG_PATHS
        ):
            logger.info(
                "Steam manifest probes: %d additional libraries omitted",
                len(libraries) - _MAX_DISCOVERY_LOG_PATHS,
            )
    for streaming_root in streaming_roots:
        candidates.extend((streaming_root / "PersistentRoot", streaming_root / "Windows"))
    candidates.extend(packaged_macos_resource_roots(home))
    _log_paths(logger, "Resource root probe", candidates)

    unique = []
    seen = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate not in seen and (include_missing or candidate.is_dir()):
            seen.add(candidate)
            unique.append(candidate)
    return tuple(unique)


def find_game_config_directory(home=None, environment=None, *, logger=None):
    roots = game_resource_roots(home, environment, logger=logger, include_missing=True)
    for index, root in enumerate(roots):
        candidate = root / "configs"
        data_config = candidate / "datacfg_1.dat"
        language_config = candidate / "language" / "json_language_en.json.dat"
        data_exists = data_config.is_file()
        language_exists = language_config.is_file()
        if (
            logger is not None
            and logger.isEnabledFor(logging.INFO)
            and index < _MAX_DISCOVERY_LOG_PATHS
        ):
            logger.info(
                "Config probe: %s (datacfg_1.dat=%s, json_language_en.json.dat=%s)",
                candidate,
                data_exists,
                language_exists,
            )
        if data_exists and language_exists:
            if logger is not None and logger.isEnabledFor(logging.INFO):
                logger.info("Selected config directory: %s (required files found)", candidate)
            return candidate.resolve()
    if logger is not None and logger.isEnabledFor(logging.INFO):
        if len(roots) > _MAX_DISCOVERY_LOG_PATHS:
            logger.info(
                "Config probes: %d additional candidate roots omitted",
                len(roots) - _MAX_DISCOVERY_LOG_PATHS,
            )
        logger.info(
            "No usable config directory: checked %d roots; requires datacfg_1.dat and "
            "language/json_language_en.json.dat",
            len(roots),
        )
    return None


def load_config_directory(path):
    root = Path(path).expanduser().resolve()
    language_path = root / "language" / "json_language_en.json.dat"
    language = parse_language_document(load_encrypted_json(language_path))
    tables = {}
    data_paths = sorted(root.glob("datacfg_*.dat"))
    if not data_paths:
        raise Reverse1999ConfigError(f"No datacfg files found in {root}")
    for data_path in data_paths:
        for name, rows in parse_data_document(load_encrypted_json(data_path)).items():
            if name in tables:
                raise Reverse1999ConfigError(f"Duplicate config table: {name}")
            tables[name] = rows
    return language, tables


def extract_character_identities(language, tables):
    identities = {}
    for row in tables.get("json_character", []):
        if not isinstance(row, list) or len(row) < 2:
            continue
        character_id = str(row[0])
        language_key = row[1] if isinstance(row[1], str) else ""
        fallback = row[24] if len(row) > 24 and isinstance(row[24], str) else ""
        display_name = language.get(language_key) or fallback
        if display_name:
            identities[character_id] = CharacterIdentity(character_id, display_name, language_key)
    return identities


def resolve_speaker_name(speaker_id, identities, catalog=None):
    speaker_id = str(speaker_id)
    identity = identities.get(speaker_id)
    if identity is not None:
        return identity.display_name
    matching_ids = [
        character_id
        for character_id in identities
        if speaker_id.startswith(character_id) and len(speaker_id) - len(character_id) <= 2
    ]
    if matching_ids:
        return identities[max(matching_ids, key=len)].display_name
    npc = catalog.get(speaker_id) if catalog is not None else None
    return npc.display_name if npc is not None else None


def extract_dialogue_evidence(language, tables, *, catalog=None):
    identities = extract_character_identities(language, tables)
    evidence = []
    table_layouts = (
        ("json_tip_dialog", 0, 1, 4, 5, None),
        ("json_guide_step", 0, 1, 6, 9, None),
        ("json_dialog_step", 0, 1, 5, 3, 4),
        ("json_battle_dialog", 0, 1, 6, 8, None),
    )
    for (
        table_name,
        chapter_index,
        sequence_index,
        speaker_index,
        key_index,
        speaker_name_key_index,
    ) in table_layouts:
        for row in tables.get(table_name, []):
            if not isinstance(row, list) or len(row) <= max(speaker_index, key_index):
                continue
            speaker_id = str(row[speaker_index]).strip()
            language_key = row[key_index]
            if not speaker_id or not isinstance(language_key, str) or not language_key:
                continue
            try:
                sequence = int(row[sequence_index])
            except (TypeError, ValueError):
                sequence = 0
            direct_name = None
            if speaker_name_key_index is not None and len(row) > speaker_name_key_index:
                direct_name_key = row[speaker_name_key_index]
                if isinstance(direct_name_key, str):
                    direct_name = language.get(direct_name_key)
            evidence.append(
                DialogueEvidence(
                    speaker_id=speaker_id,
                    speaker_name=direct_name
                    or resolve_speaker_name(speaker_id, identities, catalog=catalog),
                    chapter=str(row[chapter_index]),
                    sequence=sequence,
                    language_key=language_key,
                    text=language.get(language_key, ""),
                    source_table=table_name,
                )
            )
    evidence.sort(
        key=lambda item: (
            item.chapter,
            item.sequence,
            item.source_table,
            item.speaker_id,
        )
    )
    return identities, evidence


def build_dialogue_index(config_directory, *, catalog_path=default_catalog_path):
    language, tables = load_config_directory(config_directory)
    catalog = Reverse1999NpcCatalog.load(catalog_path)
    identities, evidence = extract_dialogue_evidence(language, tables, catalog=catalog)
    return {
        "version": index_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_directory": str(Path(config_directory).expanduser().resolve()),
        "characters": [asdict(item) for item in identities.values()],
        "dialogue": [asdict(item) for item in evidence],
        "resolved_count": sum(item.speaker_name is not None for item in evidence),
        "unresolved_count": sum(item.speaker_name is None for item in evidence),
    }


def write_dialogue_index(index, output=default_output):
    output = Path(output).expanduser().resolve()
    atomic_write_json(output, index)
    return output


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Decrypt locally installed Reverse: 1999 dialogue configs and build "
            "a chapter-aware speaker evidence index."
        )
    )
    parser.add_argument("--config-directory", type=Path)
    parser.add_argument("--catalog", type=Path, default=default_catalog_path)
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    config_directory = arguments.config_directory or find_game_config_directory()
    if config_directory is None:
        print(
            "Unable to find Reverse: 1999 configs; pass --config-directory",
            file=sys.stderr,
        )
        return 1
    try:
        index = build_dialogue_index(config_directory, catalog_path=arguments.catalog)
        output = write_dialogue_index(index, arguments.output)
    except Reverse1999ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    print(
        f"Indexed {len(index['dialogue'])} dialogue rows "
        f"({index['resolved_count']} resolved, "
        f"{index['unresolved_count']} unresolved) into {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
