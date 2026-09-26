"""Offline regression tests; these never contact Link or create credentials."""

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import stripe


class AuthStatusTests(unittest.TestCase):
    def test_object_and_list_formats(self):
        for output in ('{"authenticated": true}', '[{"authenticated": true}]'):
            self.assertTrue(stripe.parse_auth_status(output)["authenticated"])

    def test_latest_status_wins(self):
        self.assertTrue(stripe.parse_auth_status(
            '[{"authenticated": false}, {"authenticated": true}]'
        )["authenticated"])

    def test_invalid_status_is_rejected(self):
        for output in ('[]', 'null', '[null]', '{}', '{"authenticated": "false"}'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                stripe.parse_auth_status(output)

    def test_list_response_starts_login(self):
        with patch.object(stripe, "run_link", return_value=SimpleNamespace(
            stdout='[{"authenticated": false}]'
        )) as run:
            self.assertEqual(stripe.main(["login"]), 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][:2], ["auth", "login"])

    def test_authenticated_list_does_not_start_login(self):
        with patch.object(stripe, "run_link", return_value=SimpleNamespace(
            stdout='[{"authenticated": true}]'
        )) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(stripe.main(["login"]), 0)
            run.assert_called_once()

    def test_invalid_status_reports_error_without_login(self):
        with patch.object(stripe, "run_link", return_value=SimpleNamespace(
            stdout='[]'
        )) as run, contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(stripe.main(["login"]), 1)
            run.assert_called_once()
            self.assertIn("empty authentication status", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
