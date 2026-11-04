"""Mailbox-scoped references: UIDs are not account-wide, msgid is."""
import imaplib
import unittest
from email.message import EmailMessage
from unittest import mock

from gmail_imap_mcp.core import GmailError, MailClient, Settings

ALL_MAIL_RAW = "[Gmail]/All Mail"
TRASH_RAW = "[Gmail]/&XfJSIJZkkK5O9g-"
TRASH_DECODED = "[Gmail]/已删除邮件"


def make_message(subject="hello", body="body", attachment=b"", filename="a.txt"):
    msg = EmailMessage()
    msg["From"] = "Sender <sender@example.com>"
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<orig@example.com>"
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment, maintype="application",
                           subtype="octet-stream", filename=filename)
    return msg.as_bytes()


class FakeIMAP:
    """The slice of imaplib that MailClient talks to."""

    def __init__(self):
        self.selected: list[str] = []
        self.fail_select: set[str] = set()
        self.search_uids = b"1 2 3"
        self.msgid_uid = b"77"
        self.fetched: list[tuple[str, str]] = []
        self.headers = {}
        self.bodies = {}

    # -- LIST/SELECT -------------------------------------------------
    def list(self, directory='""', pattern="*"):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
            b'(\\HasNoChildren) "/" "[Gmail]/&XfJSIJZkkK5O9g-"',
        ]

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        if name in self.fail_select:
            raise imaplib.IMAP4.error("mailbox does not exist")
        self.selected.append(name)
        return "OK", [b"1"]

    # -- UID ---------------------------------------------------------
    def uid(self, command, *args):
        self.fetched.append((command, args[0] if args else ""))
        if command == "SEARCH":
            if args[0] == "X-GM-RAW":
                return "OK", [self.search_uids]
            if args[0] == "X-GM-MSGID":
                return "OK", [self.msgid_uid]
        if command == "FETCH":
            uids = str(args[0]).split(",")
            if "HEADER.FIELDS" in args[1]:
                return "OK", [self.headers[u] for u in uids if u in self.headers] + [b")"]
            uid = uids[0]
            meta = (
                f"1 (UID {uid} RFC822.SIZE 100 FLAGS () "
                f"X-GM-MSGID {1000 + int(uid)} X-GM-LABELS ())"
            ).encode()
            return "OK", [(meta, self.bodies.get(uid, b""))]
        raise AssertionError(f"unexpected UID command {command} {args}")


def client_with(fake):
    client = MailClient(Settings("me@gmail.com", "x" * 16))
    client._run = lambda fn: fn(fake)  # no network
    return client


class SearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIMAP()
        for uid in ("1", "2", "3"):
            meta = (f'1 (UID {uid} RFC822.SIZE 100 FLAGS () '
                    f'X-GM-MSGID {1000 + int(uid)} X-GM-LABELS ())').encode()
            headers = (f"From: a@b.com\r\nSubject: s{uid}\r\n"
                       "Message-ID: <m{0}@x>\r\n\r\n".format(uid)).encode()
            self.fake.headers[uid] = (meta, headers)
        self.client = client_with(self.fake)

    def test_search_returns_mailbox_and_msgid(self):
        data = self.client.search("from:a", limit=3)
        self.assertEqual(data["mailbox"], ALL_MAIL_RAW)
        self.assertEqual(data["mailbox_raw"], ALL_MAIL_RAW)
        # newest first
        self.assertEqual([m["uid"] for m in data["messages"]], ["3", "2", "1"])
        self.assertTrue(all(m["mailbox"] == ALL_MAIL_RAW for m in data["messages"]))
        self.assertEqual([m["msgid"] for m in data["messages"]],
                         ["1003", "1002", "1001"])

    def test_narrowed_search_records_its_mailbox(self):
        data = self.client.search("from:a", limit=3, mailbox="INBOX")
        self.assertEqual(data["mailbox"], "INBOX")
        self.assertEqual(self.fake.selected[-1], "INBOX")

    def test_unknown_explicit_mailbox_raises_instead_of_falling_back(self):
        self.fake.fail_select.add("Nope")
        with self.assertRaises(GmailError):
            self.client.search("x", mailbox="Nope")
        self.assertNotIn("INBOX", self.fake.selected)


class MailboxNameTests(unittest.TestCase):
    def test_decoded_folder_maps_to_raw_utf7(self):
        fake = FakeIMAP()
        client = client_with(fake)
        client.search("x", mailbox=TRASH_DECODED)
        self.assertEqual(fake.selected[-1], TRASH_RAW)

    def test_raw_folder_still_works(self):
        fake = FakeIMAP()
        client_with(fake).search("x", mailbox=TRASH_RAW)
        self.assertEqual(fake.selected[-1], TRASH_RAW)


class ReadScopeTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIMAP()
        raw = make_message(subject="sub", body="plain", attachment=b"DATA")
        for uid in ("77", "3"):
            self.fake.bodies[uid] = raw
        self.fake.headers["77"] = (
            b'1 (UID 77 BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)] {0}',
            b"Message-ID: <orig@example.com>\r\nReferences: <root@example.com>\r\n",
        )
        self.client = client_with(self.fake)

    def test_read_by_uid_uses_named_mailbox(self):
        data = self.client.read("3", mailbox="INBOX")
        self.assertEqual(self.fake.selected[-1], "INBOX")
        self.assertEqual(data["mailbox"], "INBOX")
        self.assertEqual(data["uid"], "3")
        self.assertEqual(data["subject"], "sub")

    def test_read_by_uid_defaults_to_all_mail(self):
        data = self.client.read("3")
        self.assertEqual(self.fake.selected[-1], ALL_MAIL_RAW)
        self.assertEqual(data["mailbox"], ALL_MAIL_RAW)

    def test_read_by_msgid_resolves_in_all_mail(self):
        data = self.client.read(msgid="1777")
        self.assertEqual(self.fake.selected[-1], ALL_MAIL_RAW)
        self.assertEqual(data["uid"], "77")
        self.assertEqual(data["msgid"], "1077")
        self.assertIn(("SEARCH", "X-GM-MSGID"), self.fake.fetched)

    def test_missing_reference_raises(self):
        with self.assertRaises(GmailError):
            self.client.read()

    def test_attachment_round_trip(self):
        name, ctype, blob = self.client.attachment("3", 0, mailbox="INBOX")
        self.assertEqual(name, "a.txt")
        self.assertEqual(ctype, "application/octet-stream")
        self.assertEqual(blob, b"DATA")

    def test_attachment_index_out_of_range(self):
        with self.assertRaises(GmailError):
            self.client.attachment("3", 9, mailbox="INBOX")


class ThreadingTests(unittest.TestCase):
    def test_reply_headers_are_fetched_from_the_right_box(self):
        fake = FakeIMAP()
        fake.headers["77"] = (
            b"1 (BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)] {0}",
            b"Message-ID: <orig@example.com>\r\nReferences: <root@example.com>\r\n",
        )
        client = client_with(fake)
        msg = EmailMessage()
        client._apply_threading(msg, "77", mailbox="INBOX")
        self.assertEqual(msg["In-Reply-To"], "<orig@example.com>")
        self.assertEqual(msg["References"],
                         "<root@example.com> <orig@example.com>")
        self.assertEqual(fake.selected[-1], "INBOX")


if __name__ == "__main__":
    unittest.main()
