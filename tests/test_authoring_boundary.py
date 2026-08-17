import importlib.util
import tomllib
import unittest
from pathlib import Path


class AuthoringBoundaryTest(unittest.TestCase):
    def test_generic_authoring_modules_and_commands_are_absent(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        scripts = project["project"]["scripts"]

        self.assertTrue(
            {
                "r1999-generation-queue",
                "r1999-generate",
                "r1999-pregenerate",
            }.isdisjoint(scripts)
        )
        for module in (
            "bulk_generation",
            "compatibility",
            "delivery",
            "generation_queue",
            "moss_generation",
            "pregeneration",
            "pregeneration_ui",
        ):
            with self.subTest(module=module):
                self.assertIsNone(importlib.util.find_spec(f"r1999extractor.{module}"))


if __name__ == "__main__":
    unittest.main()
