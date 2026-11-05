"""Outbound policy: read-only, attachment allowlist, secrets, audit, dry-run."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmail_imap_mcp import core
from gmail_imap_mcp.core import (
    AuditError,
    GmailError,
    MailClient,
    PartialSendError,
    SendStateUnknownError,
    SentButUnauditedError,
    Settings,
    audit_log_path,
)


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = {
            "GMAIL_AGENT_HOME": str(self.home),
            "GMAIL_ADDRESS": "",
            "GMAIL_APP_PASSWORD": "",
            "GMAIL_MCP_READ_ONLY": "",
            "GMAIL_MCP_ATTACHMENT_DIRS": "",
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        legacy = mock.patch(
            "gmail_imap_mcp.core._legacy_config",
            lambda: self.home / "legacy.json",
        )
        legacy.start()
        self.addCleanup(legacy.stop)

        self.client = MailClient(Settings("me@gmail.com", "x" * 16))
        self.sent = []

        def fake_smtp(msg, recipients):
            self.sent.append((msg, recipients))
            return {}

        self.client._smtp_send = fake_smtp

    def audit_rows(self):
        path = audit_log_path()
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line]

    def test_send_builds_message_and_audits(self):
        result = self.client.send(["a@b.com", "c@d.com"], "Hi", "Hello",
                                  cc=["cc@x.com"], bcc=["bcc@x.com"])
        self.assertTrue(result["ok"])
        msg, recipients = self.sent[0]
        self.assertEqual(msg["To"], "a@b.com, c@d.com")
        self.assertEqual(msg["Cc"], "cc@x.com")
        self.assertEqual(msg["Subject"], "Hi")
        self.assertEqual(recipients, ["a@b.com", "c@d.com", "cc@x.com", "bcc@x.com"])
        rows = self.audit_rows()
        self.assertEqual(rows[-1]["event"], "send")
        self.assertEqual(rows[-1]["to"], ["a@b.com", "c@d.com"])
        self.assertIn("message_id", rows[-1])

    def test_html_alternative(self):
        self.client.send(["a@b.com"], "s", "<b>hi</b>", html=True)
        msg, _ = self.sent[0]
        self.assertEqual(msg.get_content_type(), "multipart/alternative")

    def test_attachment_added(self):
        f = self.home / "report.txt"
        f.write_text("payload", encoding="utf-8")
        result = self.client.send(["a@b.com"], "s", "b", attachments=[str(f)])
        self.assertEqual(result["attachments"], ["report.txt"])
        msg, _ = self.sent[0]
        names = [p.get_filename() for p in msg.iter_attachments()]
        self.assertEqual(names, ["report.txt"])

    def test_dry_run_never_touches_smtp(self):
        result = self.client.send(["a@b.com"], "s", "b", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.audit_rows()[-1]["event"], "dry_run")

    def test_read_only_blocks_send(self):
        os.environ["GMAIL_MCP_READ_ONLY"] = "1"
        with self.assertRaises(GmailError):
            self.client.send(["a@b.com"], "s", "b")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.audit_rows()[-1]["event"], "blocked")

    def test_read_only_still_allows_dry_run(self):
        os.environ["GMAIL_MCP_READ_ONLY"] = "1"
        self.assertTrue(self.client.send(["a@b.com"], "s", "b", dry_run=True)["dry_run"])

    def test_secret_attachment_refused(self):
        env_file = self.home / ".env"
        env_file.write_text("TOKEN=1", encoding="utf-8")
        with self.assertRaises(GmailError):
            self.client.send(["a@b.com"], "s", "b", attachments=[str(env_file)])
        self.assertEqual(self.sent, [])

    def test_key_attachment_refused(self):
        key = self.home / "server.pem"
        key.write_text("-----BEGIN", encoding="utf-8")
        with self.assertRaises(GmailError):
            self.client.send(["a@b.com"], "s", "b", attachments=[str(key)])

    def test_attachment_allowlist(self):
        allowed = self.home / "allowed"
        allowed.mkdir()
        inside = allowed / "ok.txt"
        inside.write_text("ok", encoding="utf-8")
        outside = self.home / "elsewhere.txt"
        outside.write_text("no", encoding="utf-8")
        os.environ["GMAIL_MCP_ATTACHMENT_DIRS"] = str(allowed)

        with self.assertRaises(GmailError):
            self.client.send(["a@b.com"], "s", "b", attachments=[str(outside)])
        self.client.send(["a@b.com"], "s", "b", attachments=[str(inside)])
        self.assertEqual(len(self.sent), 1)

    def test_missing_attachment(self):
        with self.assertRaises(GmailError):
            self.client.send(["a@b.com"], "s", "b",
                             attachments=[str(self.home / "nope.txt")])

    def test_smtp_failure_is_audited(self):
        self.client._smtp_send = lambda msg, recipients: {"a@b.com": (550, b"no")}
        with self.assertRaises(GmailError):
            self.client.send(["a@b.com"], "s", "b")
        row = self.audit_rows()[-1]
        self.assertEqual(row["event"], "send_error")
        self.assertIn("refused", row["error"])


    # -- audit ordering ---------------------------------------------------
    def test_intent_is_recorded_before_smtp(self):
        seen = {}

        def fake_smtp(msg, recipients):
            seen["events"] = [row["event"] for row in self.audit_rows()]
            return {}

        self.client._smtp_send = fake_smtp
        self.client.send(["a@b.com"], "s", "b")
        self.assertIn("send_intent", seen["events"])
        self.assertEqual([row["event"] for row in self.audit_rows()],
                         ["send_intent", "send"])

    def test_strict_audit_failure_stops_before_smtp(self):
        with mock.patch.dict(os.environ, {"GMAIL_MCP_AUDIT_STRICT": "1"}):
            with mock.patch("gmail_imap_mcp.core._audit",
                            side_effect=AuditError("disk full")):
                with self.assertRaises(AuditError):
                    self.client.send(["a@b.com"], "s", "b")
        self.assertEqual(self.sent, [])

    def test_audit_failure_after_send_is_reported_as_sent(self):
        calls = {"n": 0}
        real = core._audit

        def flaky(event, **fields):
            calls["n"] += 1
            if calls["n"] == 1:
                return real(event, **fields)
            raise AuditError("disk full")

        with mock.patch("gmail_imap_mcp.core._audit", side_effect=flaky):
            with self.assertRaises(SentButUnauditedError) as ctx:
                self.client.send(["a@b.com"], "s", "b")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("WAS sent", str(ctx.exception))

    def test_partial_delivery_is_reported(self):
        self.client._smtp_send = lambda msg, recipients: {"c@d.com": (550, b"no")}
        with self.assertRaises(PartialSendError) as ctx:
            self.client.send(["a@b.com", "c@d.com"], "s", "b")
        err = ctx.exception
        self.assertEqual(err.sent_to, ["a@b.com"])
        self.assertEqual(err.refused, ["c@d.com"])
        self.assertIn("WAS delivered", str(err))
        row = self.audit_rows()[-1]
        self.assertEqual(row["event"], "send_partial")
        self.assertEqual(row["delivered"], ["a@b.com"])
        self.assertEqual(row["refused"], ["c@d.com"])

    def test_smtp_drop_mid_send_is_reported_as_unknown(self):
        def boom(msg, recipients):
            raise SendStateUnknownError("connection dropped")

        self.client._smtp_send = boom
        with self.assertRaises(SendStateUnknownError):
            self.client.send(["a@b.com"], "s", "b")
        row = self.audit_rows()[-1]
        self.assertEqual(row["event"], "send_error")
        self.assertIn("connection dropped", row["error"])

    def test_read_only_message_survives_an_audit_failure(self):
        with mock.patch.dict(os.environ, {"GMAIL_MCP_READ_ONLY": "1"}):
            with mock.patch("gmail_imap_mcp.core._audit",
                            side_effect=AuditError("disk full")):
                with self.assertRaises(GmailError) as ctx:
                    self.client.send(["a@b.com"], "s", "b")
        self.assertIn("GMAIL_MCP_READ_ONLY", str(ctx.exception))
        self.assertEqual(self.sent, [])


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {
            "GMAIL_AGENT_HOME": self.tmp.name,
            "GMAIL_ADDRESS": "",
            "GMAIL_APP_PASSWORD": "",
            "GMAIL_MCP_READ_ONLY": "",
            "GMAIL_MCP_ATTACHMENT_DIRS": "",
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        legacy = mock.patch(
            "gmail_imap_mcp.core._legacy_config",
            lambda: Path(self.tmp.name) / "legacy.json",
        )
        legacy.start()
        self.addCleanup(legacy.stop)

        self.client = MailClient(Settings("me@gmail.com", "x" * 16))
        self.original = {
            "uid": "77",
            "msgid": "1077",
            "mailbox": "INBOX",
            "mailbox_raw": "INBOX",
            "from": "Sender <sender@x.com>",
            "to": "me@gmail.com, other@x.com",
            "cc": "cc@x.com",
            "subject": "topic",
            "text": "line one\nline two",
        }
        self.client.read = lambda *a, **kw: self.original
        self.threaded = []
        self.client._apply_threading = (
            lambda msg, uid=None, mailbox=None, msgid=None:
            self.threaded.append((uid, mailbox, msgid))
        )
        self.sent = []
        self.client._smtp_send = lambda msg, recipients: (
            self.sent.append((msg, recipients)) or {}
        )

    def test_reply_keeps_thread_and_quotes(self):
        self.client.reply("77", "thanks", mailbox="INBOX")
        msg, recipients = self.sent[0]
        self.assertEqual(recipients, ["sender@x.com"])
        self.assertEqual(msg["Subject"], "Re: topic")
        self.assertIn("> line one", msg.get_content())
        self.assertEqual(self.threaded, [("77", "INBOX", "1077")])

    def test_reply_all_dedupes_self_and_sender(self):
        self.client.reply("77", "thanks", reply_all=True, mailbox="INBOX")
        msg, recipients = self.sent[0]
        self.assertEqual(recipients[0], "sender@x.com")
        self.assertIn("other@x.com", msg["Cc"])
        self.assertIn("cc@x.com", msg["Cc"])
        self.assertNotIn("me@gmail.com", msg["Cc"])


if __name__ == "__main__":
    unittest.main()
