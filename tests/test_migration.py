import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.migration import migrate_legacy_data


class MigrationTest(unittest.TestCase):
    def test_copies_legacy_indexes_and_voice_pack_without_removing_source(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "old"
            destination = root / "new"
            index = source / "reverse1999" / "dialogue-index.json"
            voice = source / "voice-packs" / "reverse1999" / "manifest.json"
            index.parent.mkdir(parents=True)
            voice.parent.mkdir(parents=True)
            index.write_text("index", encoding="utf-8")
            voice.write_text("voice", encoding="utf-8")

            results = migrate_legacy_data(source, destination)

            self.assertEqual([result[0] for result in results], ["copied", "copied"])
            self.assertEqual(
                (destination / "reverse1999" / "dialogue-index.json").read_text(),
                "index",
            )
            self.assertEqual(
                (destination / "voice-packs" / "reverse1999" / "manifest.json").read_text(),
                "voice",
            )
            self.assertTrue(index.is_file())
            self.assertTrue(voice.is_file())

    def test_dry_run_and_existing_destinations_never_overwrite(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "old"
            destination = root / "new"
            old = source / "reverse1999" / "state.json"
            old.parent.mkdir(parents=True)
            old.write_text("old", encoding="utf-8")

            dry_run = migrate_legacy_data(source, destination, dry_run=True)
            self.assertEqual(dry_run[0][0], "would-copy")
            self.assertFalse((destination / "reverse1999").exists())

            current = destination / "reverse1999" / "state.json"
            current.parent.mkdir(parents=True)
            current.write_text("current", encoding="utf-8")
            migrated = migrate_legacy_data(source, destination)
            self.assertEqual(migrated[0][0], "exists")
            self.assertEqual(current.read_text(), "current")


if __name__ == "__main__":
    unittest.main()
