import json
import sys
import unittest
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from r1999extractor.reverse1999_catalog import Reverse1999NpcCatalog
from r1999extractor.reverse1999_config import (
    Reverse1999ConfigError,
    _windows_registry_install_locations,
    config_header_size,
    config_iv,
    config_key,
    decrypt_config_data,
    extract_dialogue_evidence,
    find_game_config_directory,
    game_resource_roots,
    load_config_directory,
    parse_data_document,
    parse_language_document,
)
from r1999extractor.reverse1999_voice_import import find_game_audio_directory
from r1999extractor.story_index import find_game_resource_root, story_bundle_filename


def encrypt_config(document):
    plaintext = json.dumps(document).encode()
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(config_key), modes.CBC(config_iv)).encryptor()
    return b"header".ljust(config_header_size, b"-") + (
        encryptor.update(padded) + encryptor.finalize()
    )


def write_platform_resources(root):
    configs = root / "configs"
    (configs / "language").mkdir(parents=True)
    (configs / "datacfg_1.dat").touch()
    (configs / "language" / "json_language_en.json.dat").touch()
    bundles = root / "bundles"
    bundles.mkdir()
    (bundles / story_bundle_filename).touch()
    audio = root / "audios" / "Windows" / "en"
    audio.mkdir(parents=True)
    (audio / "story_voice.bnk").touch()
    return configs, audio


class FakeWindowsRegistry:
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def __init__(self, values):
        self.values = values

    def OpenKey(self, hive, path):
        key = (hive, path)
        if key not in self.values:
            raise OSError("missing")
        return self.Key(key)

    def QueryValueEx(self, key, name):
        try:
            return self.values[key.path][name], 1
        except KeyError as error:
            raise OSError("missing") from error

    def EnumKey(self, key, index):
        hive, path = key.path
        prefix = f"{path}\\"
        children = sorted(
            candidate_path[len(prefix) :].split("\\", 1)[0]
            for candidate_hive, candidate_path in self.values
            if candidate_hive == hive and candidate_path.startswith(prefix)
        )
        children = tuple(dict.fromkeys(children))
        try:
            return children[index]
        except IndexError as error:
            raise OSError("done") from error


