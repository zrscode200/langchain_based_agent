#!/usr/bin/env python3
"""Offline checks for muse_session.py."""

from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("muse_session.py")
SPEC = importlib.util.spec_from_file_location("muse_session", MODULE_PATH)
assert SPEC and SPEC.loader
muse_session = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(muse_session)

VALUE = "0123456789abcdef0123456789abcdef"


class NormalizeSessionTests(unittest.TestCase):
    def test_accepts_raw_value(self) -> None:
        self.assertEqual(muse_session.normalize_session(VALUE), VALUE)

    def test_accepts_name_and_value(self) -> None:
        self.assertEqual(muse_session.normalize_session(f"SESSION={VALUE}"), VALUE)

    def test_extracts_session_from_complete_header(self) -> None:
        header = f"Cookie: other=discard-me; SESSION={VALUE}; final=also-discarded"
        self.assertEqual(muse_session.normalize_session(header), VALUE)

    def test_rejects_complete_header_without_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not contain SESSION"):
            muse_session.normalize_session(f"other={VALUE}; final={VALUE}")

    def test_rejects_short_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed"):
            muse_session.normalize_session("SESSION=short")


class StorageAndStatusTests(unittest.TestCase):
    def test_write_is_owner_only_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "muse-qa-session"
            muse_session.write_session(VALUE, path)
            self.assertEqual(muse_session.read_session(path), VALUE)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_status_categories_are_distinct(self) -> None:
        self.assertEqual(muse_session.describe_status(200), ("ACTIVE", True))
        self.assertEqual(
            muse_session.describe_status(401), ("EXPIRED OR INVALID", False)
        )
        self.assertEqual(
            muse_session.describe_status(503),
            ("TEMPORARILY UNAVAILABLE (HTTP 503)", False),
        )
        self.assertEqual(
            muse_session.describe_status(None), ("TEMPORARILY UNAVAILABLE", False)
        )
        self.assertEqual(
            muse_session.describe_status(403), ("ERROR (HTTP 403)", False)
        )

    def test_curl_config_keeps_cookie_out_of_process_arguments(self) -> None:
        config = muse_session._curl_config(VALUE, muse_session.DEFAULT_CHECK_URL)
        self.assertIn(f"Cookie: SESSION={VALUE}", config)
        self.assertIn("X-Client-App: ics-ddt-companion", config)
        self.assertIn("output = /dev/null", config)

    def test_request_passes_cookie_through_stdin_not_process_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "muse-qa-session"
            muse_session.write_session(VALUE, path)
            result = mock.Mock(returncode=0, stdout="200", stderr="")
            with mock.patch.object(muse_session.subprocess, "run", return_value=result) as run:
                self.assertEqual(muse_session.request_status(path), 200)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["/usr/bin/curl", "-q", "--config", "-"])
            self.assertNotIn(VALUE, " ".join(args[0]))
            self.assertIn(f"Cookie: SESSION={VALUE}", kwargs["input"])


if __name__ == "__main__":
    unittest.main()
