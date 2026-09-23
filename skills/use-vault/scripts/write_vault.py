#!/usr/bin/env python3
"""
Write CLI for ob-note vault (write / append / mkdir / rename / delete).

Examples:
  python write_vault.py write notes/Hello.md --content "# Hello\\n"
  python write_vault.py write notes/Hello.md --file ./draft.md
  echo "more" | python write_vault.py append notes/Hello.md --stdin
  python write_vault.py mkdir 00-Inbox/projects
  python write_vault.py rename notes/Old.md notes/New.md
  python write_vault.py delete notes/New.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib_vault as vault  # noqa: E402


def _read_content(args: argparse.Namespace) -> str:
    if getattr(args, "stdin", False):
        return sys.stdin.read()
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    if args.content is None:
        raise SystemExit("Provide --content, --file, or --stdin")
    # Allow escaped newlines from shell: --content $'line\\nline'
    return args.content.replace("\\n", "\n")


def cmd_write(args: argparse.Namespace) -> int:
    content = _read_content(args)
    payload = vault.api_request(
        "PUT",
        "/api/files/write",
        user_id=args.user_id,
        body={"path": args.path, "content": content},
    )
    vault.print_json(payload)
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    content = _read_content(args)
    payload = vault.api_request(
        "POST",
        "/api/files/append",
        user_id=args.user_id,
        body={
            "path": args.path,
            "content": content,
            "create": not args.no_create,
            "separator": args.separator,
        },
    )
    vault.print_json(payload)
    return 0


def cmd_mkdir(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "POST",
        "/api/files/mkdir",
        user_id=args.user_id,
        body={"path": args.path},
    )
    vault.print_json(payload)
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "POST",
        "/api/files/rename",
        user_id=args.user_id,
        body={"from_path": args.from_path, "to_path": args.to_path},
    )
    vault.print_json(payload)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "POST",
        "/api/files/delete",
        user_id=args.user_id,
        body={"path": args.path},
    )
    vault.print_json(payload)
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "POST",
        "/api/graph/rebuild",
        user_id=args.user_id,
    )
    vault.print_json(payload)
    return 0


def _add_content_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--content", default=None, help="Inline content (\\n → newline)")
    parser.add_argument("--file", default=None, help="Read content from a local file")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read content from stdin",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write ob-note vault via API")
    parser.add_argument("--user-id", default=None, help="Override USER_ID / CURRENT_USER_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("write", help="PUT /api/files/write (overwrite)")
    p.add_argument("path", help="Vault-relative path")
    _add_content_flags(p)
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("append", help="POST /api/files/append")
    p.add_argument("path", help="Vault-relative path")
    _add_content_flags(p)
    p.add_argument("--no-create", action="store_true", help="Fail if file missing")
    p.add_argument(
        "--separator",
        default="\n",
        help="Separator when file does not end with newline (default: \\n)",
    )
    p.set_defaults(func=cmd_append)

    p = sub.add_parser("mkdir", help="POST /api/files/mkdir")
    p.add_argument("path", help="Folder path")
    p.set_defaults(func=cmd_mkdir)

    p = sub.add_parser("rename", help="POST /api/files/rename")
    p.add_argument("from_path", help="Source path")
    p.add_argument("to_path", help="Destination path")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("delete", help="POST /api/files/delete")
    p.add_argument("path", help="File or folder path")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("rebuild", help="POST /api/graph/rebuild")
    p.set_defaults(func=cmd_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
