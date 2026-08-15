import hashlib
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.reverse1999_catalog import (
    Reverse1999CatalogError,
    Reverse1999NpcCatalog,
    build_catalog_document,
    main,
    migrate_catalog_to_overlay,
)


def catalog_document(**npc_overrides):
    npc = {
        "id": "520301",
        "display_name": "Kamuta",
        "aliases": ["Village Chief"],
        "language": "en",
        "game_versions": ["3.6.5"],
        "banks": ["kamuta.bnk"],
        "approved_references": [
            {
                "bank": "kamuta.bnk",
                "media_id": 123,
                "source_sha256": "a" * 64,
                "reference": "references/kamuta.wav",
                "reference_sha256": "b" * 64,
            }
        ],
    }
    npc.update(npc_overrides)
    return {"version": 1, "game": "Reverse: 1999", "npcs": [npc]}


class Reverse1999NpcCatalogTest(unittest.TestCase):
    def test_resolves_display_name_alias_and_internal_id(self):
        catalog = Reverse1999NpcCatalog.from_dict(catalog_document())

        self.assertEqual(catalog.resolve(" kamuta ").npc_id, "520301")
        self.assertEqual(catalog.resolve("Village-Chief").npc_id, "520301")
        self.assertEqual(catalog.get(520301).display_name, "Kamuta")
        self.assertEqual(catalog.get("520301").banks, ("kamuta.bnk",))

    def test_rejects_invalid_reference_metadata(self):
        invalid = catalog_document(
            approved_references=[
                {
                    "bank": "other.bnk",
                    "media_id": 0,
                    "source_sha256": "bad",
                    "reference": "",
                    "reference_sha256": "bad",
                }
            ]
        )

        with self.assertRaisesRegex(Reverse1999CatalogError, "reference bank"):
            Reverse1999NpcCatalog.from_dict(invalid)

    def test_loads_and_validates_reference_checksum(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            references = root / "references"
            references.mkdir()
            reference = references / "kamuta.wav"
            reference.write_bytes(b"voice")
            document = catalog_document()
            document["npcs"][0]["approved_references"][0]["reference_sha256"] = hashlib.sha256(
                b"voice"
            ).hexdigest()
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(document), encoding="utf-8")

            catalog = Reverse1999NpcCatalog.load(catalog_path)

            self.assertTrue(catalog.validate_reference_files(root))

    def test_builds_catalog_from_installed_metadata_and_local_overlay(self):
        language = {"test_name": "Test Character"}
        character = ["1001", "test_name"] + [""] * 23
        overlay = {
            "schema": "r1999.npc-catalog-overlay",
            "schema_version": 1,
            "npcs": [
                {
                    "id": "1001",
                    "display_name": "Corrected Test Character",
                    "aliases": ["Test Alias"],
                    "approved_references": [],
                }
            ],
        }
        document = build_catalog_document(
            language,
            {"json_character": [character]},
            {"banks": [{"filename": "voice_npc1001.bnk"}]},
            overlay=overlay,
            game_version="test",
        )
        catalog = Reverse1999NpcCatalog.from_dict(document)

        self.assertEqual(catalog.get("1001").display_name, "Corrected Test Character")
        self.assertEqual(catalog.resolve("Test Alias").npc_id, "1001")
        self.assertEqual(catalog.get("1001").banks, ("voice_npc1001.bnk",))

    def test_migrates_combined_catalog_to_review_only_overlay(self):
        overlay = migrate_catalog_to_overlay(catalog_document())

        self.assertEqual(overlay["schema"], "r1999.npc-catalog-overlay")
        self.assertNotIn("banks", overlay["npcs"][0])
        self.assertEqual(overlay["npcs"][0]["aliases"], ["Village Chief"])
        self.assertEqual(len(overlay["npcs"][0]["approved_references"]), 1)

    def test_validation_command_checks_locally_provisioned_references(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            references = root / "references"
            references.mkdir()
            reference = references / "kamuta.wav"
            reference.write_bytes(b"voice")
            document = catalog_document()
            document["npcs"][0]["approved_references"][0]["reference_sha256"] = hashlib.sha256(
                b"voice"
            ).hexdigest()
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(document), encoding="utf-8")
            output = StringIO()

            with redirect_stdout(output):
                result = main(
                    [
                        "validate",
                        "--catalog",
                        str(catalog_path),
                        "--reference-root",
                        str(root),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertIn("Validated 1 approved reference", output.getvalue())

    def test_validation_command_reports_missing_local_reference(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(catalog_document()), encoding="utf-8")
            error = StringIO()

            with redirect_stderr(error):
                result = main(
                    [
                        "validate",
                        "--catalog",
                        str(catalog_path),
                        "--reference-root",
                        str(root),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("Approved reference does not exist", error.getvalue())


if __name__ == "__main__":
    unittest.main()
