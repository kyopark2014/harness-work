"""Documents API — Configure / Sync for per-user ``.session_storage/{user}/documents``.

Projects + Drawings only (no Regulations / Test Cases).
"""

from __future__ import annotations

import html
import logging
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from application.api.routes_auth import require_user_id
from application.documents_jobs import ensure_documents_sync, get_documents_job_status
from application import utils

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

_MAX_MULTIPART_DOC_BYTES = 80 * 1024 * 1024  # 80 MiB


class DocumentsConfigPut(BaseModel):
    foundation_model_parser_enabled: bool | None = None
    parallel_processing_enabled: bool | None = None


class DocumentsPresignRequest(BaseModel):
    file_name: str = Field(..., min_length=1)
    size: int | None = Field(default=None, ge=0)
    content_type: str | None = None


class DocumentsCompleteRequest(BaseModel):
    file_name: str = Field(..., min_length=1)
    s3_key: str = Field(..., min_length=1)
    size: int | None = Field(default=None, ge=0)
    original_filename: str | None = None


def _load_project_list_payload(user_id: str, *, enrich: bool = False) -> dict:
    try:
        utils._ensure_documents_on_path()
        from doc_list import PROJECTS, load_doc_list, list_documents

        root = utils.get_user_documents_dir(user_id)
        data = load_doc_list(root, PROJECTS)
        documents = list_documents(root, PROJECTS)
        if enrich:
            documents = utils.enrich_documents_for_ui(
                documents, user_id=user_id, publish_md=True, kind="project"
            )
        return {
            "project_list": str(Path(root) / "project_list.json"),
            "doc_list": str(Path(root) / "project_list.json"),
            "documents": documents,
            "doc_count": len(data.get("documents") or []),
            "doc_list_updated_at": data.get("updated_at"),
            "sharing_url": utils.sharing_url or None,
        }
    except Exception:
        return {
            "project_list": utils.documents_project_list_path(user_id),
            "doc_list": utils.documents_project_list_path(user_id),
            "documents": [],
            "doc_count": 0,
            "doc_list_updated_at": None,
            "sharing_url": utils.sharing_url or None,
        }


def _load_drawings_list_payload(user_id: str, *, enrich: bool = False) -> dict:
    try:
        utils._ensure_documents_on_path()
        from doc_list import DRAWINGS, load_doc_list, list_documents

        root = utils.get_user_documents_dir(user_id)
        data = load_doc_list(root, DRAWINGS)
        documents = list_documents(root, DRAWINGS)
        if enrich:
            documents = utils.enrich_documents_for_ui(
                documents, user_id=user_id, publish_md=True, kind="drawing"
            )
        return {
            "drawings_list": str(Path(root) / "drawings_list.json"),
            "doc_list": str(Path(root) / "drawings_list.json"),
            "documents": documents,
            "doc_count": len(data.get("documents") or []),
            "doc_list_updated_at": data.get("updated_at"),
            "sharing_url": utils.sharing_url or None,
        }
    except Exception:
        return {
            "drawings_list": utils.documents_drawings_list_path(user_id),
            "doc_list": utils.documents_drawings_list_path(user_id),
            "documents": [],
            "doc_count": 0,
            "doc_list_updated_at": None,
            "sharing_url": utils.sharing_url or None,
        }


def _safe_doc_name(name: str) -> str:
    cleaned = Path(unquote(name or "")).name.strip()
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid document name")
    return cleaned


def _resolve_documents_doc_path(
    user_id: str,
    filename: str,
    *,
    kind: str = "project",
) -> Path:
    if kind == "drawing":
        bases = [
            utils.documents_drawings_dir(user_id),
            utils.documents_projects_dir(user_id),
        ]
    else:
        bases = [
            utils.documents_projects_dir(user_id),
            utils.documents_drawings_dir(user_id),
        ]

    for base_str in bases:
        docs = Path(base_str)
        path = (docs / filename).resolve()
        try:
            path.relative_to(docs.resolve())
        except ValueError:
            continue
        if path.is_file():
            return path
    raise HTTPException(status_code=404, detail=f"Document not found: {filename}")


def _assert_documents_doc_size(size: int | None) -> None:
    if size is None:
        return
    if size <= 0:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다.")
    if size > utils.MAX_DOCUMENTS_DOC_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"파일이 너무 큽니다 "
                f"(최대 {utils.MAX_DOCUMENTS_DOC_BYTES // (1024 * 1024)}MB)."
            ),
        )


