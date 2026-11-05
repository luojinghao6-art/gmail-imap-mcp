"""Web UI: token gate, Host/Origin checks, headers, routes."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from http.server import ThreadingHTTPServer

from gmail_imap_mcp.web import app as web


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name)
        cls.env = mock.patch.dict(os.environ, {
            "GMAIL_AGENT_HOME": str(cls.home),
            "GMAIL_ADDRESS": "",
            "GMAIL_APP_PASSWORD": "",
        }, clear=False)
        cls.env.start()
        cls.legacy = mock.patch(
            "gmail_imap_mcp.core._legacy_config",
            lambda: cls.home / "legacy.json",
        )
        cls.legacy.start()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.legacy.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            res = conn.getresponse()
            payload = res.read()
            return res.status, dict(res.getheaders()), payload
        finally:
            conn.close()

    def token_path(self, path):
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}t={web.TOKEN}"

    # -- static + headers -------------------------------------------------
    def test_index_served_without_token_and_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"gmail-imap", body)
        self.assertNotIn(b"__APP_TOKEN__", body)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_index_no_longer_advertises_the_old_skill_path(self):
        _, _, body = self.request("GET", "/")
        self.assertNotIn(b".claude/skills/gmail", body)

    # -- Host allowlist ---------------------------------------------------
    def test_foreign_host_rejected(self):
        status, _, _ = self.request("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_loopback_host_with_port_allowed(self):
        status, _, _ = self.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_localhost_host_allowed(self):
        status, _, _ = self.request("GET", "/", headers={"Host": "localhost:8765"})
        self.assertEqual(status, 200)

    # -- token ------------------------------------------------------------
    def test_api_requires_token(self):
        status, _, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 403)

    def test_api_with_token(self):
        status, _, body = self.request("GET", self.token_path("/api/status"))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("config_path", data)
        self.assertIn("read_only", data)

    # -- Origin / Content-Type --------------------------------------------
    def post(self, path, payload=None, origin=None, ctype="application/json"):
        headers = {"X-App-Token": web.TOKEN, "Content-Type": ctype}
        if origin:
            headers["Origin"] = origin
        body = json.dumps(payload or {})
        return self.request("POST", path, headers=headers, body=body)

    def test_cross_origin_post_rejected(self):
        status, _, _ = self.post("/api/reconnect", origin="http://evil.example")
        self.assertEqual(status, 403)

    def test_same_origin_post_allowed(self):
        status, _, body = self.post(
            "/api/reconnect", origin=f"http://127.0.0.1:{self.port}"
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["configured"])

    def test_referer_origin_checked_when_no_origin(self):
        headers = {
            "X-App-Token": web.TOKEN,
            "Content-Type": "application/json",
            "Referer": "http://evil.example/page",
        }
        status, _, _ = self.request("POST", "/api/reconnect", headers=headers, body=b"{}")
        self.assertEqual(status, 403)

    def test_post_requires_json_content_type(self):
        status, _, _ = self.post("/api/reconnect", ctype="text/plain")
        self.assertEqual(status, 415)

    def test_unknown_route(self):
        status, _, _ = self.request("GET", self.token_path("/api/nope"))
        self.assertEqual(status, 404)

    # -- attachments ------------------------------------------------------
    def test_attachment_response_carries_security_headers(self):
        fake = ("a.txt", "text/plain", b"hi")
        with mock.patch.object(
            web, "get_attachment",
            lambda uid, idx, mailbox=None, msgid=None: fake,
        ):
            status, headers, body = self.request(
                "GET", self.token_path("/api/attachment?uid=1&idx=0")
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hi")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("attachment;", headers.get("Content-Disposition", ""))

    def test_token_redacted_before_logging(self):
        line = web.redact_token(
            '"GET /api/attachment?uid=1&t=%s HTTP/1.1" 200 -' % web.TOKEN
        )
        self.assertNotIn(web.TOKEN, line)
        self.assertIn("t=***", line)

    def test_redaction_keeps_other_params(self):
        line = web.redact_token("GET /api/message?uid=42&t=secret&idx=1 HTTP/1.1")
        self.assertIn("uid=42", line)
        self.assertIn("idx=1", line)
        self.assertNotIn("secret", line)


if __name__ == "__main__":
    unittest.main()
