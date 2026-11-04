"""Unit tests that touch no network.

    python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmail_imap_mcp.core import (
    Settings,
    _clean_addresses,
    _extract_labels,
    _safe_filename,
    config_file,
    has_credentials,
    html_to_text,
    imap_utf7_decode,
    load_settings,
    save_settings,
)


class Utf7Tests(unittest.TestCase):
    def test_chinese_folder(self):
        # Gmail localised Trash, as returned by LIST
        self.assertEqual(imap_utf7_decode("[Gmail]/&XfJSIJZkkK5O9g-"),
                         "[Gmail]/已删除邮件")

    def test_ampersand_escape(self):
        self.assertEqual(imap_utf7_decode("A&-B"), "A&B")

    def test_plain_name_untouched(self):
        self.assertEqual(imap_utf7_decode("INBOX"), "INBOX")

    def test_empty(self):
        self.assertEqual(imap_utf7_decode(""), "")


class LabelParsingTests(unittest.TestCase):
    def test_simple(self):
        meta = b'1 (UID 42 X-GM-LABELS ("\\\\Inbox" "\\\\Important"))'
        self.assertEqual(_extract_labels(meta), ["\\Inbox", "\\Important"])

    def test_labels_with_spaces_and_parens(self):
        meta = b'1 (X-GM-LABELS ("\\\\Inbox" "Work (2026)" "a \\"quoted\\" tag"))'
        labels = _extract_labels(meta)
        self.assertIn("Work (2026)", labels)
        self.assertIn('a "quoted" tag', labels)

    def test_missing(self):
        self.assertEqual(_extract_labels(b"1 (FLAGS (\\Seen))"), [])


class HtmlTests(unittest.TestCase):
    def test_strips_tags_and_entities(self):
        self.assertEqual(html_to_text("<p>a &amp; b</p><p>c</p>").splitlines(),
                         ["a & b", "c"])

    def test_drops_script_and_style(self):
        self.assertEqual(html_to_text("<style>x{}</style><p>hi</p>"), "hi")


class AddressTests(unittest.TestCase):
    def test_splits_and_strips(self):
        self.assertEqual(_clean_addresses(["a@b.com, c@d.com", "e@f.com "]),
                         ["a@b.com", "c@d.com", "e@f.com"])

    def test_empty(self):
        self.assertEqual(_clean_addresses(None), [])

    def test_filename_sanitised(self):
        self.assertEqual(_safe_filename('a/b:c*?"<>|.txt'), "a_b_c______.txt")


class SettingsTests(unittest.TestCase):
    def test_spaces_removed(self):
        s = Settings("me@gmail.com", "abcd efgh ijkl mnop")
        self.assertEqual(s.app_password, "abcdefghijklmnop")

    def test_repr_hides_secret(self):
        s = Settings("me@gmail.com", "abcdefghijklmnop")
        self.assertNotIn("abcdefghijklmnop", repr(s))


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {
            "GMAIL_AGENT_HOME": self.tmp.name,
            "GMAIL_ADDRESS": "",
            "GMAIL_APP_PASSWORD": "",
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Do not let a real credentials file elsewhere on the machine leak in
        legacy = mock.patch(
            "gmail_imap_mcp.core._legacy_config",
            lambda: Path(self.tmp.name) / "legacy.json",
        )
        legacy.start()
        self.addCleanup(legacy.stop)

    def test_save_then_load(self):
        path = save_settings("me@gmail.com", "abcd efgh ijkl mnop")
        self.assertTrue(path.exists())
        self.assertEqual(path, config_file())
        loaded = load_settings()
        self.assertEqual(loaded.address, "me@gmail.com")
        self.assertEqual(loaded.app_password, "abcdefghijklmnop")

    def test_rejects_short_password(self):
        with self.assertRaises(Exception):
            save_settings("me@gmail.com", "nope")

    def test_rejects_bad_address(self):
        with self.assertRaises(Exception):
            save_settings("not-an-address", "abcdefghijklmnop")

    def test_unconfigured(self):
        self.assertFalse(has_credentials())
        self.assertIsNone(load_settings(required=False))


if __name__ == "__main__":
    unittest.main()