def _documents_folder_scope(kind: str) -> str:
    normalized = (kind or "project").strip().lower()
    if normalized == "drawing":
        return "drawing"
    return "project"


def _documents_folder_dir(user_id: str, scope: str) -> str:
    if scope == "drawing":
        return utils.documents_drawings_dir(user_id)
    return utils.documents_projects_dir(user_id)


@router.get("/status")
def documents_status(request: Request) -> dict:
    user_id = require_user_id(request)
    job = get_documents_job_status(user_id)
    project_files = utils.list_documents_project_files(user_id)
    drawing_files = utils.list_documents_drawing_files(user_id)
    files = project_files + drawing_files
    converted = Path(utils.documents_converted_dir(user_id))
    status = job.get("status") or "idle"
    if status in ("idle", "unchanged") and files:
        status = "ready" if status == "idle" else status
    return {
        "documents_dir": utils.get_user_documents_dir(user_id),
        "projects_dir": utils.documents_projects_dir(user_id),
        "drawings_dir": utils.documents_drawings_dir(user_id),
        "converted_dir": str(converted),
        "files": files,
        "exists": len(files) > 0,
        "status": status,
        "foundation_model_parser_enabled": utils.is_documents_foundation_model_parser_enabled(
            user_id
        ),
        "parallel_processing_enabled": utils.is_documents_parallel_processing_enabled(
            user_id
        ),
        "error": job.get("error"),
        "message": job.get("message"),
        "last_success_at": job.get("last_success_at"),
        "progress": job.get("progress"),
        **_load_project_list_payload(user_id),
    }


@router.get("/config")
def get_documents_config(request: Request) -> dict:
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    return {
        "documents_dir": utils.get_user_documents_dir(user_id),
        "projects_dir": utils.documents_projects_dir(user_id),
        "drawings_dir": utils.documents_drawings_dir(user_id),
        "files": utils.list_documents_project_files(user_id)
        + utils.list_documents_drawing_files(user_id),
        "foundation_model_parser_enabled": utils.is_documents_foundation_model_parser_enabled(
            user_id
        ),
        "parallel_processing_enabled": utils.is_documents_parallel_processing_enabled(
            user_id
        ),
        **_load_project_list_payload(user_id),
    }


@router.put("/config")
def put_documents_config(body: DocumentsConfigPut, request: Request) -> dict:
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    if body.foundation_model_parser_enabled is not None:
        utils.set_documents_foundation_model_parser_enabled(
            bool(body.foundation_model_parser_enabled),
            user_id=user_id,
        )
    if body.parallel_processing_enabled is not None:
        utils.set_documents_parallel_processing_enabled(
            bool(body.parallel_processing_enabled),
            user_id=user_id,
        )
    return {
        "documents_dir": utils.get_user_documents_dir(user_id),
        "projects_dir": utils.documents_projects_dir(user_id),
        "drawings_dir": utils.documents_drawings_dir(user_id),
        "files": utils.list_documents_project_files(user_id)
        + utils.list_documents_drawing_files(user_id),
        "foundation_model_parser_enabled": utils.is_documents_foundation_model_parser_enabled(
            user_id
        ),
        "parallel_processing_enabled": utils.is_documents_parallel_processing_enabled(
            user_id
        ),
        **_load_project_list_payload(user_id),
    }


@router.get("/project-list")
def get_documents_project_list(
    request: Request,
    publish_md: bool = Query(True),
) -> dict:
    """Return project documents with PDF/MD view URLs (``project_list.json``)."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    payload = _load_project_list_payload(user_id, enrich=False)
    payload["documents"] = utils.enrich_documents_for_ui(
        payload.get("documents") or [],
        user_id=user_id,
        publish_md=bool(publish_md),
        kind="project",
    )
    payload["doc_count"] = len(payload["documents"])
    return {
        "documents_dir": utils.get_user_documents_dir(user_id),
        "projects_dir": utils.documents_projects_dir(user_id),
        "docs_dir": utils.documents_projects_dir(user_id),
        **payload,
    }


@router.get("/drawing-list")
def get_documents_drawing_list(
    request: Request,
    publish_md: bool = Query(True),
) -> dict:
    """Return drawing documents with PDF/MD view URLs (``drawings_list.json``)."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    payload = _load_drawings_list_payload(user_id, enrich=False)
    payload["documents"] = utils.enrich_documents_for_ui(
        payload.get("documents") or [],
        user_id=user_id,
        publish_md=bool(publish_md),
        kind="drawing",
    )
    payload["doc_count"] = len(payload["documents"])
    return {
        "documents_dir": utils.get_user_documents_dir(user_id),
        "drawings_dir": utils.documents_drawings_dir(user_id),
        "docs_dir": utils.documents_drawings_dir(user_id),
        **payload,
    }


