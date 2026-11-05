"""Inbound attachment policy: allowed folders, safe names, no clobbering."""
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from gmail_imap_mcp import core
from gmail_imap_mcp.core import (
    GmailError,
    _safe_filename,
    _save_attachments,
    default_download_dir,
    resolve_download_dir,
    write_attachment,
)


class SafeNameTests(unittest.TestCase):
    def test_plain_name_kept(self):
        self.assertEqual(_safe_filename("report.pdf"), "report.pdf")

    def test_path_separators_replaced(self):
        name = _safe_filename("../../etc/passwd")
        self.assertNotIn("/", name)
        self.assertNotIn(os.sep, name)
        self.assertNotIn(".." + os.sep, name)
        self.assertTrue(name)

    def test_backslash_path_replaced(self):
        name = _safe_filename(".." + chr(92) + ".." + chr(92) + "Startup" + chr(92) + "evil.exe")
        self.assertNotIn(chr(92), name)
        self.assertFalse(name.startswith(".."))

    def test_dot_only_names_fall_back(self):
        for raw in ("", ".", "..", "...", "   "):
            self.assertEqual(_safe_filename(raw), "attachment.bin")

    def test_windows_device_name_prefixed(self):
        self.assertTrue(_safe_filename("CON.txt").startswith("_"))
        self.assertTrue(_safe_filename("nul").startswith("_"))

    def test_control_characters_stripped(self):
        self.assertNotIn(chr(0), _safe_filename("a" + chr(0) + "b.txt"))
        self.assertNotIn(chr(10), _safe_filename("a" + chr(10) + "b.txt"))

    def test_long_name_clipped(self):
        self.assertLessEqual(len(_safe_filename("x" * 500 + ".pdf")), 140)


class DownloadDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "allowed"
        self.outside = Path(self.tmp.name) / "outside"
        self.env = mock.patch.dict(
            os.environ,
            {core.DOWNLOAD_DIRS_ENV: str(self.root)},
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_default_dir_is_under_downloads(self):
        self.assertEqual(default_download_dir(),
                         Path.home() / "Downloads" / core.APP_NAME)

    def test_env_root_allowed_and_created(self):
        target = resolve_download_dir(self.root / "nested")
        self.assertTrue(target.is_dir())
        self.assertEqual(target, (self.root / "nested").resolve())

    def test_outside_root_refused(self):
        with self.assertRaises(GmailError) as ctx:
            resolve_download_dir(self.outside)
        self.assertIn(core.DOWNLOAD_DIRS_ENV, str(ctx.exception))

    def test_parent_of_root_refused(self):
        with self.assertRaises(GmailError):
            resolve_download_dir(Path(self.tmp.name))

    def test_path_that_is_a_file_refused(self):
        blob = self.root / "already-a-file"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_text("x", encoding="utf-8")
        with self.assertRaises(GmailError):
            resolve_download_dir(blob)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "dl"
        self.env = mock.patch.dict(
            os.environ, {core.DOWNLOAD_DIRS_ENV: str(self.root)}, clear=False
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_writes_below_root(self):
        target = write_attachment(self.root / "sub", "note.txt", b"hello")
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertTrue(str(target).startswith(str(self.root.resolve())))

    def test_existing_file_is_not_overwritten(self):
        first = write_attachment(self.root, "a.txt", b"one")
        second = write_attachment(self.root, "a.txt", b"two")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"one")
        self.assertEqual(second.read_bytes(), b"two")
        self.assertTrue(second.name.startswith("a_"))

    def test_traversal_name_cannot_escape(self):
        target = write_attachment(self.root, "../../escape.txt", b"x")
        self.assertEqual(target.parent, self.root.resolve())

    def test_refuses_outside_root(self):
        with self.assertRaises(GmailError):
            write_attachment(Path(self.tmp.name) / "elsewhere", "a.txt", b"x")

    def test_save_attachments_from_message(self):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "b@example.com"
        msg["Subject"] = "hi"
        msg.set_content("body")
        msg.add_attachment(b"payload", maintype="application",
                           subtype="octet-stream", filename=".." + os.sep + "evil.bin")
        saved = _save_attachments(msg, self.root)
        self.assertEqual(len(saved), 1)
        path = Path(saved[0])
        self.assertEqual(path.parent, self.root.resolve())
        self.assertEqual(path.read_bytes(), b"payload")


if __name__ == "__main__":
    unittest.main()
