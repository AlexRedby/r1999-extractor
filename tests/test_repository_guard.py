import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from r1999extractor.repository_guard import check_repository_paths


class RepositoryGuardTest(unittest.TestCase):
    def test_rejects_game_payloads_and_generated_artifacts(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = (
                "data/catalog.json",
                "output/story-index.jsonl",
                "audio/123.wem",
            )
            for value in paths:
                path = root / value
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"test")

            violations = check_repository_paths(root, paths)

        self.assertEqual(len(violations), 3)

    def test_allows_code_and_small_synthetic_fixtures(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = ("r1999extractor/tool.py", "tests/fixtures/synthetic.wav")
            for value in paths:
                path = root / value
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic")

            self.assertEqual(check_repository_paths(root, paths), ())


if __name__ == "__main__":
    unittest.main()
