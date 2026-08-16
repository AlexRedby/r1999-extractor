from importlib import import_module

from r1999extractor.cli import cli_error
from r1999extractor.compatibility import legacy_workflow_notice
from r1999extractor.pregeneration import default_jobs_root


def audition_main(arguments=None):
    return _run_optional_qt_ui(
        "r1999extractor.reverse1999_audition_ui",
        arguments,
        "Source-reference audition",
    )


def pregenerate_main(arguments=None):
    legacy_workflow_notice("r1999-pregenerate", (default_jobs_root,))
    return _run_optional_qt_ui(
        "r1999extractor.pregeneration_ui",
        arguments,
        "Legacy pregeneration",
    )


def _run_optional_qt_ui(module_name, arguments, label):
    try:
        entrypoint = import_module(module_name).main
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            return cli_error(
                f"{label} requires optional Qt; install it with `uv sync --extra ui`. "
                "Headless source extraction does not require Qt."
            )
        raise
    return entrypoint(arguments)