@router.delete("/documents/{filename}")
def api_delete_documents_document(
    filename: str,
    request: Request,
    kind: str = Query("project"),
) -> dict:
    """Delete a Documents entry (source + JSON/MD sidecars + list entry).

    ``kind``: ``project`` | ``drawing``
    """
    user_id = require_user_id(request)
    name = _safe_doc_name(filename)
    scope = (kind or "project").strip().lower()
    if scope not in {"project", "drawing"}:
        raise HTTPException(
            status_code=400,
            detail="kind must be project or drawing",
        )
    try:
        return utils.delete_documents_document(user_id, name, kind=scope)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/documents/{filename}/pdf")
def get_documents_document_pdf(
    filename: str,
    request: Request,
    kind: str = Query("project"),
):
    """Open PDF in-browser: stream local file, else S3."""
    user_id = require_user_id(request)
    name = _safe_doc_name(filename)
    if not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Not a PDF document")

    scope = _documents_folder_scope(kind)
    try:
        path = _resolve_documents_doc_path(user_id, name, kind=scope)
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=name,
            content_disposition_type="inline",
        )
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    streamed = utils.stream_documents_pdf_from_s3(name, user_id=user_id, kind=scope)
    if streamed is not None:
        return streamed

    raise HTTPException(status_code=404, detail=f"Document not found: {name}")


@router.get("/documents/{filename}/markdown")
def get_documents_document_markdown_viewer(
    filename: str,
    request: Request,
    kind: str = Query("project"),
    download: int = Query(0),
):
    """Markdown viewer HTML for a new browser tab."""
    user_id = require_user_id(request)
    name = _safe_doc_name(filename)
    stem = Path(name).stem
    md_name = name if name.lower().endswith(".md") else f"{stem}.md"
    scope = _documents_folder_scope(kind)
    docs = Path(_documents_folder_dir(user_id, scope))
    md_path = docs / md_name
    if not md_path.is_file():
        alt_dirs = [
            _documents_folder_dir(user_id, folder)
            for folder in ("project", "drawing")
            if folder != scope
        ]
        for alt_str in alt_dirs:
            alt_docs = Path(alt_str)
            if (alt_docs / md_name).is_file():
                md_path = alt_docs / md_name
                break
        else:
            alt = Path(utils.documents_md_local_artifacts_path(md_name, user_id=user_id))
            if alt.is_file():
                md_path = alt
            else:
                raise HTTPException(
                    status_code=404, detail=f"Markdown not found: {md_name}"
                )

    if bool(download):
        return FileResponse(
            md_path,
            media_type="text/markdown; charset=utf-8",
            filename=md_name,
            content_disposition_type="attachment",
        )

    published = utils.publish_documents_markdown_to_artifacts(
        md_path, user_id=user_id, file_name=md_name
    )
    raw_url = (published or {}).get("url") or ""

    try:
        text = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = md_path.read_text(encoding="utf-8", errors="replace")

    download_href = (
        f"/api/documents/documents/{quote(md_name)}/markdown"
        f"?kind={quote(kind)}&download=1"
    )
    actions = [
        f'<a class="action" href="{html.escape(download_href, quote=True)}">Download</a>',
    ]
    if raw_url:
        actions.append(
            f'<a class="action" href="{html.escape(raw_url, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">Raw</a>'
        )
    from application.viewer_html import build_markdown_viewer_page

    page = build_markdown_viewer_page(
        md_name,
        text,
        topbar_right_html='<span style="display:flex;gap:14px;align-items:center">'
        + "".join(actions)
        + "</span>",
    )
    return HTMLResponse(content=page, media_type="text/html; charset=utf-8")


