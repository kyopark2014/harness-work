#!/usr/bin/env python3
"""Documents registry — projects + drawings lists.

Tracks documents under ``documents/projects/`` (``project_list.json``) and
``documents/drawings/`` (``drawings_list.json``). Call ``upsert_document`` on
upload and ``mark_extracted`` / ``upsert_document`` after Sync writes
``{stem}.md`` / ``{stem}.json``.

Usage:
    from doc_list import upsert_document, load_doc_list, rebuild_doc_list, PROJECTS, DRAWINGS

    upsert_document(docs_root, filename="b.pdf", source_path=..., registry=PROJECTS)
    upsert_document(docs_root, filename="c.pdf", source_path=..., registry=DRAWINGS)
    mark_extracted(docs_root, source_path=..., md_path=..., json_path=...)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_LIST_NAME = "project_list.json"
PROJECTS_DIR_NAME = "projects"
DRAWINGS_LIST_NAME = "drawings_list.json"
DRAWINGS_DIR_NAME = "drawings"

@dataclass(frozen=True)
class DocRegistry:
    """Which folder + JSON registry a Documents entry belongs to."""

    list_name: str
    dir_name: str


PROJECTS = DocRegistry(PROJECT_LIST_NAME, PROJECTS_DIR_NAME)
DRAWINGS = DocRegistry(DRAWINGS_LIST_NAME, DRAWINGS_DIR_NAME)
DEFAULT_REGISTRY = PROJECTS

_SOURCE_SUFFIXES = {
    ".pdf",
    ".md",
    ".txt",
    ".text",
    ".rst",
    ".markdown",
}
# Keep letters/digits/._- ; collapse everything else (spaces, unicode punct, …).
_UNSAFE_FILENAME_RE = re.compile(r"[^0-9A-Za-z._-]+")
_MULTI_SEP_RE = re.compile(r"[_.-]{2,}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _file_fingerprint(path: str | Path | None) -> str | None:
    """Stable ``mtime:size`` token for change detection."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        st = p.stat()
        return f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        return None


def _document_content_fingerprints(
    source_path: str | Path | None,
    md_path: str | None,
    json_path: str | None,
) -> dict[str, str | None]:
    src = Path(source_path or "")
    return {
        "source": _file_fingerprint(src) if src.is_file() else None,
        "md": _file_fingerprint(md_path),
        "json": _file_fingerprint(json_path),
    }


def _prior_content_fingerprints(prev: dict[str, Any]) -> dict[str, str | None]:
    stored = prev.get("content_fingerprints")
    if isinstance(stored, dict):
        return {
            "source": stored.get("source"),
            "md": stored.get("md"),
            "json": stored.get("json"),
        }
    return _document_content_fingerprints(
        prev.get("source_path"),
        prev.get("md_path"),
        prev.get("json_path"),
    )


def _extracted_at_from_paths(
    source_path: str | Path,
    md_path: str | None,
    *,
    registry: DocRegistry,
) -> str | None:
    """Best-effort extraction timestamp from sidecar/source mtimes on disk."""
    if md_path and Path(md_path).is_file():
        return _iso_from_mtime(Path(md_path))
    return None


def sanitize_documents_filename(filename: str, *, default: str = "upload.bin") -> str:
    """Return a shell/path-safe basename (spaces → ``_``, strip unsafe chars).

    Preserves a single final extension (lowercased). Examples::

        ``s9540_3_2025 1.pdf`` → ``s9540_3_2025_1.pdf``
        ``Report (final).PDF`` → ``Report_final.pdf``
    """
    name = os.path.basename((filename or "").strip()) or default
    name = name.replace("\x00", "")
    if name in {".", ".."}:
        return default

    stem, ext = os.path.splitext(name)
    ext = ext.lower() if ext else ""
    # Drop trailing dots/spaces from stem before sanitizing.
    stem = stem.strip(" .")
    stem = _UNSAFE_FILENAME_RE.sub("_", stem)
    stem = _MULTI_SEP_RE.sub("_", stem).strip("._-")
    if not stem:
        stem = "document"
    # Cap length; leave room for extension.
    max_stem = 180
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip("._-") or "document"
    safe_ext = _UNSAFE_FILENAME_RE.sub("", ext) if ext else ""
    if safe_ext and not safe_ext.startswith("."):
        safe_ext = "." + safe_ext
    return f"{stem}{safe_ext}" if safe_ext else stem



