"""Small, explicit output helpers shared by extractor command entry points."""

import sys


def cli_message(message, *, exit_code=0, error=False):
    """Write one command-line message and return its process exit code."""
    print(message, file=sys.stderr if error else sys.stdout)
    return exit_code


def cli_error(error, *, exit_code=1):
    return cli_message(error, exit_code=exit_code, error=True)


def cli_success(message):
    return cli_message(message)
