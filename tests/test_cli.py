import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from r1999extractor.cli import cli_error, cli_message, cli_success


class CliTest(unittest.TestCase):
    def test_success_writes_stdout_and_returns_zero(self):
        output = StringIO()
        with redirect_stdout(output):
            result = cli_success("done")
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "done\n")

    def test_error_writes_stderr_and_preserves_requested_exit_code(self):
        output = StringIO()
        with redirect_stderr(output):
            result = cli_error(ValueError("bad input"), exit_code=2)
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "bad input\n")

    def test_message_can_report_nonzero_stdout_status(self):
        output = StringIO()
        with redirect_stdout(output):
            result = cli_message("violations", exit_code=1)
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