@router.post("/projects/presign")
def documents_projects_presign(body: DocumentsPresignRequest, request: Request) -> dict:
    """Return a short-lived S3 PUT URL for project document uploads."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    _assert_documents_doc_size(body.size)

    try:
        presign = utils.generate_documents_projects_presigned_put(
            body.file_name, user_id=user_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"업로드 URL 생성 실패: {exc}"
        ) from exc
    if not presign or not presign.get("upload_url"):
        raise HTTPException(status_code=500, detail="업로드 URL 생성 실패")

    return {
        "ok": True,
        "file_name": presign["file_name"],
        "original_filename": presign.get("original_filename") or body.file_name,
        "sanitized": bool(presign.get("sanitized")),
        "s3_key": presign["s3_key"],
        "content_type": presign.get("content_type"),
        "upload_url": presign["upload_url"],
        "headers": presign.get("headers") or {},
        "expires_in": presign.get("expires_in"),
        "projects_dir": utils.documents_projects_dir(user_id),
        "docs_dir": utils.documents_projects_dir(user_id),
    }


@router.post("/projects/complete")
def documents_projects_complete(body: DocumentsCompleteRequest, request: Request) -> dict:
    """Confirm a presigned PUT and materialize into ``documents/projects/``."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    _assert_documents_doc_size(body.size)

    try:
        utils._ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe_name = sanitize_documents_filename(body.file_name)
    except Exception:
        safe_name = Path(body.file_name).name

    expected_key = utils.documents_projects_s3_key(safe_name, user_id=user_id)
    key = (body.s3_key or "").strip()
    if key != expected_key:
        raise HTTPException(status_code=400, detail="Invalid upload target")

    head = utils.head_session_upload_object(key)
    if not head:
        raise HTTPException(status_code=404, detail="Uploaded object not found")
    content_length = int(head.get("content_length") or 0)
    if content_length <= 0:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다.")
    if body.size is not None and content_length != body.size:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Uploaded size mismatch (expected {body.size}, got {content_length})"
            ),
        )
    _assert_documents_doc_size(content_length)

    result = utils.materialize_documents_projects_from_s3(
        key,
        safe_name,
        user_id=user_id,
        original_filename=body.original_filename or body.file_name,
    )
    if not result:
        raise HTTPException(
            status_code=500, detail="Failed to save file to documents/projects"
        )

    return {
        "ok": True,
        "documents_dir": result["documents_dir"],
        "projects_dir": result.get("projects_dir"),
        "docs_dir": result.get("projects_dir"),
        "raw_dir": result.get("projects_dir"),
        "saved": result["saved"],
        "count": result.get("count", 1),
        "s3_key": key,
        "files": utils.list_documents_project_files(user_id),
        **_load_project_list_payload(user_id),
    }


async def _upload_documents_project_multipart(request: Request, file: UploadFile) -> dict:
    """Legacy multipart upload for project docs (small files only)."""
    user_id = require_user_id(request)
    name = (file.filename or "").strip() or "upload.bin"
    try:
        data = await file.read()
    finally:
        try:
            await file.close()
        except Exception:
            pass

    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다.")
    if len(data) > _MAX_MULTIPART_DOC_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"파일이 너무 큽니다: {name} "
                f"(최대 {_MAX_MULTIPART_DOC_BYTES // (1024 * 1024)}MB). "
                "브라우저를 강력 새로고침(Cmd+Shift+R / Ctrl+Shift+R)한 뒤 "
                "다시 업로드하세요. (presigned S3 업로드로 전환됩니다)"
            ),
        )

    try:
        result = utils.save_documents_project_upload(name, data, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"문서 저장 실패: {exc}",
        ) from exc

    return {
        "documents_dir": result["documents_dir"],
        "projects_dir": result.get("projects_dir"),
        "docs_dir": result.get("projects_dir") or result.get("docs_dir"),
        "raw_dir": result.get("projects_dir") or result.get("docs_dir"),
        "saved": result["saved"],
        "count": result["count"],
        "files": utils.list_documents_project_files(user_id),
        **_load_project_list_payload(user_id),
    }


