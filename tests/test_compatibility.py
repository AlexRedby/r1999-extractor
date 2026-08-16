import subprocess
import sys
import tomllib
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from r1999extractor import entrypoints
from r1999extractor.bulk_generation import main as generation_main
from r1999extractor.compatibility import legacy_workflow_notice
from r1999extractor.model_benchmark import main as benchmark_main
from r1999extractor.model_listening import main as listening_main

project_root = Path(__file__).resolve().parents[1]


class CompatibilityTest(unittest.TestCase):
    def test_headless_modules_import_when_vntts_and_qt_are_blocked(self):
        script = """
import importlib
import importlib.abc
import sys

class BlockOptionalRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        del path, target
        if fullname == "vntts" or fullname.startswith(("vntts.", "PySide6")):
            raise ModuleNotFoundError(f"blocked optional runtime: {fullname}", name=fullname)
        return None

sys.meta_path.insert(0, BlockOptionalRuntime())
for module in (
    "r1999extractor.bootstrap",
    "r1999extractor.reverse1999_catalog",
    "r1999extractor.source_audit",
    "r1999extractor.story_index",
    "r1999extractor.update_diff",
    "r1999extractor.moss_generation",
    "r1999extractor.entrypoints",
):
    importlib.import_module(module)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_base_dependencies_exclude_qt_and_vntts_speech_runtime(self):
        document = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = tuple(value.casefold() for value in document["project"]["dependencies"])

        self.assertFalse(any("pyside" in value for value in dependencies))
        self.assertFalse(any(value.split("[")[0] == "vntts" for value in dependencies))
        self.assertEqual(document["project"]["optional-dependencies"]["ui"], ["PySide6==6.10.1"])

    def test_notice_discovers_existing_artifacts_without_modifying_them(self):
        with TemporaryDirectory() as directory:
            artifact = Path(directory) / "generation-state.json"
            artifact.write_text("preserve me", encoding="utf-8")
            stderr = StringIO()

            with redirect_stderr(stderr):
                legacy_workflow_notice("r1999-generate", (artifact,))

            self.assertEqual(artifact.read_text(encoding="utf-8"), "preserve me")

        self.assertIn("Compatibility notice: r1999-generate", stderr.getvalue())
        self.assertIn("Discovered existing artifacts", stderr.getvalue())
        self.assertIn("does not migrate, delete, or regenerate", stderr.getvalue())

    def test_legacy_command_entrypoints_emit_compatibility_notices(self):
        with (
            patch("r1999extractor.bulk_generation.legacy_workflow_notice") as generation_notice,
            patch("r1999extractor.bulk_generation.review_item"),
        ):
            self.assertEqual(
                generation_main(["review", "--state", "state.json", "line", "approved"]),
                0,
            )
        generation_notice.assert_called_once()

        with (
            patch("r1999extractor.model_benchmark.legacy_workflow_notice") as benchmark_notice,
            patch("r1999extractor.model_benchmark.load_provider_config", return_value=[]),
            patch(
                "r1999extractor.model_benchmark.benchmark_models",
                return_value={"models": [], "sample_count": 0},
            ),
        ):
            self.assertEqual(benchmark_main(["--models", "models.json"]), 0)
        benchmark_notice.assert_called_once()

        with (
            patch("r1999extractor.model_listening.legacy_workflow_notice") as listening_notice,
            patch("r1999extractor.model_listening.load_listening_session", return_value={}),
            patch("r1999extractor.model_listening.listening_progress", return_value=(0, 0)),
        ):
            self.assertEqual(listening_main(["status", "--session", "session.json"]), 0)
        listening_notice.assert_called_once()

        with (
            patch("r1999extractor.entrypoints.legacy_workflow_notice") as pregeneration_notice,
            patch("r1999extractor.entrypoints._run_optional_qt_ui", return_value=0),
        ):
            self.assertEqual(entrypoints.pregenerate_main([]), 0)
        pregeneration_notice.assert_called_once()

    def test_optional_qt_entrypoints_explain_headless_install(self):
        missing_qt = ModuleNotFoundError("No module named PySide6", name="PySide6")
        stderr = StringIO()
        with (
            patch("r1999extractor.entrypoints.import_module", side_effect=missing_qt),
            redirect_stderr(stderr),
        ):
            exit_code = entrypoints.audition_main([])

        self.assertEqual(exit_code, 1)
        self.assertIn("requires optional Qt", stderr.getvalue())
        self.assertIn("Headless source extraction does not require Qt", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