class Reverse1999ConfigTest(unittest.TestCase):
    def setUp(self):
        registry = patch(
            "r1999extractor.reverse1999_config._windows_registry_install_locations",
            return_value=((), ()),
        )
        registry.start()
        self.addCleanup(registry.stop)
        self.registry = registry

    def test_finds_official_windows_client_resources(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            program_files = root / "Program Files (x86)"
            platform = (
                program_files
                / "reverse1999_global"
                / "Reverse1999en"
                / "reverse1999_Data"
                / "StreamingAssets"
                / "PersistentRoot"
            )
            configs, audio = write_platform_resources(platform)
            environment = {"ProgramFiles(x86)": str(program_files)}
            home = root / "Users" / "player"

            self.assertEqual(find_game_config_directory(home, environment), configs.resolve())
            self.assertEqual(find_game_resource_root(home, environment), platform.resolve())
            self.assertEqual(find_game_audio_directory(home, environment), audio.resolve())

    def test_finds_custom_steam_library_resources(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            steam = root / "Steam"
            library = root / "Games"
            steamapps = steam / "steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "libraryfolders.vdf").write_text(
                f'"libraryfolders" {{ "1" {{ "path" "{library}" }} }}',
                encoding="utf-8",
            )
            library_steamapps = library / "steamapps"
            library_steamapps.mkdir(parents=True)
            (library_steamapps / "appmanifest_3092660.acf").write_text(
                '"AppState" { "installdir" "Reverse 1999" }',
                encoding="utf-8",
            )
            platform = (
                library_steamapps
                / "common"
                / "Reverse 1999"
                / "reverse1999_Data"
                / "StreamingAssets"
                / "Windows"
            )
            configs, audio = write_platform_resources(platform)
            environment = {"STEAM_PATH": str(steam)}
            home = root / "Users" / "player"

            self.assertEqual(find_game_config_directory(home, environment), configs.resolve())
            self.assertEqual(find_game_resource_root(home, environment), platform.resolve())
            self.assertEqual(find_game_audio_directory(home, environment), audio.resolve())

    def test_finds_windows_registry_steam_and_official_client_resources(self):
        self.registry.stop()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            steam = root / "CustomSteam"
            steamapps = steam / "steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "appmanifest_3092660.acf").write_text(
                '"AppState" { "installdir" "Reverse 1999" }', encoding="utf-8"
            )
            steam_platform = (
                steamapps
                / "common"
                / "Reverse 1999"
                / "reverse1999_Data"
                / "StreamingAssets"
                / "Windows"
            )
            write_platform_resources(steam_platform)
            launcher = root / "Launcher"
            launcher_platform = (
                launcher / "Reverse1999en" / "reverse1999_Data" / "StreamingAssets" / "Windows"
            )
            launcher_configs, _ = write_platform_resources(launcher_platform)
            official = root / "Official, Client"
            official_platform = official / "reverse1999_Data" / "StreamingAssets" / "Windows"
            write_platform_resources(official_platform)
            uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
            registry = FakeWindowsRegistry(
                {
                    ("HKCU", r"Software\Valve\Steam"): {"SteamPath": str(steam)},
                    ("HKCU", uninstall): {},
                    ("HKCU", uninstall + r"\Reverse1999"): {
                        "DisplayName": "reverse 1999",
                        "InstallLocation": str(launcher),
                        "DisplayIcon": f'"{official / "Reverse, 1999.exe"}",0',
                    },
                    ("HKCU", uninstall + r"\Stale"): {
                        "DisplayName": "Reverse: 1999",
                        "InstallLocation": str(root / "stale"),
                    },
                    ("HKCU", uninstall + r"\Broken"): {
                        "DisplayName": "Reverse: 1999",
                        "InstallLocation": "relative",
                    },
                    ("HKCU", uninstall + r"\OtherGame"): {
                        "DisplayName": "Some Other Game",
                        "InstallLocation": str(root / "unrelated"),
                    },
                }
            )
            home = root / "Users" / "player"
            with (
                patch.object(sys, "platform", "win32"),
                patch.dict(sys.modules, {"winreg": registry}),
                patch(
                    "r1999extractor.reverse1999_config.packaged_macos_resource_roots",
                    return_value=(),
                ),
            ):
                roots = game_resource_roots(home, {})
                found = find_game_config_directory(home, {})

            self.assertIn(steam_platform.resolve(), roots)
            self.assertIn(launcher_platform.resolve(), roots)
            self.assertIn(official_platform.resolve(), roots)
            self.assertNotIn(
                (root / "stale" / "reverse1999_Data" / "StreamingAssets").resolve(), roots
            )
            self.assertEqual(found, launcher_configs.resolve())

    def test_reads_custom_drive_steam_registry_path(self):
        self.registry.stop()
        registry = FakeWindowsRegistry(
            {
                ("HKCU", r"Software\Valve\Steam"): {
                    "SteamPath": r"D:\\Games\\Steam",
                    "InstallPath": "relative",
                }
            }
        )
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"winreg": registry}),
            patch("r1999extractor.reverse1999_config.Path", PureWindowsPath),
        ):
            steam_roots, game_locations = _windows_registry_install_locations()

        self.assertEqual(steam_roots, (PureWindowsPath(r"D:\Games\Steam"),))
        self.assertEqual(game_locations, ())

    def test_windows_registry_discovery_is_safe_off_windows(self):
        self.registry.stop()
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(sys, "platform", "darwin"),
                patch(
                    "r1999extractor.reverse1999_config.packaged_macos_resource_roots",
                    return_value=(),
                ),
            ):
                self.assertEqual(game_resource_roots(root, {}), ())

    def test_decrypts_config_after_authenticated_header(self):
        document = {"hello": "world"}
        encrypted = encrypt_config(document)

        decrypted = json.loads(decrypt_config_data(encrypted))

        self.assertEqual(decrypted, document)

    def test_rejects_truncated_and_misaligned_configs(self):
        with self.assertRaisesRegex(Reverse1999ConfigError, "missing its payload"):
            decrypt_config_data(b"short")
        with self.assertRaisesRegex(Reverse1999ConfigError, "not aligned"):
            decrypt_config_data(b"x" * (config_header_size + 1))

    def test_parses_language_and_nested_data_tables(self):
        language = parse_language_document(["language_en", [["name", "Fatutu"], ["line", "Hello"]]])
        tables = parse_data_document({"json_tip_dialog": json.dumps(["json_tip_dialog", [[1, 2]]])})

        self.assertEqual(language, {"name": "Fatutu", "line": "Hello"})
        self.assertEqual(tables, {"json_tip_dialog": [[1, 2]]})

    def test_loads_split_encrypted_config_directory(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "language").mkdir()
            (root / "language" / "json_language_en.json.dat").write_bytes(
                encrypt_config(["language_en", [["line", "Hello"]]])
            )
            (root / "datacfg_1.dat").write_bytes(
                encrypt_config({"json_tip_dialog": json.dumps(["json_tip_dialog", []])})
            )

            language, tables = load_config_directory(root)

        self.assertEqual(language, {"line": "Hello"})
        self.assertEqual(tables, {"json_tip_dialog": []})

    def test_extracts_chapter_dialogue_and_resolves_character_and_npc_ids(self):
        catalog = Reverse1999NpcCatalog.from_dict(
            {
                "version": 1,
                "game": "Reverse: 1999",
                "npcs": [
                    {
                        "id": "520301",
                        "display_name": "Kamuta",
                        "aliases": [],
                        "language": "en",
                        "game_versions": ["3.6.5"],
                        "banks": ["kamuta.bnk"],
                    }
                ],
            }
        )
        language = {
            "fatutu_name": "Fatutu",
            "fatutu_line": "Take this.",
            "kamuta_line": "Paddle out.",
            "unknown_line": "Selone!",
        }
        tables = {
            "json_character": [
                [3109, "fatutu_name", *([""] * 22), "Fatutu"],
            ],
            "json_tip_dialog": [
                [24006, 3, "talk", "300#236", "310918", "fatutu_line", 0],
                [24007, 3, "talk", "300#236", "520301", "kamuta_line", 0],
                [24008, 1, "talk", "300#236", "999999", "unknown_line", 0],
            ],
            "json_guide_step": [
                [24401, 6, "talk", 0, 0, "235#236", "520301", 0, "", "kamuta_line"]
            ],
            "json_dialog_step": [[30, 30001, 1, "unknown_line", "selone_name", "521001", 1]],
        }
        language["selone_name"] = "Selone"

        identities, evidence = extract_dialogue_evidence(language, tables, catalog=catalog)

        self.assertEqual(identities["3109"].display_name, "Fatutu")
        self.assertEqual(
            [(item.speaker_id, item.speaker_name) for item in evidence],
            [
                ("310918", "Fatutu"),
                ("520301", "Kamuta"),
                ("999999", None),
                ("520301", "Kamuta"),
                ("521001", "Selone"),
            ],
        )
        self.assertEqual(evidence[0].chapter, "24006")

    def test_finds_macos_config_directory(self):
        with TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            root = (
                home
                / "Library"
                / "Containers"
                / "game"
                / "Data"
                / "Documents"
                / "ResLib"
                / "iOS"
                / "configs"
            )
            (root / "language").mkdir(parents=True)
            (root / "datacfg_1.dat").touch()
            (root / "language" / "json_language_en.json.dat").touch()

            found = find_game_config_directory(home=home, environment={})

        self.assertEqual(found, root.resolve())


if __name__ == "__main__":
    unittest.main()