def sanitize_ess_filename(filename: str, *, default: str = "upload.bin") -> str:
    """Alias for :func:`sanitize_documents_filename` (compat)."""
    return sanitize_documents_filename(filename, default=default)

def unique_documents_filename(directory: str | Path, filename: str) -> str:
    """Sanitize *filename* and avoid clobbering an unrelated existing file."""
    directory = Path(directory)
    safe = sanitize_documents_filename(filename)
    candidate = directory / safe
    if not candidate.exists():
        return safe
    stem, ext = os.path.splitext(safe)
    n = 2
    while True:
        alt = f"{stem}_{n}{ext}"
        if not (directory / alt).exists():
            return alt
        n += 1



def unique_ess_filename(directory: str | Path, filename: str) -> str:
    """Alias for :func:`unique_documents_filename` (compat)."""
    return unique_documents_filename(directory, filename)


def doc_list_path(
    docs_root: str | Path,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> Path:
    return Path(docs_root) / registry.list_name


def docs_dir(
    docs_root: str | Path,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> Path:
    """Return ``{documents}/projects`` or ``{documents}/drawings``."""
    return Path(docs_root) / registry.dir_name


def _source_suffixes_for(registry: DocRegistry) -> set[str]:
    return _SOURCE_SUFFIXES


def resolve_registry(
    docs_root: str | Path,
    *,
    source_path: str | None = None,
    registry: DocRegistry | None = None,
) -> DocRegistry:
    """Pick projects / drawings from registry or source path."""
    if registry is not None:
        return registry
    if not source_path:
        return DEFAULT_REGISTRY
    root = Path(docs_root)
    try:
        src = Path(source_path).resolve()
    except OSError:
        src = Path(source_path)
    for cand in (DRAWINGS, PROJECTS):
        folder = (root / cand.dir_name).resolve()
        try:
            src.relative_to(folder)
            return cand
        except ValueError:
            continue
    return DEFAULT_REGISTRY



def empty_doc_list(*, user_id: str | None = None) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "updated_at": _now_iso(),
        "documents": [],
    }


def load_doc_list(
    docs_root: str | Path,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    path = doc_list_path(docs_root, registry)
    if not path.is_file():
        return empty_doc_list()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_doc_list()
    if not isinstance(data, dict):
        return empty_doc_list()
    docs = data.get("documents")
    if not isinstance(docs, list):
        data["documents"] = []
    return data


def save_doc_list(
    docs_root: str | Path,
    data: dict[str, Any],
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> Path:
    root = Path(docs_root)
    root.mkdir(parents=True, exist_ok=True)
    path = doc_list_path(root, registry)
    payload = dict(data) if isinstance(data, dict) else empty_doc_list()
    payload["updated_at"] = _now_iso()
    if not isinstance(payload.get("documents"), list):
        payload["documents"] = []
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def _norm_key(filename: str | None = None, source_path: str | None = None) -> str:
    if filename and str(filename).strip():
        return os.path.basename(str(filename).strip())
    if source_path:
        return os.path.basename(str(source_path).strip())
    return ""


def _find_index(documents: list[Any], key: str) -> int:
    if not key:
        return -1
    for i, item in enumerate(documents):
        if not isinstance(item, dict):
            continue
        name = str(item.get("filename") or "")
        src = str(item.get("source_path") or "")
        if name == key or os.path.basename(src) == key:
            return i
    return -1


def list_documents(
    docs_root: str | Path,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> list[dict[str, Any]]:
    data = load_doc_list(docs_root, registry)
    return [d for d in (data.get("documents") or []) if isinstance(d, dict)]


def get_document(
    docs_root: str | Path,
    *,
    filename: str | None = None,
    source_path: str | None = None,
    registry: DocRegistry | None = None,
) -> dict[str, Any] | None:
    reg = resolve_registry(docs_root, source_path=source_path, registry=registry)
    key = _norm_key(filename, source_path)
    docs = list_documents(docs_root, reg)
    idx = _find_index(docs, key)
    return dict(docs[idx]) if idx >= 0 else None


def upsert_document(
    docs_root: str | Path,
    *,
    filename: str,
    source_path: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    md_path: str | None = None,
    json_path: str | None = None,
    bytes_size: int | None = None,
    suffix: str | None = None,
    status: str | None = None,
    user_id: str | None = None,
    extra: dict[str, Any] | None = None,
    registry: DocRegistry | None = None,
) -> dict[str, Any]:
    """Insert or update one document entry; persist the matching list JSON."""
    root = Path(docs_root)
    reg = resolve_registry(root, source_path=source_path, registry=registry)
    docs_dir(root, reg).mkdir(parents=True, exist_ok=True)
    data = load_doc_list(root, reg)
    if user_id is not None:
        data["user_id"] = user_id

    documents: list[Any] = list(data.get("documents") or [])
    key = _norm_key(filename, source_path)
    now = _now_iso()
    idx = _find_index(documents, key)

    src_path = source_path
    if not src_path and key:
        candidate = docs_dir(root, reg) / key
        src_path = str(candidate.resolve()) if candidate.is_file() else str(candidate)

    resolved_suffix = suffix
    if resolved_suffix is None and key:
        resolved_suffix = Path(key).suffix.lower()

    size = bytes_size
    if size is None and src_path and Path(src_path).is_file():
        try:
            size = Path(src_path).stat().st_size
        except OSError:
            size = None

    md = md_path
    js = json_path
    stem = Path(key).stem if key else ""
    docs_path = docs_dir(root, reg)
    if md is None and stem:
        cand = docs_path / f"{stem}.md"
        if cand.is_file():
            md = str(cand.resolve())
    if js is None and stem:
        cand = docs_path / f"{stem}.json"
        if cand.is_file():
            js = str(cand.resolve())

    inferred_status = status
    if inferred_status is None:
        inferred_status = "extracted" if md and Path(md).is_file() else "uploaded"

    entry: dict[str, Any] = {
        "filename": key,
        "created_at": created_at or now,
        "updated_at": updated_at or now,
        "source_path": src_path,
        "md_path": md,
        "md_file": os.path.basename(md) if md else None,
        "json_path": js,
        "bytes": size,
        "suffix": resolved_suffix,
        "status": inferred_status,
    }
    entry_fps = _document_content_fingerprints(src_path, md, js)
    entry["content_fingerprints"] = entry_fps
    extracted = _extracted_at_from_paths(
        src_path or "", md, registry=reg
    )
    if extracted:
        entry["extracted_at"] = extracted
    if extra:
        for k, v in extra.items():
            if v is not None:
                entry[k] = v

    if idx >= 0:
        prev = documents[idx] if isinstance(documents[idx], dict) else {}
        # Preserve original created_at unless caller overrides.
        if created_at is None and prev.get("created_at"):
            entry["created_at"] = prev["created_at"]
        if extra is None or extra.get("original_filename") is None:
            if prev.get("original_filename"):
                entry["original_filename"] = prev["original_filename"]
        # Keep existing md/json if not provided and still present.
        if md_path is None and prev.get("md_path"):
            entry["md_path"] = prev["md_path"]
            entry["md_file"] = prev.get("md_file") or (
                os.path.basename(str(prev["md_path"]))
                if prev.get("md_path")
                else None
            )
        if json_path is None and prev.get("json_path"):
            entry["json_path"] = prev["json_path"]
        if status is None and prev.get("status") == "extracted" and entry.get("md_path"):
            entry["status"] = "extracted"
        prev_fps = _prior_content_fingerprints(prev)
        if updated_at is None:
            if entry_fps == prev_fps:
                entry["updated_at"] = prev.get("updated_at") or now
            else:
                entry["updated_at"] = now
        if entry_fps == prev_fps and prev.get("extracted_at"):
            entry["extracted_at"] = prev.get("extracted_at")
        documents[idx] = entry
    else:
        documents.append(entry)

    data["documents"] = documents
    save_doc_list(root, data, reg)
    return entry


def mark_extracted(
    docs_root: str | Path,
    *,
    source_path: str,
    md_path: str,
    json_path: str | None = None,
    user_id: str | None = None,
    registry: DocRegistry | None = None,
) -> dict[str, Any]:
    """Update registry after Sync produces markdown next to the source."""
    name = os.path.basename(source_path)
    return upsert_document(
        docs_root,
        filename=name,
        source_path=source_path,
        md_path=md_path,
        json_path=json_path,
        status="extracted",
        user_id=user_id,
        updated_at=_now_iso(),
        registry=registry,
    )


def remove_document(
    docs_root: str | Path,
    *,
    filename: str | None = None,
    source_path: str | None = None,
    registry: DocRegistry | None = None,
) -> bool:
    """Remove one entry from the matching list JSON. Returns True if removed."""
    root = Path(docs_root)
    reg = resolve_registry(root, source_path=source_path, registry=registry)
    data = load_doc_list(root, reg)
    documents: list[Any] = list(data.get("documents") or [])
    key = _norm_key(filename, source_path)
    idx = _find_index(documents, key)
    if idx < 0:
        return False
    documents.pop(idx)
    data["documents"] = documents
    save_doc_list(root, data, reg)
    return True


def _is_sidecar(
    path: Path,
    docs_path: Path,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> bool:
    suf = path.suffix.lower()
    stem = path.stem
    if suf == ".json":
        for ext in _SOURCE_SUFFIXES - {".md"}:
            if (docs_path / f"{stem}{ext}").is_file():
                return True
        return (docs_path / f"{stem}.md").is_file()
    if suf == ".md":
        for ext in (".pdf", ".txt", ".text", ".rst", ".markdown"):
            if (docs_path / f"{stem}{ext}").is_file():
                return True
    return False


def rebuild_doc_list(
    docs_root: str | Path,
    *,
    user_id: str | None = None,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Rescan the registry folder and rewrite its list JSON from source files."""
    root = Path(docs_root)
    docs_path = docs_dir(root, registry)
    docs_path.mkdir(parents=True, exist_ok=True)
    previous = {
        str(d.get("filename") or ""): d
        for d in list_documents(root, registry)
        if isinstance(d, dict)
    }
    # Also index by original_filename so rebuild after sanitize keeps metadata.
    for d in list(previous.values()):
        orig = d.get("original_filename")
        if orig and orig not in previous:
            previous[str(orig)] = d

    source_suffixes = _source_suffixes_for(registry)
    documents: list[dict[str, Any]] = []
    if docs_path.is_dir():
        for path in sorted(docs_path.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in source_suffixes:
                continue
            if _is_sidecar(path, docs_path, registry):
                continue
            stem = path.stem
            md = docs_path / f"{stem}.md"
            js = docs_path / f"{stem}.json"
            # Uploaded markdown is itself the source (and the md).
            if path.suffix.lower() == ".md":
                md_path = str(path.resolve())
            else:
                md_path = str(md.resolve()) if md.is_file() else None
            json_p = str(js.resolve()) if js.is_file() else None
            prev = previous.get(path.name) or {}
            # Match prior entry whose sanitized name equals current file.
            if not prev:
                for cand in previous.values():
                    orig = str(cand.get("original_filename") or cand.get("filename") or "")
                    if orig and sanitize_documents_filename(orig) == path.name:
                        prev = cand
                        break
            created = prev.get("created_at") or _iso_from_mtime(path) or _now_iso()
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            original = (
                prev.get("original_filename")
                or prev.get("filename")
                or path.name
            )
            status = (
                "extracted"
                if md_path and Path(md_path).is_file()
                else "uploaded"
            )
            fps = _document_content_fingerprints(
                str(path.resolve()), md_path, json_p
            )
            prev_fps = _prior_content_fingerprints(prev) if prev else {}
            content_changed = not prev or fps != prev_fps
            updated = _now_iso() if content_changed else (
                prev.get("updated_at") or _now_iso()
            )
            extracted_at = _extracted_at_from_paths(
                path, md_path, registry=registry
            )
            entry: dict[str, Any] = {
                "filename": path.name,
                "original_filename": original,
                "sanitized": sanitize_documents_filename(str(original)) != str(original)
                or str(original) != path.name,
                "created_at": created,
                "updated_at": updated,
                "extracted_at": extracted_at,
                "content_fingerprints": fps,
                "source_path": str(path.resolve()),
                "md_path": md_path,
                "md_file": os.path.basename(md_path) if md_path else None,
                "json_path": json_p,
                "bytes": size,
                "suffix": path.suffix.lower(),
                "status": status,
            }
            documents.append(entry)

    data = {
        "user_id": user_id
        if user_id is not None
        else load_doc_list(root, registry).get("user_id"),
        "updated_at": _now_iso(),
        "documents": documents,
    }
    save_doc_list(root, data, registry)
    return data


def sanitize_existing_docs_filenames(
    docs_root: str | Path,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> list[dict[str, str]]:
    """Rename unsanitized files under the registry folder (and matching sidecars / FMP paths).

    Returns a list of ``{from, to}`` renames for source files.
    """
    root = Path(docs_root)
    docs_path = docs_dir(root, registry)
    docs_path.mkdir(parents=True, exist_ok=True)
    renames: list[dict[str, str]] = []
    source_suffixes = _source_suffixes_for(registry)

    # Group by current stem so pdf/md/json (or xlsx/json) move together.
    by_stem: dict[str, list[Path]] = {}
    if not docs_path.is_dir():
        return renames
    for path in list(docs_path.iterdir()):
        if not path.is_file():
            continue
        by_stem.setdefault(path.stem, []).append(path)

    stem_map: dict[str, str] = {}  # old_stem → new_stem
    for old_stem, paths in sorted(by_stem.items()):
        # Prefer sanitizing from a source file name if present.
        source = None
        for p in paths:
            if p.suffix.lower() in source_suffixes and not _is_sidecar(
                p, docs_path, registry
            ):
                source = p
                break
        sample = source or paths[0]
        safe_name = sanitize_documents_filename(sample.name)
        new_stem = Path(safe_name).stem
        if new_stem == old_stem:
            continue
        # Collision: if target stem already occupied by other files, skip.
        conflict = False
        for p in paths:
            dest = docs_path / f"{new_stem}{p.suffix.lower() if p.suffix else p.suffix}"
            # Allow replacing our own renamed path; block unrelated existing files.
            if dest.exists() and dest.resolve() not in {x.resolve() for x in paths}:
                conflict = True
                break
        if conflict:
            continue
        stem_map[old_stem] = new_stem

    for old_stem, new_stem in stem_map.items():
        moved: list[tuple[str, Path]] = []
        for path in by_stem.get(old_stem, []):
            ext = path.suffix.lower() if path.suffix else path.suffix
            dest = docs_path / f"{new_stem}{ext}"
            if path.resolve() == dest.resolve() or dest.exists():
                continue
            old_name = path.name
            try:
                path.rename(dest)
            except OSError:
                continue
            moved.append((old_name, dest))

            if dest.suffix.lower() in {".json", ".md"}:
                try:
                    text = dest.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text:
                    updated = text
                    # Prefer full path rewrite when possible.
                    updated = updated.replace(str(path), str(dest.resolve()))
                    updated = updated.replace(old_name, dest.name)
                    updated = updated.replace(old_stem, new_stem)
                    if updated != text:
                        dest.write_text(updated, encoding="utf-8")

        for old_name, dest in moved:
            if dest.suffix.lower() in {
                ".pdf",
                ".txt",
                ".text",
                ".rst",
                ".markdown",
                ".xlsx",
            } or (
                dest.suffix.lower() == ".md"
                and not any(
                    (docs_path / f"{new_stem}{e}").is_file()
                    for e in (".pdf", ".txt", ".text", ".rst", ".markdown")
                )
            ):
                renames.append(
                    {
                        "from": old_name,
                        "to": dest.name,
                        "original_filename": old_name,
                    }
                )

    if stem_map:
        _fixup_paths_after_filename_sanitize(root, stem_map)

    return renames


def _fixup_paths_after_filename_sanitize(
    docs_root: Path, stem_map: dict[str, str]
) -> None:
    """Update ``.pdf_pages`` source_path.txt and work-dir names after rename."""
    pages_root = docs_root / "out" / "converted" / ".pdf_pages"
    if not pages_root.is_dir():
        return
    for work in list(pages_root.iterdir()):
        if not work.is_dir():
            continue
        marker = work / "source_path.txt"
        if not marker.is_file():
            continue
        try:
            line = marker.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            continue
        old = Path(line)
        new_stem = stem_map.get(old.stem)
        if not new_stem:
            # Also try matching basename with spaces already partially fixed.
            continue
        # Prefer the same folder the source lived in (projects or drawings).
        parent = old.parent if old.parent.is_dir() else (docs_root / PROJECTS_DIR_NAME)
        new_path = parent / f"{new_stem}{old.suffix.lower()}"
        if not new_path.is_file():
            # Fall back to projects / drawings roots.
            for dirname in (PROJECTS_DIR_NAME, DRAWINGS_DIR_NAME):
                cand = docs_root / dirname / f"{new_stem}{old.suffix.lower()}"
                if cand.is_file():
                    new_path = cand
                    break
        if not new_path.is_file():
            continue
        marker.write_text(str(new_path.resolve()) + "\n", encoding="utf-8")
        new_digest = hashlib.sha256(str(new_path.resolve()).encode()).hexdigest()[:8]
        new_name = f"{new_stem}_{new_digest}"
        if work.name == new_name:
            continue
        target = pages_root / new_name
        if target.exists():
            continue
        try:
            work.rename(target)
        except OSError:
            pass


def sync_doc_list_with_filesystem(
    docs_root: str | Path,
    *,
    user_id: str | None = None,
    registry: DocRegistry = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Ensure the registry folder exists, sanitize filenames, rebuild list JSON."""
    docs_dir(docs_root, registry).mkdir(parents=True, exist_ok=True)
    sanitize_existing_docs_filenames(docs_root, registry)
    return rebuild_doc_list(docs_root, user_id=user_id, registry=registry)


def sync_all_doc_lists(
    docs_root: str | Path,
    *,
    user_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Rebuild projects / drawings list JSON files."""
    return {
        "projects": sync_doc_list_with_filesystem(
            docs_root, user_id=user_id, registry=PROJECTS
        ),
        "drawings": sync_doc_list_with_filesystem(
            docs_root, user_id=user_id, registry=DRAWINGS
        ),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage documents project/drawings lists")
    parser.add_argument(
        "--docs-root",
        required=True,
        help="Path to .session_storage/{user}/documents",
    )
    parser.add_argument(
        "--user", default=None, help="User id to store in list JSON"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rescan projects/ and rewrite project_list.json",
    )
    args = parser.parse_args()
    root = Path(args.docs_root).expanduser().resolve()
    if args.rebuild:
        data = sync_doc_list_with_filesystem(root, user_id=args.user)
        print(f"Rebuilt {doc_list_path(root)} ({len(data.get('documents') or [])} doc(s))")
    else:
        data = load_doc_list(root)
        print(json.dumps(data, ensure_ascii=False, indent=2))
