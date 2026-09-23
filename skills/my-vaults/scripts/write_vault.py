#!/usr/bin/env python3
"""
Write CLI for ob-note vault (write / append / mkdir / rename / delete / upload).

Examples:
  python write_vault.py write notes/Hello.md --content "# Hello\\n"
  python write_vault.py write notes/Hello.md --file ./draft.md --embed-images
  python write_vault.py upload Architecture/dxf_analysis.png --file ./dxf_analysis.png
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


def _parse_map(values: list[str] | None) -> dict[str, str]:
    """Parse --map URL=localpath (or basename=localpath) pairs."""
    out: dict[str, str] = {}
    for raw in values or []:
        if "=" not in raw:
            raise SystemExit(f"--map expects URL=localpath, got: {raw!r}")
        key, path = raw.split("=", 1)
        key = key.strip()
        path = path.strip()
        if not key or not path:
            raise SystemExit(f"--map expects URL=localpath, got: {raw!r}")
        out[key] = path
        # also allow lookup by basename
        base = Path(key.replace("\\", "/")).name
        if base and base not in out:
            out[base] = path
    return out


def _maybe_embed(
    note_path: str,
    content: str,
    args: argparse.Namespace,
) -> tuple[str, list]:
    if not getattr(args, "embed_images", False):
        return content, []
    return vault.embed_images(
        note_path,
        content,
        user_id=args.user_id,
        local_map=_parse_map(getattr(args, "map", None)),
    )


def cmd_write(args: argparse.Namespace) -> int:
    content = _read_content(args)
    content, embeds = _maybe_embed(args.path, content, args)
    payload = vault.api_request(
        "PUT",
        "/api/files/write",
        user_id=args.user_id,
        body={"path": args.path, "content": content},
    )
    if embeds:
        payload = {**payload, "embedded_images": embeds}
    vault.print_json(payload)
    return 0


def cmd_append(args: argparse.Namespace) -> int:
    content = _read_content(args)
    content, embeds = _maybe_embed(args.path, content, args)
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
    if embeds:
        payload = {**payload, "embedded_images": embeds}
    vault.print_json(payload)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    data = Path(args.file).read_bytes()
    payload = vault.api_upload(
        args.path,
        data,
        filename=Path(args.file).name,
        user_id=args.user_id,
    )
    vault.print_json(payload)
    return 0


def cmd_embed_images(args: argparse.Namespace) -> int:
    """Re-process an existing note: pull remote images into its folder."""
    existing = vault.api_request(
        "GET",
        "/api/files/read",
        user_id=args.user_id,
        query={"path": args.path},
    )
    content = ""
    if isinstance(existing, dict):
        content = existing.get("content") or existing.get("body") or ""
    if not content:
        raise SystemExit(f"empty or missing note: {args.path}")

    new_content, embeds = vault.embed_images(
        args.path,
        content,
        user_id=args.user_id,
        local_map=_parse_map(args.map),
    )
    if new_content == content:
        vault.print_json({"ok": True, "path": args.path, "changed": False, "embedded_images": embeds})
        return 0

    payload = vault.api_request(
        "PUT",
        "/api/files/write",
        user_id=args.user_id,
        body={"path": args.path, "content": new_content},
    )
    payload = {**payload, "changed": True, "embedded_images": embeds}
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


def _add_embed_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--embed-images",
        action="store_true",
        help="Download/copy remote ![img](url) into the note folder and rewrite links",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="URL=LOCAL",
        help="Map a remote image URL (or basename) to a local file (repeatable)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write ob-note vault via API")
    parser.add_argument("--user-id", default=None, help="Override USER_ID / CURRENT_USER_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("write", help="PUT /api/files/write (overwrite)")
    p.add_argument("path", help="Vault-relative path")
    _add_content_flags(p)
    _add_embed_flags(p)
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("append", help="POST /api/files/append")
    p.add_argument("path", help="Vault-relative path")
    _add_content_flags(p)
    _add_embed_flags(p)
    p.add_argument("--no-create", action="store_true", help="Fail if file missing")
    p.add_argument(
        "--separator",
        default="\n",
        help="Separator when file does not end with newline (default: \\n)",
    )
    p.set_defaults(func=cmd_append)

    p = sub.add_parser("upload", help="POST /api/files/upload (binary / image)")
    p.add_argument("path", help="Vault-relative destination path")
    p.add_argument("--file", required=True, help="Local file to upload")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser(
        "embed-images",
        help="Re-fetch remote images in an existing note into its folder",
    )
    p.add_argument("path", help="Vault-relative .md path")
    p.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="URL=LOCAL",
        help="Map a remote image URL (or basename) to a local file (repeatable)",
    )
    p.set_defaults(func=cmd_embed_images)

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
