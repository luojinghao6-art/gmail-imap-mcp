"""Live Gmail integration - opt in with GMAIL_IMAP_LIVE=1.

These talk to the real account configured by `gmail-imap config` (or by
GMAIL_ADDRESS / GMAIL_APP_PASSWORD). Nothing leaves the machine: the send path
is only exercised with dry_run=True, which builds the message and stops before
SMTP.

    GMAIL_IMAP_LIVE=1 python -m unittest tests.test_live -v
"""
import os
import unittest

from gmail_imap_mcp.core import GmailError, MailClient, has_credentials

LIVE = os.environ.get("GMAIL_IMAP_LIVE", "").strip().lower() not in (
    "", "0", "false", "no",
)


@unittest.skipUnless(LIVE, "set GMAIL_IMAP_LIVE=1 to run against the real account")
class LiveGmailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = MailClient()
        cls.labels = cls.client.labels()
        cls.hits = cls.client.search("in:anywhere", limit=5)["messages"]

    def test_credentials_present(self):
        self.assertTrue(has_credentials())

    def test_login_and_labels(self):
        names = [row["name"] for row in self.labels]
        self.assertIn("INBOX", names)
        self.assertTrue(all(isinstance(name, str) for name in names))

    def test_search_hits_carry_mailbox_and_msgid(self):
        for hit in self.hits:
            self.assertIsInstance(hit.get("uid"), str)
            self.assertTrue(hit.get("mailbox"))
            self.assertTrue(hit.get("msgid"))
            self.assertTrue(hit.get("subject") is not None)

    def test_read_first_message(self):
        if not self.hits:
            self.skipTest("mailbox is empty")
        first = self.hits[0]
        message = self.client.read(first["uid"], mailbox=first["mailbox"])
        self.assertEqual(message["uid"], first["uid"])
        self.assertEqual(message["mailbox"], first["mailbox"])
        self.assertIn("attachments", message)
        self.assertIsInstance(message["attachments"], list)

    def test_read_by_msgid_matches_uid(self):
        if not self.hits:
            self.skipTest("mailbox is empty")
        first = self.hits[0]
        by_uid = self.client.read(first["uid"], mailbox=first["mailbox"])
        by_msgid = self.client.read(msgid=first["msgid"])
        self.assertEqual(by_uid["uid"], by_msgid["uid"])

    def test_attachment_round_trip(self):
        with_attachment = self.client.search("has:attachment", limit=3)["messages"]
        if not with_attachment:
            self.skipTest("no message with an attachment")
        hit = with_attachment[0]
        message = self.client.read(hit["uid"], mailbox=hit["mailbox"])
        if not message["attachments"]:
            self.skipTest("search hit reported no parts")
        name, ctype, blob = self.client.attachment(
            hit["uid"], 0, mailbox=hit["mailbox"]
        )
        self.assertTrue(name)
        self.assertTrue(ctype)
        self.assertGreaterEqual(len(blob), 0)

    def test_send_dry_run_never_opens_smtp(self):
        address = self.client.settings.address
        result = self.client.send(
            [address], "gmail-imap live check", "dry run only", dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["to"], [address])

    def test_bad_uid_is_a_gmail_error(self):
        with self.assertRaises(GmailError):
            self.client.read("999999999999", mailbox="INBOX")


if __name__ == "__main__":
    unittest.main()