@router.post("/projects")
async def upload_documents_project_file(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Legacy multipart path — UI uses ``/projects/presign`` + ``/projects/complete``."""
    return await _upload_documents_project_multipart(request, file)


@router.post("/drawings/presign")
def documents_drawings_presign(body: DocumentsPresignRequest, request: Request) -> dict:
    """Return a short-lived S3 PUT URL for drawing document uploads."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    _assert_documents_doc_size(body.size)

    try:
        presign = utils.generate_documents_drawings_presigned_put(
            body.file_name, user_id=user_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"업로드 URL 생성 실패: {exc}"
        ) from exc
    if not presign or not presign.get("upload_url"):
        raise HTTPException(status_code=500, detail="업로드 URL 생성 실패")

    return {
        "ok": True,
        "file_name": presign["file_name"],
        "original_filename": presign.get("original_filename") or body.file_name,
        "sanitized": bool(presign.get("sanitized")),
        "s3_key": presign["s3_key"],
        "content_type": presign.get("content_type"),
        "upload_url": presign["upload_url"],
        "headers": presign.get("headers") or {},
        "expires_in": presign.get("expires_in"),
        "drawings_dir": utils.documents_drawings_dir(user_id),
        "docs_dir": utils.documents_drawings_dir(user_id),
    }


@router.post("/drawings/complete")
def documents_drawings_complete(body: DocumentsCompleteRequest, request: Request) -> dict:
    """Confirm a presigned PUT and materialize into ``documents/drawings/``."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    _assert_documents_doc_size(body.size)

    try:
        utils._ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe_name = sanitize_documents_filename(body.file_name)
    except Exception:
        safe_name = Path(body.file_name).name

    expected_key = utils.documents_drawings_s3_key(safe_name, user_id=user_id)
    key = (body.s3_key or "").strip()
    if key != expected_key:
        raise HTTPException(status_code=400, detail="Invalid upload target")

    head = utils.head_session_upload_object(key)
    if not head:
        raise HTTPException(status_code=404, detail="Uploaded object not found")
    content_length = int(head.get("content_length") or 0)
    if content_length <= 0:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다.")
    if body.size is not None and content_length != body.size:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Uploaded size mismatch (expected {body.size}, got {content_length})"
            ),
        )
    _assert_documents_doc_size(content_length)

    result = utils.materialize_documents_drawings_from_s3(
        key,
        safe_name,
        user_id=user_id,
        original_filename=body.original_filename or body.file_name,
    )
    if not result:
        raise HTTPException(
            status_code=500, detail="Failed to save file to documents/drawings"
        )

    return {
        "ok": True,
        "documents_dir": result["documents_dir"],
        "drawings_dir": result.get("drawings_dir"),
        "docs_dir": result.get("drawings_dir"),
        "raw_dir": result.get("drawings_dir"),
        "saved": result["saved"],
        "count": result.get("count", 1),
        "s3_key": key,
        "files": utils.list_documents_drawing_files(user_id),
        **_load_drawings_list_payload(user_id),
    }


async def _upload_documents_drawing_multipart(request: Request, file: UploadFile) -> dict:
    """Legacy multipart upload for drawing docs (small files only)."""
    user_id = require_user_id(request)
    name = (file.filename or "").strip() or "upload.bin"
    try:
        data = await file.read()
    finally:
        try:
            await file.close()
        except Exception:
            pass

    if not data:
        raise HTTPException(status_code=400, detail="빈 파일은 업로드할 수 없습니다.")
    if len(data) > _MAX_MULTIPART_DOC_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"파일이 너무 큽니다: {name} "
                f"(최대 {_MAX_MULTIPART_DOC_BYTES // (1024 * 1024)}MB). "
                "브라우저를 강력 새로고침(Cmd+Shift+R / Ctrl+Shift+R)한 뒤 "
                "다시 업로드하세요. (presigned S3 업로드로 전환됩니다)"
            ),
        )

    try:
        result = utils.save_documents_drawing_upload(name, data, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"문서 저장 실패: {exc}",
        ) from exc

    return {
        "documents_dir": result["documents_dir"],
        "drawings_dir": result.get("drawings_dir"),
        "docs_dir": result.get("drawings_dir") or result.get("docs_dir"),
        "raw_dir": result.get("drawings_dir") or result.get("docs_dir"),
        "saved": result["saved"],
        "count": result["count"],
        "files": utils.list_documents_drawing_files(user_id),
        **_load_drawings_list_payload(user_id),
    }


@router.post("/drawings")
async def upload_documents_drawing_file(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Legacy multipart path — UI uses ``/drawings/presign`` + ``/drawings/complete``."""
    return await _upload_documents_drawing_multipart(request, file)


@router.post("/sync")
def sync_documents(
    request: Request,
    full: bool = Query(False),
    model: str | None = Query(None),
) -> dict:
    """Enqueue Documents sync for the user's documents directory."""
    user_id = require_user_id(request)
    utils.ensure_user_documents_dir(user_id)
    job = ensure_documents_sync(user_id, full=full, model=model)
    files = utils.list_documents_project_files(user_id) + utils.list_documents_drawing_files(
        user_id
    )
    return {
        "documents_dir": utils.get_user_documents_dir(user_id),
        "projects_dir": utils.documents_projects_dir(user_id),
        "drawings_dir": utils.documents_drawings_dir(user_id),
        "files": files,
        "exists": len(files) > 0,
        "foundation_model_parser_enabled": utils.is_documents_foundation_model_parser_enabled(
            user_id
        ),
        "parallel_processing_enabled": utils.is_documents_parallel_processing_enabled(
            user_id
        ),
        **_load_project_list_payload(user_id),
        **job,
    }
