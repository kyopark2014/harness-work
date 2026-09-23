#!/usr/bin/env python3
"""
Read-only CLI for ob-note vault (list / read / search / graph).

Examples:
  python read_vault.py health
  python read_vault.py tree
  python read_vault.py list --prefix notes
  python read_vault.py read notes/Convention.md
  python read_vault.py search "온톨로지"
  python read_vault.py graph
  python read_vault.py backlinks notes/Convention.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib_vault as vault  # noqa: E402


def cmd_health(args: argparse.Namespace) -> int:
    payload = vault.api_request("GET", "/api/health", user_id=args.user_id)
    vault.print_json(payload)
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    payload = vault.api_request("GET", "/api/session", user_id=args.user_id)
    vault.print_json(payload)
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    payload = vault.api_request("GET", "/api/files/tree", user_id=args.user_id)
    vault.print_json(payload)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "GET",
        "/api/files/list",
        user_id=args.user_id,
        query={"prefix": args.prefix or "", "ext": args.ext},
    )
    vault.print_json(payload)
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "GET",
        "/api/files/read",
        user_id=args.user_id,
        query={"path": args.path},
    )
    if args.raw:
        print(payload.get("content") or "", end="" if str(payload.get("content") or "").endswith("\n") else "\n")
        return 0
    vault.print_json(payload)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "GET",
        "/api/search",
        user_id=args.user_id,
        query={"q": args.query, "limit": args.limit},
    )
    vault.print_json(payload)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    payload = vault.api_request("GET", "/api/graph", user_id=args.user_id)
    vault.print_json(payload)
    return 0


def cmd_backlinks(args: argparse.Namespace) -> int:
    payload = vault.api_request(
        "GET",
        "/api/graph/backlinks",
        user_id=args.user_id,
        query={"path": args.path},
    )
    vault.print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read ob-note vault via API")
    parser.add_argument("--user-id", default=None, help="Override USER_ID / CURRENT_USER_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("health", help="GET /api/health")
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("session", help="GET /api/session")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("tree", help="GET /api/files/tree")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("list", help="GET /api/files/list")
    p.add_argument("--prefix", default="", help="Folder prefix (e.g. notes)")
    p.add_argument("--ext", default="md", help="Extension filter (md|txt|* )")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("read", help="GET /api/files/read")
    p.add_argument("path", help="Vault-relative path, e.g. notes/Welcome.md")
    p.add_argument("--raw", action="store_true", help="Print markdown body only")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("search", help="GET /api/search")
    p.add_argument("query", help="Search query")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("graph", help="GET /api/graph")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("backlinks", help="GET /api/graph/backlinks")
    p.add_argument("path", help="Vault-relative note path")
    p.set_defaults(func=cmd_backlinks)

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
