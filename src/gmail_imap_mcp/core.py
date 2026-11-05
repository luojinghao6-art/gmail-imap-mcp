"""Core Gmail client: IMAP/SMTP over an App Password.

No OAuth, no Google Cloud project, no third-party relay. Standard library only.

The public surface is :class:`MailClient`. Everything else here is a helper that
the CLI, the MCP server and the web UI share, so behaviour cannot drift between
interfaces.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import html as html_mod
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import threading
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "GmailError",
    "AuthError",
    "MailClient",
    "Settings",
    "load_settings",
    "save_settings",
    "has_credentials",
    "config_file",
    "decode_mime",
    "imap_utf7_decode",
    "html_to_text",
    "is_read_only",
    "audit_log_path",
    "resolve_attachment",
    "default_download_dir",
    "resolve_download_dir",
    "write_attachment",
]

APP_NAME = "gmail-imap-mcp"
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_SSL_PORT = 465
DEFAULT_TIMEOUT = 45

# Write-path policy. Environment flags keep one core safe for both a personal
# CLI and a shared deployment without forking behaviour.
READ_ONLY_ENV = "GMAIL_MCP_READ_ONLY"
ATTACHMENT_DIRS_ENV = "GMAIL_MCP_ATTACHMENT_DIRS"
DOWNLOAD_DIRS_ENV = "GMAIL_MCP_DOWNLOAD_DIRS"

HEADER_SPEC = (
    "(UID RFC822.SIZE FLAGS X-GM-MSGID X-GM-LABELS "
    "BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID REFERENCES)])"
)


class GmailError(Exception):
    """Anything that went wrong talking to Gmail, with a message worth showing."""


class AuthError(GmailError):
    """Bad App Password, or 2-Step Verification is off."""


# ---------------------------------------------------------------- settings

class Settings:
    __slots__ = ("address", "app_password", "proxy")

    def __init__(self, address: str, app_password: str, proxy: str | None = None):
        self.address = address.strip()
        # Google shows the password with spaces; drop them.
        self.app_password = app_password.replace(" ", "").strip()
        self.proxy = proxy or os.environ.get("GMAIL_PROXY") or None

    def __repr__(self) -> str:  # never leak the password into logs
        return f"Settings(address={self.address!r}, app_password='***')"


def _config_home() -> Path:
    override = os.environ.get("GMAIL_AGENT_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def config_file() -> Path:
    return _config_home() / "credentials.json"


def _legacy_config() -> Path:
    """Where the original Claude-skill version kept its credentials."""
    return Path.home() / ".claude" / "skills" / "gmail" / ".credentials.json"


def load_settings(required: bool = True) -> Settings | None:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()

    if not (address and password):
        for path in (config_file(), _legacy_config()):
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise GmailError(f"Cannot read credentials at {path}: {exc}") from exc
            address = address or str(data.get("address", "")).strip()
            password = password or str(data.get("app_password", "")).strip()
            if address and password:
                break

    if not address or not password:
        if not required:
            return None
        raise AuthError(
            "No credentials found. Run `gmail-imap config` (or set "
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD).\n"
            "Create an App Password at https://myaccount.google.com/apppasswords "
            "— 2-Step Verification must be on first."
        )
    return Settings(address, password)


def save_settings(address: str, app_password: str) -> Path:
    address = (address or "").strip()
    if "@" not in address:
        raise GmailError("That does not look like an email address.")
    clean = (app_password or "").replace(" ", "").strip()
    if len(clean) != 16:
        raise GmailError(
            f"An App Password is 16 characters, got {len(clean)}. "
            "You probably pasted the account password instead."
        )
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"address": address, "app_password": clean}, indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def has_credentials() -> bool:
    try:
        return load_settings(required=False) is not None
    except GmailError:
        return False


# ---------------------------------------------------------------- policy
# A prompt that says "confirm before sending" is not a control; these are.

_TRUE_VALUES = {"1", "true", "yes", "on"}


def is_read_only() -> bool:
    """True when GMAIL_MCP_READ_ONLY is set to a truthy value."""
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in _TRUE_VALUES


def audit_log_path() -> Path:
    """JSONL log of every outbound write, next to the credentials."""
    return _config_home() / "audit.log"


def _audit(event: str, **fields: Any) -> None:
    """Append one JSON line to the audit log. Auditing never breaks mail."""
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
    }
    row.update(fields)
    try:
        path = audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _ensure_writable(action: str) -> None:
    if is_read_only():
        _audit("blocked", action=action, reason=READ_ONLY_ENV)
        raise GmailError(
            f"{action} is disabled because {READ_ONLY_ENV} is set. "
            "Unset it to allow outbound mail."
        )


_SECRET_FILENAMES = {
    ".credentials.json", "credentials.json", ".env", ".netrc", ".npmrc",
    ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks"}


def _looks_secret(path: Path) -> bool:
    name = path.name.lower()
    if name in _SECRET_FILENAMES or name.startswith(".env."):
        return True
    if path.suffix.lower() in _SECRET_SUFFIXES:
        return True
    if ".ssh" in path.parts:
        return True
    try:
        resolved = path.resolve()
        if resolved in {config_file().resolve(), audit_log_path().resolve()}:
            return True  # the agent's own credentials or audit trail
    except OSError:
        pass
    return False


def resolve_attachment(raw: str) -> Path:
    """Validate an outbound attachment path.

    The file must exist; if GMAIL_MCP_ATTACHMENT_DIRS is set it must live under
    one of those roots; credential-looking files are always refused so a
    poisoned context cannot exfiltrate the App Password.
    """
    path = Path(raw).expanduser()
    if not path.is_file():
        raise GmailError(f"Attachment not found: {path}")
    resolved = path.resolve()
    roots = [p for p in os.environ.get(ATTACHMENT_DIRS_ENV, "").split(os.pathsep) if p]
    if roots:
        for root in roots:
            try:
                resolved.relative_to(Path(root).expanduser().resolve())
                break
            except ValueError:
                continue
        else:
            raise GmailError(
                f"Attachment is outside {ATTACHMENT_DIRS_ENV}: {resolved}"
            )
    if _looks_secret(resolved):
        raise GmailError(f"Refusing to send a credential or secret file: {resolved}")
    return resolved


# ---------------------------------------------------------------- downloads

def default_download_dir() -> Path:
    """Where inbound attachments go when no directory is named."""
    return Path.home() / "Downloads" / APP_NAME


def _allowed_download_roots() -> list[Path]:
    """The default download folder plus anything in GMAIL_MCP_DOWNLOAD_DIRS."""
    raw = [str(default_download_dir())]
    raw.extend(
        chunk for chunk in os.environ.get(DOWNLOAD_DIRS_ENV, "").split(os.pathsep)
        if chunk.strip()
    )
    roots: list[Path] = []
    for item in raw:
        try:
            resolved = Path(item.strip()).expanduser().resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def resolve_download_dir(raw: str | Path | None = None) -> Path:
    """Validate and create a directory for inbound attachments.

    Mail is untrusted input, so an agent must never be talked into writing a
    payload into an autostart folder: inbound files are confined to
    ``~/Downloads/gmail-imap`` plus the roots named in
    ``GMAIL_MCP_DOWNLOAD_DIRS`` (``os.pathsep``-separated).
    """
    target = default_download_dir() if raw in (None, "") else Path(raw).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise GmailError(f"Cannot use {target}: {exc}") from exc

    roots = _allowed_download_roots()
    for root in roots:
        if resolved == root or root in resolved.parents:
            break
    else:
        allowed = ", ".join(str(r) for r in roots)
        raise GmailError(
            f"Refusing to save attachments outside the download folders: {resolved}. "
            f"Allowed: {allowed}. Set {DOWNLOAD_DIRS_ENV} to add a folder."
        )

    if resolved.exists() and not resolved.is_dir():
        raise GmailError(f"Not a directory: {resolved}")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GmailError(f"Cannot create {resolved}: {exc}") from exc
    return resolved


# ---------------------------------------------------------------- decoding

def decode_mime(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
    except Exception:
        return raw
    out: list[str] = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            for candidate in (enc, "utf-8", "gb18030", "latin-1"):
                if not candidate:
                    continue
                try:
                    out.append(chunk.decode(candidate, errors="replace"))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def imap_utf7_decode(value: str) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501 §5.1.3).

    Gmail localises folder names, so Chinese users see ``[Gmail]/&XfJSIJZkkK5O9g-``
    unless this is decoded.
    """
    if not value or "&" not in value:
        return value

    def _decode_seq(seq: str) -> str:
        raw = seq.replace(",", "/")
        raw += "=" * (-len(raw) % 4)
        return base64.b64decode(raw).decode("utf-16-be", errors="replace")

    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = value.find("-", i + 1)
        if end < 0:
            out.append(value[i:])
            break
        body = value[i + 1:end]
        if not body:
            out.append("&")
        else:
            try:
                out.append(_decode_seq(body))
            except Exception:
                out.append(value[i:end + 1])
        i = end + 1
    return "".join(out)


