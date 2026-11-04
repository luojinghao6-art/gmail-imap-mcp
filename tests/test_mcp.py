"""MCP surface: tools exist, schemas carry the mailbox-safe parameters."""
import asyncio
import unittest

try:
    import mcp  # noqa: F401
    from gmail_imap_mcp import server
except ImportError:  # core-only install (CI leg without the [mcp] extra)
    server = None


@unittest.skipIf(server is None, "mcp extra not installed")
class McpSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

    def schema(self, name):
        tool = self.tools[name]
        return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})

    def test_expected_tools(self):
        self.assertEqual(set(self.tools), {
            "gmail_status", "gmail_search", "gmail_read", "gmail_send",
            "gmail_reply", "gmail_labels", "gmail_download_attachment",
        })

    def test_read_exposes_mailbox_and_msgid(self):
        props = self.schema("gmail_read")["properties"]
        self.assertIn("mailbox", props)
        self.assertIn("msgid", props)
        self.assertNotIn("uid", self.schema("gmail_read").get("required", []))

    def test_reply_exposes_mailbox_and_msgid(self):
        props = self.schema("gmail_reply")["properties"]
        self.assertIn("mailbox", props)
        self.assertIn("msgid", props)

    def test_download_exposes_mailbox_and_msgid(self):
        props = self.schema("gmail_download_attachment")["properties"]
        self.assertIn("mailbox", props)
        self.assertIn("msgid", props)

    def test_send_exposes_reply_refs(self):
        props = self.schema("gmail_send")["properties"]
        self.assertIn("reply_to_uid", props)
        self.assertIn("reply_mailbox", props)
        self.assertIn("reply_msgid", props)

    def test_search_documents_mailbox(self):
        props = self.schema("gmail_search")["properties"]
        self.assertIn("mailbox", props)

    def test_untrusted_input_warning_is_present(self):
        text = (self.tools["gmail_read"].description or "").lower()
        self.assertIn("untrusted", text)


if __name__ == "__main__":
    unittest.main()
