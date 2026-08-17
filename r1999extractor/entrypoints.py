from importlib import import_module

from r1999extractor.cli import cli_error


def audition_main(arguments=None):
    return _run_optional_qt_ui(
        "r1999extractor.reverse1999_audition_ui",
        arguments,
        "Source-reference audition",
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