def html_to_text(rich: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", rich)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(text).strip()


def _extract_labels(meta: bytes) -> list[str]:
    """Pull X-GM-LABELS out of a FETCH response.

    Labels are quoted and may contain parentheses, so lazy regexes are wrong.
    """
    key = b"X-GM-LABELS ("
    start = meta.find(key)
    if start < 0:
        return []
    j = start + len(key)
    depth, in_quote = 1, False
    while j < len(meta):
        ch = meta[j:j + 1]
        if in_quote:
            if ch == b"\\":
                j += 2
                continue
            if ch == b'"':
                in_quote = False
        elif ch == b'"':
            in_quote = True
        elif ch == b"(":
            depth += 1
        elif ch == b")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    inner = meta[start + len(key):j].decode("utf-8", errors="replace")
    return [
        m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', inner)
    ]


def _parse_msgid(meta: bytes) -> str | None:
    """X-GM-MSGID is stable across mailboxes; UIDs are not."""
    match = re.search(b"X-GM-MSGID ([0-9]+)", meta)
    return match.group(1).decode() if match else None


def _parse_header_item(meta: bytes, raw_headers: bytes) -> dict[str, Any]:
    uid = None
    match = re.search(rb"\bUID (\d+)", meta)
    if match:
        uid = match.group(1).decode()

    size = None
    match = re.search(rb"RFC822\.SIZE (\d+)", meta)
    if match:
        size = int(match.group(1))

    flags: list[str] = []
    match = re.search(rb"FLAGS \(([^)]*)\)", meta)
    if match:
        flags = [f.decode("ascii", errors="replace") for f in match.group(1).split()]

    msg = BytesParser(policy=policy.default).parsebytes(raw_headers)
    return {
        "uid": uid,
        "msgid": _parse_msgid(meta),
        "size": size,
        "unread": "\\Seen" not in flags,
        "flags": flags,
        "from": decode_mime(msg.get("From")),
        "to": decode_mime(msg.get("To")),
        "cc": decode_mime(msg.get("Cc")),
        "subject": decode_mime(msg.get("Subject")),
        "date": decode_mime(msg.get("Date")),
        "message_id": (msg.get("Message-ID") or "").strip(),
        "references": (msg.get("References") or "").strip(),
        "labels": _extract_labels(meta),
    }


# ---------------------------------------------------------------- client

class MailClient:
    """One Gmail account, IMAP for reading, SMTP for sending.

    Thread-safe: a single IMAP connection is serialised behind a lock, because
    IMAP is stateful and interleaving commands corrupts responses.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()
        self._imap: imaplib.IMAP4_SSL | None = None
        self._all_mail: str | None = None
        self._mailbox_names: dict[str, str] | None = None
        self._lock = threading.RLock()

    # -- plumbing -------------------------------------------------

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def _socks_socket(self, host: str, port: int) -> Any:
        proxy = self.settings.proxy
        if not proxy:
            return None
        try:
            import socks  # PySocks
        except ImportError as exc:
            raise GmailError(
                "GMAIL_PROXY is set but PySocks is missing: pip install PySocks"
            ) from exc
        match = re.match(r"^(?:socks5h?|socks4)://([^:/]+):(\d+)$", proxy)
        if not match:
            raise GmailError(
                f"GMAIL_PROXY must look like socks5://127.0.0.1:7890, got {proxy!r}"
            )
        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, match.group(1), int(match.group(2)))
        sock.settimeout(DEFAULT_TIMEOUT)
        sock.connect((host, port))
        return sock

    def _connect_imap(self) -> imaplib.IMAP4_SSL:
        try:
            tunnel = self._socks_socket(IMAP_HOST, IMAP_PORT)
        except OSError as exc:
            raise GmailError(f"Cannot reach {IMAP_HOST}:{IMAP_PORT}: {exc}") from exc

        if tunnel is not None:
            sock = self._ssl_context().wrap_socket(tunnel, server_hostname=IMAP_HOST)
            imap = imaplib.IMAP4()
            imap.sock = sock
            imap.file = sock.makefile("rb")
            imap._connect()  # noqa: SLF001 - documented way to adopt a socket
        else:
            try:
                imap = imaplib.IMAP4_SSL(
                    IMAP_HOST, IMAP_PORT,
                    ssl_context=self._ssl_context(), timeout=DEFAULT_TIMEOUT,
                )
            except OSError as exc:
                raise GmailError(
                    f"Cannot reach {IMAP_HOST}:{IMAP_PORT}: {exc}\n"
                    "In mainland China you may need GMAIL_PROXY=socks5://127.0.0.1:7890"
                ) from exc

        try:
            imap.login(self.settings.address, self.settings.app_password)
        except imaplib.IMAP4.error as exc:
            raise AuthError(_auth_hint("IMAP", exc)) from exc
        return imap

    def imap(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            self._imap = self._connect_imap()
            self._all_mail = None
            self._mailbox_names = None
        return self._imap

    def close(self) -> None:
        with self._lock:
            try:
                if self._imap is not None:
                    self._imap.logout()
            except Exception:
                pass
            self._imap = None
            self._all_mail = None
            self._mailbox_names = None

    def _run(self, fn):
        """Run an IMAP operation, reconnecting once if the connection died."""
        with self._lock:
            last: Exception | None = None
            for attempt in (1, 2):
                try:
                    return fn(self.imap())
                except GmailError:
                    raise
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                    last = exc
                    self.close()
                    if attempt == 2:
                        break
            raise GmailError(f"IMAP failure: {last}") from last

    # -- mailboxes ------------------------------------------------

    def _find_all_mail(self, imap: imaplib.IMAP4_SSL) -> str:
        """Gmail localises '[Gmail]/All Mail', so find it by the \\All flag."""
        try:
            status, data = imap.list('""', "*")
        except Exception:
            return "INBOX"
        if status != "OK":
            return "INBOX"
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            text = line.decode("utf-8", errors="replace")
            if "\\All" not in text:
                continue
            match = re.search(r'\)\s+"[^"]*"\s+(.+)$', text)
            if not match:
                continue
            name = match.group(1).strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            return name
        return "INBOX"

    def _quote_mailbox(self, name: str) -> str:
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _mailbox_map(self, imap: imaplib.IMAP4_SSL) -> dict[str, str]:
        """Map decoded and raw mailbox names to the raw IMAP name."""
        if self._mailbox_names is None:
            mapping: dict[str, str] = {}
            try:
                status, data = imap.list('""', "*")
            except Exception:
                status, data = "NO", []
            if status == "OK":
                for line in data or []:
                    if not isinstance(line, bytes):
                        continue
                    text = line.decode("utf-8", errors="replace")
                    match = re.search(r'\(([^)]*)\)\s+"[^"]*"\s+(.+)$', text)
                    if not match or "\\Noselect" in match.group(1):
                        continue
                    raw = match.group(2).strip()
                    if raw.startswith('"') and raw.endswith('"'):
                        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                    mapping.setdefault(imap_utf7_decode(raw), raw)
                    mapping.setdefault(raw, raw)
                self._mailbox_names = mapping
            return mapping
        return self._mailbox_names

    def _select(self, imap: imaplib.IMAP4_SSL, mailbox: str | None) -> str:
        """Select a mailbox and return its raw IMAP name.

        An explicit mailbox that cannot be opened raises. Silently falling back
        to INBOX would change the meaning of every UID the caller already holds.
        """
        if mailbox:
            raw = self._mailbox_map(imap).get(mailbox, mailbox)
            try:
                status, data = imap.select(self._quote_mailbox(raw), readonly=True)
            except imaplib.IMAP4.error as exc:
                raise GmailError(f"Cannot open mailbox {mailbox!r}: {exc}") from exc
            if status != "OK":
                raise GmailError(f"Cannot open mailbox {mailbox!r}: {data}")
            return raw

        if self._all_mail is None:
            self._all_mail = self._find_all_mail(imap)
        box = self._all_mail
        try:
            status, _ = imap.select(self._quote_mailbox(box), readonly=True)
        except imaplib.IMAP4.error:
            status = "NO"
        if status != "OK":
            box = "INBOX"
            imap.select(self._quote_mailbox("INBOX"), readonly=True)
        return box

    def _resolve_ref(self, imap: imaplib.IMAP4_SSL, uid: str | None = None,
                     mailbox: str | None = None,
                     msgid: str | None = None) -> tuple[str, str]:
        """Return (raw mailbox, uid) for a message reference.

        X-GM-MSGID is account-wide, so it is always safe. A bare UID is only
        meaningful inside one mailbox; without one we use All Mail, which is
        the mailbox every search hit comes from unless the caller narrowed it.
        """
        if msgid:
            box = self._select(imap, None)
            status, data = imap.uid("SEARCH", "X-GM-MSGID", str(msgid).strip())
            if status != "OK":
                raise GmailError(f"Cannot look up message id {msgid}: {data}")
            found = (data[0] or b"").split()
            if not found:
                raise GmailError(f"No message with X-GM-MSGID {msgid}")
            return box, found[-1].decode()
        real = str(uid or "").strip()
        if not real:
            raise GmailError("uid (or msgid) is required")
        return self._select(imap, mailbox), real

    def labels(self) -> list[dict[str, str]]:
        def run(imap: imaplib.IMAP4_SSL):
            status, data = imap.list('""', "*")
            if status != "OK":
                raise GmailError(f"Cannot list mailboxes: {data}")
            rows = []
            for line in data or []:
                if not isinstance(line, bytes):
                    continue
                text = line.decode("utf-8", errors="replace")
                match = re.search(r"\(([^)]*)\)\s+\"[^\"]*\"\s+(.+)$", text)
                if not match:
                    continue
                flags = match.group(1).strip()
                if "\\Noselect" in flags:
                    continue
                raw = match.group(2).strip().strip('"').replace('\\"', '"')
                rows.append({"name": imap_utf7_decode(raw), "raw": raw, "flags": flags})
            rows.sort(key=_folder_rank)
            return rows

        return self._run(run)

    # -- reading --------------------------------------------------

    def search(self, query: str, limit: int = 10,
               mailbox: str | None = None) -> dict[str, Any]:
        """Search with Gmail's own syntax (X-GM-RAW), not crippled IMAP SEARCH."""
        limit = max(1, min(int(limit), 200))

        def run(imap: imaplib.IMAP4_SSL):
            box = self._select(imap, mailbox)
            escaped = query.replace("\\", "\\\\").replace('"', '\\"')
            status, data = imap.uid("SEARCH", "X-GM-RAW", f'"{escaped}"')
            if status != "OK":
                raise GmailError(f"Search failed: {data}")
            uids = (data[0] or b"").split()
            picked = [u.decode() for u in uids[-limit:]][::-1]  # newest first
            rows = self._fetch_headers(imap, picked)
            box_name = imap_utf7_decode(box)
            for row in rows:
                row["mailbox"] = box_name
                row["mailbox_raw"] = box
            return {"mailbox": box_name, "mailbox_raw": box, "query": query,
                    "count": len(rows), "messages": rows}

        return self._run(run)

    def _fetch_headers(self, imap: imaplib.IMAP4_SSL, uids: Sequence[str]) -> list[dict]:
        if not uids:
            return []
        # One round trip for the whole page. Doing it per message is 2N round
        # trips and takes ~25s for 30 messages.
        status, data = imap.uid("FETCH", ",".join(uids), HEADER_SPEC)
        if status != "OK":
            raise GmailError(f"Cannot fetch headers: {data}")
        rows = []
        for item in data or []:
            if isinstance(item, tuple):
                rows.append(_parse_header_item(item[0] or b"", item[1] or b""))
        order = {str(uid): i for i, uid in enumerate(uids)}
        rows.sort(key=lambda r: order.get(str(r.get("uid")), 10 ** 9))
        return rows

    def read(self, uid: str | None = None, mailbox: str | None = None,
             save_attachments: str | None = None,
             msgid: str | None = None) -> dict[str, Any]:
        """Read one message.

        Pass `mailbox` alongside a UID from a narrowed search, or pass `msgid`
        (X-GM-MSGID) when the reference has to survive crossing mailboxes.
        """
        def run(imap: imaplib.IMAP4_SSL):
            box, real_uid = self._resolve_ref(imap, uid, mailbox, msgid)
            status, data = imap.uid("FETCH", real_uid, "(RFC822 X-GM-MSGID)")
            item = next((x for x in data or [] if isinstance(x, tuple)), None)
            if status != "OK" or item is None:
                raise GmailError(
                    f"No message with uid {real_uid} in {imap_utf7_decode(box)}"
                )
            meta, raw = item
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            plain, rich = _extract_body(msg)
            attachments = _attachment_meta(msg)
            result = {
                "uid": real_uid,
                "msgid": _parse_msgid(meta),
                "mailbox": imap_utf7_decode(box),
                "mailbox_raw": box,
                "from": decode_mime(msg.get("From")),
                "to": decode_mime(msg.get("To")),
                "cc": decode_mime(msg.get("Cc")),
                "subject": decode_mime(msg.get("Subject")),
                "date": decode_mime(msg.get("Date")),
                "message_id": (msg.get("Message-ID") or "").strip(),
                "text": plain or html_to_text(rich),
                "html": rich,
                "attachments": attachments,
            }
            if save_attachments:
                result["saved"] = _save_attachments(msg, Path(save_attachments))
            return result

        return self._run(run)

    def attachment(self, uid: str | None = None, index: int | None = None,
                   mailbox: str | None = None,
                   msgid: str | None = None) -> tuple[str, str, bytes]:
        if index is None:
            raise GmailError("index is required")
        index = int(index)

        def run(imap: imaplib.IMAP4_SSL):
            box, real_uid = self._resolve_ref(imap, uid, mailbox, msgid)
            status, data = imap.uid("FETCH", real_uid, "(RFC822)")
            item = next((x for x in data or [] if isinstance(x, tuple)), None)
            if status != "OK" or item is None:
                raise GmailError(
                    f"No message with uid {real_uid} in {imap_utf7_decode(box)}"
                )
            msg = BytesParser(policy=policy.default).parsebytes(item[1])
            parts = [p for p in msg.walk() if p.get_filename()]
            if index < 0 or index >= len(parts):
                raise GmailError(
                    f"Attachment index {index} out of range, message has {len(parts)}"
                )
            part = parts[index]
            name = _safe_filename(decode_mime(part.get_filename()) or "attachment.bin")
            return name, part.get_content_type(), part.get_payload(decode=True) or b""

        return self._run(run)

    # -- sending --------------------------------------------------

    def send(
        self,
        to: Iterable[str],
        subject: str,
        body: str,
        cc: Iterable[str] | None = None,
        bcc: Iterable[str] | None = None,
        html: bool = False,
        attachments: Iterable[str] | None = None,
        reply_to_uid: str | None = None,
        reply_mailbox: str | None = None,
        reply_msgid: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        to_list = _clean_addresses(to)
        if not to_list:
            raise GmailError("At least one recipient is required")
        cc_list = _clean_addresses(cc)
        bcc_list = _clean_addresses(bcc)
        subject = (subject or "").strip()
        body = body or ""
        if not subject and not body:
            raise GmailError("Both subject and body are empty")

        action = "reply" if (reply_to_uid or reply_msgid) else "send"

        msg = EmailMessage()
        msg["From"] = self.settings.address
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        if bcc_list:
            msg["Bcc"] = ", ".join(bcc_list)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.settings.address.split("@")[-1])

        if html:
            msg.set_content(html_to_text(body) or "(HTML message)")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        paths: list[Path] = []
        for raw_path in attachments or []:
            path = resolve_attachment(raw_path)
            paths.append(path)
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(
                path.read_bytes(), maintype=maintype, subtype=subtype,
                filename=path.name,
            )

        recipients = to_list + cc_list + bcc_list
        if dry_run:
            _audit("dry_run", action=action, to=recipients, subject=subject,
                   attachments=[p.name for p in paths])
            return {
                "ok": True,
                "dry_run": True,
                "to": recipients,
                "subject": subject,
                "attachments": [p.name for p in paths],
            }

        _ensure_writable(action)
        if reply_to_uid or reply_msgid:
            self._apply_threading(msg, reply_to_uid, reply_mailbox, reply_msgid)

        try:
            refused = self._smtp_send(msg, recipients)
        except GmailError as exc:
            _audit("send_error", action=action, to=recipients, subject=subject,
                   error=str(exc))
            raise
        if refused:
            _audit("send_error", action=action, to=recipients, subject=subject,
                   error=f"refused: {refused}")
            raise GmailError(f"Recipients refused: {refused}")
        _audit(action, to=to_list, cc=cc_list, bcc=bcc_list, subject=subject,
               message_id=str(msg["Message-ID"]),
               attachments=[p.name for p in paths],
               reply_to=reply_msgid or reply_to_uid or None)
        return {
            "ok": True,
            "message_id": msg["Message-ID"],
            "to": recipients,
            "subject": subject,
            "attachments": [p.name for p in paths],
        }

    def reply(self, uid: str | None = None, body: str = "",
              reply_all: bool = False, html: bool = False,
              mailbox: str | None = None,
              msgid: str | None = None) -> dict[str, Any]:
        original = self.read(uid, mailbox=mailbox, msgid=msgid)
        to = _address_of(original["from"])
        if not to:
            raise GmailError("Cannot work out the original sender")
        cc: list[str] = []
        if reply_all:
            seen = {to.lower(), self.settings.address.lower()}
            for field in ("to", "cc"):
                for addr in _clean_addresses(
                    _address_of(part) for part in str(original.get(field) or "").split(",")
                ):
                    if addr.lower() not in seen:
                        seen.add(addr.lower())
                        cc.append(addr)
        subject = original["subject"] or ""
        if not re.match(r"(?i)^re:", subject):
            subject = f"Re: {subject}"
        quoted = "\n".join(f"> {line}" for line in (original.get("text") or "").splitlines())
        full_body = f"{body}\n\n-------- Original message --------\n{quoted}".strip()
        return self.send(
            to=[to], cc=cc, subject=subject, body=full_body, html=html,
            reply_to_uid=original.get("uid"),
            reply_mailbox=original.get("mailbox_raw"),
            reply_msgid=original.get("msgid"),
        )

    def _apply_threading(self, msg: EmailMessage, uid: str | None = None,
                         mailbox: str | None = None,
                         msgid: str | None = None) -> None:
        def run(imap: imaplib.IMAP4_SSL):
            _, real_uid = self._resolve_ref(imap, uid, mailbox, msgid)
            status, data = imap.uid(
                "FETCH", real_uid,
                "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES)])",
            )
            item = next((x for x in data or [] if isinstance(x, tuple)), None)
            if status != "OK" or item is None:
                return
            head = BytesParser(policy=policy.default).parsebytes(item[1])
            original_id = (head.get("Message-ID") or "").strip()
            if not original_id:
                return
            msg["In-Reply-To"] = original_id
            msg["References"] = f"{(head.get('References') or '').strip()} {original_id}".strip()

        self._run(run)

    def _smtp_send(self, msg: EmailMessage, recipients: list[str]) -> dict:
        try:
            tunnel = self._socks_socket(SMTP_HOST, SMTP_SSL_PORT)
        except OSError as exc:
            raise GmailError(f"Cannot reach {SMTP_HOST}:{SMTP_SSL_PORT}: {exc}") from exc

        try:
            if tunnel is not None:
                sock = self._ssl_context().wrap_socket(tunnel, server_hostname=SMTP_HOST)
                smtp = smtplib.SMTP()
                smtp._host = SMTP_HOST  # noqa: SLF001 - needed for STARTTLS naming
                smtp.sock = sock
                smtp.file = sock.makefile("rb")
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP_SSL(
                    SMTP_HOST, SMTP_SSL_PORT,
                    context=self._ssl_context(), timeout=DEFAULT_TIMEOUT,
                )
                smtp.ehlo()
            try:
                smtp.login(self.settings.address, self.settings.app_password)
            except smtplib.SMTPAuthenticationError as exc:
                raise AuthError(_auth_hint("SMTP", exc)) from exc
        except smtplib.SMTPException as exc:
            raise GmailError(f"SMTP error: {exc}") from exc

        try:
            return smtp.send_message(
                msg, from_addr=self.settings.address, to_addrs=recipients
            )
        except (smtplib.SMTPException, OSError) as exc:
            raise GmailError(f"Send failed: {exc}") from exc
        finally:
            try:
                smtp.quit()
            except Exception:
                pass


# ---------------------------------------------------------------- helpers

def _auth_hint(where: str, exc: Exception) -> str:
    return (
        f"{where} login failed: {exc}\n"
        "Usual causes:\n"
        "  - you pasted the account password instead of an App Password\n"
        "  - 2-Step Verification is off (App Passwords do not exist without it)\n"
        "  - the App Password was revoked\n"
        "Get a new one at https://myaccount.google.com/apppasswords"
    )


def _folder_rank(row: dict) -> tuple:
    if str(row.get("raw", "")).upper() == "INBOX":
        return (0, "")
    for order, flag in enumerate(
        ("\\All", "\\Flagged", "\\Sent", "\\Drafts", "\\Important", "\\Junk", "\\Trash"),
        start=1,
    ):
        if flag in row.get("flags", ""):
            return (order, "")
    return (99, str(row.get("name", "")).lower())


def _clean_addresses(values: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for piece in re.split(r"[,;\n]", str(value)):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def _address_of(header: str) -> str:
    match = re.search(r"<([^>]+)>", header or "")
    return (match.group(1) if match else (header or "")).strip()


_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _safe_filename(name: str) -> str:
    """Turn a sender-supplied filename into one bare, writable path part.

    Separators and control characters are replaced, `.`/`..` cannot survive,
    Windows device names get a prefix, and the result is clipped so the OS
    still accepts it.
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "")
    cleaned = cleaned.strip().strip(".").strip()
    if not cleaned or set(cleaned) <= {"_"}:
        return "attachment.bin"
    stem, dot, suffix = cleaned.rpartition(".")
    if not dot or not stem:
        stem, suffix = cleaned, ""
    if stem.lower() in _RESERVED_NAMES:
        stem = "_" + stem
    suffix = suffix[:20]
    return (stem[:120] + ("." + suffix if suffix else "")) or "attachment.bin"


def _extract_body(msg) -> tuple[str, str]:
    plain = rich = ""
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in (part.get("Content-Disposition") or "").lower():
                continue
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                content = payload.decode(part.get_content_charset() or "utf-8",
                                         errors="replace")
            if ctype == "text/plain" and not plain:
                plain = content
            elif ctype == "text/html" and not rich:
                rich = content
    else:
        try:
            content = msg.get_content()
        except Exception:
            content = (msg.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            rich = content
        else:
            plain = content
    return plain, rich


def _attachment_meta(msg) -> list[dict[str, Any]]:
    rows = []
    for index, part in enumerate(p for p in msg.walk() if p.get_filename()):
        try:
            size = len(part.get_payload(decode=True) or b"")
        except Exception:
            size = 0
        rows.append({
            "index": index,
            "name": decode_mime(part.get_filename()),
            "size": size,
            "type": part.get_content_type(),
        })
    return rows


def _free_path(out_dir: Path, name: str) -> Path:
    """First unused ``name``, ``name_1``, ``name_2`` … inside out_dir."""
    target = out_dir / name
    if not target.exists() and not target.is_symlink():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    for counter in range(1, 10000):
        candidate = out_dir / f"{stem}_{counter}{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise GmailError(f"Too many files named like {name} in {out_dir}")


def write_attachment(out_dir: Path, name: str, blob: bytes) -> Path:
    """Write one inbound attachment without clobbering an existing file.

    The file is opened with ``O_EXCL`` semantics ("xb"), so even a file that
    appears between the name check and the write cannot be overwritten.
    """
    out_dir = resolve_download_dir(out_dir)
    safe = _safe_filename(decode_mime(name))
    target = _free_path(out_dir, safe)
    try:
        with open(target, "xb") as fh:
            fh.write(blob)
    except FileExistsError:
        target = _free_path(out_dir, safe)
        with open(target, "xb") as fh:
            fh.write(blob)
    return target


def _save_attachments(msg, out_dir: Path) -> list[str]:
    out_dir = resolve_download_dir(out_dir)
    saved = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        target = write_attachment(
            out_dir, filename, part.get_payload(decode=True) or b""
        )
        saved.append(str(target))
    return saved
