"""Credential storage, audit trail durability and optional attachment scanning."""
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from email.message import EmailMessage
from io import StringIO
from pathlib import Path
from unittest import mock

from gmail_imap_mcp import core
from gmail_imap_mcp.core import (
    GmailError,
    PermissionHardeningError,
    _audit,
    _harden_file,
    _save_attachments,
    _windows_acl_private,
    _windows_principals,
    audit_log_path,
    load_settings,
    save_settings,
    scan_attachment,
)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.env = {
            "GMAIL_AGENT_HOME": str(self.home),
            "GMAIL_ADDRESS": "me@gmail.com",
            "GMAIL_APP_PASSWORD": "",
            "GMAIL_APP_PASSWORD_FILE": "",
            "GMAIL_MCP_AUDIT_STRICT": "",
            "GMAIL_MCP_SCAN_CMD": "",
            "GMAIL_MCP_DOWNLOAD_DIRS": str(Path(self.tmp.name) / "dl"),
        }
        patcher = mock.patch.dict(os.environ, self.env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        legacy = mock.patch(
            "gmail_imap_mcp.core._legacy_config",
            lambda: Path(self.tmp.name) / "legacy.json",
        )
        legacy.start()
        self.addCleanup(legacy.stop)
        core._audit_warned = False
        core._perms_warned = False
        core._hardened_paths.clear()


class AuditTests(Base):
    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_audit_file_is_owner_only(self):
        _audit("test_event", note="hello")
        mode = audit_log_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def blocked_home(self):
        """Point the config home at a regular file so writes cannot happen."""
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        os.environ["GMAIL_AGENT_HOME"] = str(blocker)
        return blocker

    def test_failed_audit_warns_once(self):
        self.blocked_home()
        err = StringIO()
        with redirect_stderr(err):
            _audit("one")
            _audit("two")
        text = err.getvalue()
        self.assertEqual(text.count("warning: cannot write audit log"), 1)
        self.assertIn("audit.log", text)

    def test_failed_audit_is_fatal_when_strict(self):
        self.blocked_home()
        os.environ["GMAIL_MCP_AUDIT_STRICT"] = "1"
        with redirect_stderr(StringIO()):
            with self.assertRaises(GmailError) as ctx:
                _audit("send")
        self.assertIn("GMAIL_MCP_AUDIT_STRICT", str(ctx.exception))


class PermissionTests(Base):
    def probe(self):
        path = Path(self.tmp.name) / "probe.txt"
        path.write_text("x", encoding="utf-8")
        return path

    def force_failure(self):
        if os.name == "nt":
            return mock.patch("gmail_imap_mcp.core._harden_windows",
                              return_value=(False, "denied"))
        return mock.patch("gmail_imap_mcp.core.os.chmod",
                          side_effect=OSError("denied"))

    def test_failure_is_reported_not_swallowed(self):
        path = self.probe()
        err = StringIO()
        with self.force_failure():
            with redirect_stderr(err):
                self.assertFalse(_harden_file(path))
                _harden_file(path)
        self.assertEqual(err.getvalue().count("could not restrict permissions"), 1)
        self.assertIn("denied", err.getvalue())

    def test_strict_permissions_raises(self):
        path = self.probe()
        with mock.patch.dict(os.environ,
                             {"GMAIL_MCP_STRICT_PERMISSIONS": "1"}):
            with self.force_failure():
                with redirect_stderr(StringIO()):
                    with self.assertRaises(PermissionHardeningError):
                        _harden_file(path)

    def test_save_settings_does_not_keep_an_unprotected_secret(self):
        with mock.patch.dict(os.environ,
                             {"GMAIL_MCP_STRICT_PERMISSIONS": "1"}):
            with mock.patch("gmail_imap_mcp.core._harden_once",
                            side_effect=PermissionHardeningError("nope")):
                with self.assertRaises(PermissionHardeningError):
                    save_settings("me@gmail.com", "a" * 16)
        self.assertFalse((self.home / "credentials.json").exists())

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_acl_is_verified(self):
        path = self.probe()
        self.assertTrue(_harden_file(path))
        self.assertTrue(_windows_acl_private(path, _windows_principals()))

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_saved_credentials_are_owner_only(self):
        path = save_settings("me@gmail.com", "a" * 16)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class PasswordFileTests(Base):
    def secret_file(self, content):
        path = Path(self.tmp.name) / "app-password"
        path.write_text(content, encoding="utf-8")
        return path

    def test_password_can_come_from_a_file(self):
        path = self.secret_file("abcd efgh ijkl mnop\n")
        os.environ["GMAIL_APP_PASSWORD_FILE"] = str(path)
        settings = load_settings()
        self.assertEqual(settings.app_password, "abcdefghijklmnop")

    def test_env_password_wins_over_file(self):
        path = self.secret_file("aaaaaaaabbbbbbbb")
        os.environ["GMAIL_APP_PASSWORD_FILE"] = str(path)
        os.environ["GMAIL_APP_PASSWORD"] = "ccccccccdddddddd"
        self.assertEqual(load_settings().app_password, "ccccccccdddddddd")

    def test_missing_secret_file_is_reported(self):
        os.environ["GMAIL_APP_PASSWORD_FILE"] = str(Path(self.tmp.name) / "nope")
        with self.assertRaises(GmailError) as ctx:
            load_settings()
        self.assertIn("GMAIL_APP_PASSWORD_FILE", str(ctx.exception))


class ScanTests(Base):
    def scanner(self, exit_code, spaced=False):
        folder = Path(self.tmp.name) / ("with space" if spaced else "bin")
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / "scan.py"
        script.write_text(f"import sys\nsys.exit({exit_code})\n", encoding="utf-8")
        return script

    def use_scanner(self, script, placeholder=True):
        tail = " {}" if placeholder else ""
        os.environ["GMAIL_MCP_SCAN_CMD"] = f'"{sys.executable}" "{script}"{tail}'

    def target(self):
        path = Path(self.tmp.name) / "dl" / "file.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")
        return path

    def test_no_scanner_means_unscanned(self):
        result = scan_attachment(self.target())
        self.assertFalse(result["scanned"])
        self.assertIn("GMAIL_MCP_SCAN_CMD", result["reason"])

    def test_clean_file_kept(self):
        self.use_scanner(self.scanner(0))
        path = self.target()
        result = scan_attachment(path)
        self.assertTrue(result["scanned"])
        self.assertTrue(result["clean"])
        self.assertTrue(path.exists())

    def test_scanner_path_with_spaces(self):
        self.use_scanner(self.scanner(0, spaced=True))
        self.assertTrue(scan_attachment(self.target())["clean"])

    def test_flagged_file_deleted(self):
        self.use_scanner(self.scanner(1))
        path = self.target()
        with self.assertRaises(GmailError) as ctx:
            scan_attachment(path)
        self.assertIn("deleted", str(ctx.exception))
        self.assertFalse(path.exists())

    def test_scanner_error_deletes_and_raises(self):
        self.use_scanner(self.scanner(3))
        path = self.target()
        with self.assertRaises(GmailError):
            scan_attachment(path)
        self.assertFalse(path.exists())

    def test_missing_scanner_deletes_and_raises(self):
        os.environ["GMAIL_MCP_SCAN_CMD"] = "definitely-not-a-real-scanner-xyz {}"
        path = self.target()
        with self.assertRaises(GmailError):
            scan_attachment(path)
        self.assertFalse(path.exists())

    def test_save_attachments_runs_the_scanner(self):
        self.use_scanner(self.scanner(1))
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "b@example.com"
        msg["Subject"] = "hi"
        msg.set_content("body")
        msg.add_attachment(b"payload", maintype="application",
                           subtype="octet-stream", filename="evil.bin")
        out_dir = Path(self.tmp.name) / "dl"
        with self.assertRaises(GmailError):
            _save_attachments(msg, out_dir)
        self.assertEqual(list(out_dir.glob("evil*")), [])


if __name__ == "__main__":
    unittest.main()
