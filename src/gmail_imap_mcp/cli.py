"""Command-line interface.

Kept deliberately thin: everything lives in :mod:`gmail_imap_mcp.core`, so an agent
can call this CLI or the MCP server and get identical behaviour.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .core import (
    AuthError,
    GmailError,
    MailClient,
    config_file,
    decode_mime,
    load_settings,
    save_settings,
)


def _emit(payload, as_json: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _human_message(row: dict) -> str:
    mark = "●" if row.get("unread") else " "
    size = row.get("size") or 0
    kb = f"{size // 1024}KB" if size else "?"
    labels = ", ".join(
        l for l in row.get("labels", []) if l not in ("\\Inbox",)
    )
    lines = [
        f"UID {row.get('uid', '?'):<10} {mark} {str(row.get('date', ''))[:31]:<32} {kb:>7}",
        f"  Subject: {row.get('subject') or '(no subject)'}",
        f"  From:    {row.get('from', '')}",
    ]
    if labels:
        lines.append(f"  Labels:  {labels}")
    return "\n".join(lines)


def cmd_config(args) -> int:
    address = (args.address or "").strip()
    if not address:
        try:
            address = input("Gmail address: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("No address given.", file=sys.stderr)
            return 2

    password = (args.app_password or "").strip()
    if not password:
        if not sys.stdin.isatty():
            print("Not a TTY. Pass --app-password, or run this in a terminal.",
                  file=sys.stderr)
            return 2
        password = getpass.getpass("App Password (16 chars, hidden): ").strip()

    path = save_settings(address, password)
    print(f"Saved to {path}")
    print("Next: gmail-imap status")
    return 0


def cmd_status(args) -> int:
    settings = load_settings()
    client = MailClient(settings)
    try:
        rows = client.labels()
    finally:
        client.close()
    payload = {
        "configured": True,
        "address": settings.address,
        "connected": True,
        "mailboxes": [r["name"] for r in rows],
    }
    if args.json:
        _emit(payload, True)
    else:
        print(f"Account:  {settings.address}")
        print(f"Config:   {config_file()}")
        print(f"Folders:  {len(rows)}")
        for row in rows[:12]:
            print(f"  - {row['name']}")
    return 0


def cmd_send(args) -> int:
    client = MailClient(load_settings())
    try:
        result = client.send(
            to=args.to,
            cc=args.cc,
            bcc=args.bcc,
            subject=args.subject,
            body=args.body or "",
            html=args.html,
            attachments=args.attach,
            reply_to_uid=args.reply_to_uid,
        )
    finally:
        client.close()
    if args.dry_run:
        print("(dry run) would send to:", ", ".join(result["to"]))
        return 0
    _emit(result, True)
    return 0


def cmd_search(args) -> int:
    client = MailClient(load_settings())
    try:
        data = client.search(args.query, limit=args.limit, mailbox=args.mailbox)
    finally:
        client.close()
    if args.json:
        _emit(data, True)
        return 0
    print(f"[{data['mailbox']}] {data['count']} match(es) for {data['query']!r}\n")
    if not data["messages"]:
        print("Nothing found.")
        return 0
    for row in data["messages"]:
        print(_human_message(row))
        print()
    return 0


def cmd_read(args) -> int:
    client = MailClient(load_settings())
    try:
        data = client.read(
            args.uid,
            mailbox=args.mailbox,
            save_attachments=args.save_attachments,
        )
    finally:
        client.close()
    if args.json:
        _emit(data, True)
        return 0
    print(f"From:    {data['from']}")
    print(f"To:      {data['to']}")
    if data.get("cc"):
        print(f"Cc:      {data['cc']}")
    print(f"Subject: {data['subject']}")
    print(f"Date:    {data['date']}")
    print("-" * 60)
    print(data["text"] or "(no readable body)")
    if data.get("saved"):
        print("\nSaved attachments:")
        for path in data["saved"]:
            print(f"  {path}")
    return 0


def cmd_labels(args) -> int:
    client = MailClient(load_settings())
    try:
        rows = client.labels()
    finally:
        client.close()
    if args.json:
        _emit(rows, True)
        return 0
    for row in rows:
        print(f"{row['name']:<40} {row['flags']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gmail-imap",
        description="Gmail for AI agents — App Password only, no OAuth.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("config", help="save account + App Password")
    p.add_argument("--address")
    p.add_argument("--app-password", help="omit to be prompted (hidden)")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("status", help="verify login and list folders")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("send", help="send an email")
    p.add_argument("--to", action="append", required=True)
    p.add_argument("--cc", action="append")
    p.add_argument("--bcc", action="append")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--html", action="store_true", help="treat --body as HTML")
    p.add_argument("--attach", action="append", metavar="PATH")
    p.add_argument("--reply-to-uid", help="thread this as a reply")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("search", help="search with Gmail query syntax")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--mailbox")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("read", help="read one message")
    p.add_argument("--uid", required=True)
    p.add_argument("--mailbox")
    p.add_argument("--save-attachments", metavar="DIR")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("labels", help="list folders and labels")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_labels)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AuthError as exc:
        print(f"Auth error: {exc}", file=sys.stderr)
        return 3
    except GmailError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
