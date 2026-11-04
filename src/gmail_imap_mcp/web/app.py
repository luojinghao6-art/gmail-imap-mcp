"""Local web UI for gmail-imap.

A browser front-end over the same core the MCP server uses. Binds 127.0.0.1
only and requires a per-process token, so other sites cannot drive it.

    gmail-imap-ui
    python -m gmail_imap_mcp.web.app --port 8765 --no-browser
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from ..core import (
    GmailError,
    MailClient,
    Settings,
    config_file,
    has_credentials,
    load_settings,
    save_settings,
)

HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)
UI_LOCK = threading.Lock()

_clients: dict[str, MailClient] = {}


def client() -> MailClient:
    """One client per account address, reused across requests."""
    settings = load_settings()
    key = settings.address
    if key not in _clients:
        _clients[key] = MailClient(settings)
    return _clients[key]


def drop_clients() -> None:
    for c in list(_clients.values()):
        try:
            c.close()
        except Exception:
            pass
    _clients.clear()


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- handlers

def status_payload() -> dict[str, Any]:
    if not has_credentials():
        return {"configured": False, "address": None, "connected": False}
    settings = load_settings()
    connected = False
    error = None
    try:
        client().labels()
        connected = True
    except GmailError as exc:
        error = str(exc)
    return {"configured": True, "address": settings.address,
            "connected": connected, "error": error}


def save_config(address: str, app_password: str) -> dict[str, Any]:
    address = (address or "").strip()
    app_password = (app_password or "").strip()
    if "@" not in address:
        raise ApiError("That does not look like an email address.")
    if not app_password:
        raise ApiError("App Password is empty.")

    # Validate before persisting, so a typo never overwrites working creds.
    settings = Settings(address, app_password)
    probe = MailClient(settings)
    try:
        probe.labels()
    except GmailError as exc:
        raise ApiError(f"Login check failed: {exc}") from exc
    finally:
        probe.close()

    save_settings(address, app_password)
    drop_clients()
    # The probe above already proved the login; do not log in a second time.
    return {"configured": True, "address": address,
            "connected": True, "error": None}


def list_messages(query: str, limit: int, mailbox: str | None) -> dict[str, Any]:
    try:
        return client().search(query, limit=limit, mailbox=mailbox)
    except GmailError as exc:
        raise ApiError(str(exc)) from exc


def get_message(uid: str, mailbox: str | None) -> dict[str, Any]:
    try:
        data = client().read(uid, mailbox=mailbox)
    except GmailError as exc:
        raise ApiError(str(exc)) from exc
    # The UI indexes attachments by position; core already does, but keep the
    # old key name so index.html/app.js stay stable.
    data["attachments"] = [
        {"idx": a["index"], "name": a["name"], "size": a["size"], "type": a["type"]}
        for a in data.get("attachments", [])
    ]
    return data


def get_attachment(uid: str, idx: int, mailbox: str | None):
    try:
        return client().attachment(uid, idx, mailbox=mailbox)
    except GmailError as exc:
        raise ApiError(str(exc)) from exc


def send_mail(payload: dict[str, Any]) -> dict[str, Any]:
    to = [x.strip() for x in (payload.get("to") or []) if str(x).strip()]
    if not to:
        raise ApiError("At least one recipient is required.")
    tmpdir = None
    paths: list[str] = []
    try:
        raw = payload.get("attachments") or []
        if raw:
            tmpdir = tempfile.mkdtemp(prefix="gmail-imap-ui-")
            for i, item in enumerate(raw):
                name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_",
                              item.get("name") or f"file{i}")
                blob = base64.b64decode(item.get("data") or "")
                target = Path(tmpdir) / name
                target.write_bytes(blob)
                paths.append(str(target))
        try:
            return client().send(
                to=to,
                cc=[x.strip() for x in (payload.get("cc") or []) if str(x).strip()],
                bcc=[x.strip() for x in (payload.get("bcc") or []) if str(x).strip()],
                subject=(payload.get("subject") or "").strip(),
                body=payload.get("body") or "",
                html=bool(payload.get("html")),
                attachments=paths,
                reply_to_uid=(payload.get("reply_to_uid") or "").strip() or None,
            )
        except GmailError as exc:
            raise ApiError(str(exc)) from exc
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- HTTP

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "gmail-imap-ui/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {fmt % args}\n")

    def _json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg: Any, status: int = 400) -> None:
        self._json({"ok": False, "error": str(msg)}, status)

    def _authorized(self, query: dict) -> bool:
        if self.headers.get("X-App-Token") == TOKEN:
            return True
        return secrets.compare_digest((query.get("t") or [""])[0], TOKEN)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            raise ApiError("Request body is not valid JSON") from exc

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path in STATIC:
            name, ctype = STATIC[path]
            file = HERE / name
            if not file.exists():
                return self._err(f"{name} is missing", 404)
            body = file.read_bytes()
            if name == "index.html":
                body = body.replace(b"__APP_TOKEN__", TOKEN.encode())
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if not path.startswith("/api/"):
            return self._err("not found", 404)
        if not self._authorized(query):
            return self._err("bad token", 403)

        try:
            if path == "/api/status":
                return self._json(status_payload())
            if path == "/api/labels":
                return self._json({"labels": client().labels()})
            if path == "/api/messages":
                q = (query.get("q") or [""])[0]
                limit = max(1, min(int((query.get("limit") or ["30"])[0]), 200))
                mailbox = (query.get("mailbox") or [None])[0]
                return self._json(list_messages(q, limit, mailbox))
            if path == "/api/message":
                uid = (query.get("uid") or [""])[0]
                if not uid:
                    raise ApiError("uid is required")
                mailbox = (query.get("mailbox") or [None])[0]
                return self._json(get_message(uid, mailbox))
            if path == "/api/attachment":
                uid = (query.get("uid") or [""])[0]
                idx = int((query.get("idx") or ["-1"])[0])
                if not uid or idx < 0:
                    raise ApiError("uid and idx are required")
                filename, ctype, blob = get_attachment(uid, idx, None)
                self.send_response(200)
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename*=UTF-8''" + quote(filename),
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(blob)
                return
            return self._err("not found", 404)
        except ApiError as exc:
            return self._err(exc, 400)
        except GmailError as exc:
            return self._err(exc, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._err(f"Server error: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if not self._authorized(query):
            return self._err("bad token", 403)
        try:
            payload = self._read_json()
            if path == "/api/config":
                return self._json(save_config(
                    payload.get("address") or "", payload.get("app_password") or ""
                ))
            if path == "/api/send":
                return self._json(send_mail(payload))
            if path == "/api/reconnect":
                drop_clients()
                return self._json(status_payload())
            return self._err("not found", 404)
        except ApiError as exc:
            return self._err(exc, 400)
        except GmailError as exc:
            return self._err(exc, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._err(f"Server error: {exc}", 500)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gmail-imap-ui",
                                     description="Local Gmail web UI")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    httpd = None
    port = args.port
    for _ in range(12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        print("No free port found.", file=sys.stderr)
        raise SystemExit(1)

    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print("  gmail-imap — local web UI")
    print("=" * 56)
    print(f"  {url}")
    print(f"  config: {config_file()}")
    print("  localhost only; closing this window stops it")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        drop_clients()
        httpd.server_close()


if __name__ == "__main__":
    main()
