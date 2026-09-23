import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type DocumentsDocument } from "../api";
import { copyDocumentForChat } from "../pendingLoadFile";

interface Props {
  onClose: () => void;
  /** ``project`` → project_list.json, ``drawing`` → drawings_list.json */
  kind?: "project" | "drawing";
}

function formatBytes(bytes: number | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCreatedAt(value: string | undefined): string | null {
  const raw = (value || "").trim();
  if (!raw) return null;
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function documentSublineParts(
  doc: DocumentsDocument,
  extra: Array<string | null | undefined> = [],
): string {
  const timestamp =
    doc.status === "extracted" || doc.status === "saved"
      ? formatCreatedAt(doc.extracted_at) || formatCreatedAt(doc.created_at)
      : formatCreatedAt(doc.created_at);
  const parts = [timestamp, ...extra, formatBytes(doc.bytes)].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

function openInNewTab(url: string) {
  window.open(url, "_blank", "noopener,noreferrer");
}

/** Runtime workspace path for agent chat (preferred over CloudFront URL). */
function markdownChatPath(doc: DocumentsDocument): string | null {
  const ws = (doc.md_workspace_path || "").trim();
  if (ws) return ws;
  const localArtifacts = (doc.md_local_artifacts || "").trim();
  if (localArtifacts.startsWith("/mnt/workspace/")) return localArtifacts;
  return null;
}

/** CloudFront URL fallback when workspace path is unavailable. */
function markdownCopyUrl(doc: DocumentsDocument): string | null {
  const url = (doc.md_url || "").trim();
  return url || null;
}

function markdownFileName(doc: DocumentsDocument): string | null {
  const fromField = (doc.md_file || "").trim();
  if (fromField) return fromField;
  const local = (doc.md_path || "").trim();
  if (local) return local.split("/").pop() || local;
  return null;
}


function documentKey(doc: DocumentsDocument): string {
  return doc.filename || doc.md_file || doc.display_name || "doc";
}

export function DocumentsListModal({ onClose, kind = "project" }: Props) {
  const [documents, setDocuments] = useState<DocumentsDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const title = kind === "drawing" ? "Drawings" : "Projects";
  const emptyHint =
    kind === "drawing"
      ? "등록된 Drawing 문서가 없습니다. Configure에서 Drawing 문서를 추가한 뒤 Sync 하세요."
      : "등록된 Project 문서가 없습니다. Configure에서 Project 문서를 추가한 뒤 Sync 하세요.";

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data =
          kind === "drawing"
            ? await api.getDocumentsDrawingList(true)
            : await api.getDocumentsProjectList(true);
        if (cancelled) return;
        setDocuments(data.documents ?? []);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [kind]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  function openMarkdown(doc: DocumentsDocument) {
    const url = doc.md_viewer_url;
    if (!url) return;
    openInNewTab(url);
  }

  function openPdf(doc: DocumentsDocument) {
    // Prefer API route (CloudFront redirect when object exists, else local stream).
    const url = doc.pdf_api_url || doc.pdf_url;
    if (!url) return;
    openInNewTab(url);
  }



  async function copyMarkdownPath(doc: DocumentsDocument) {
    const chatPath = markdownChatPath(doc);
    const url = markdownCopyUrl(doc);
    const attachPath = chatPath || url;
    if (!attachPath) return;
    const name = markdownFileName(doc) || attachPath.split("/").pop() || attachPath;
    const mdBytes = Number(doc.md_bytes);
    // Attach chip immediately + stage for Load files; prefer workspace path for runtime.
    copyDocumentForChat({
      path: attachPath,
      name,
      size: Number.isFinite(mdBytes) && mdBytes > 0 ? mdBytes : 0,
    });
    try {
      await navigator.clipboard.writeText(attachPath);
    } catch {
      // Clipboard is optional; chat attachment still works via event.
    }
    onClose();
  }


  async function deleteDocument(doc: DocumentsDocument) {
    const filename = (doc.filename || "").trim();
    if (!filename) {
      setError("삭제할 파일명이 없습니다.");
      return;
    }
    const label =
      doc.display_name ||
      doc.title ||
      doc.original_filename ||
      filename;
    const kindLabel = kind === "drawing" ? "Drawing" : "Project";
    const confirmed = window.confirm(
      `"${label}" ${kindLabel} 문서와 관련 파일(원본·JSON·Markdown)을 삭제할까요?\n이 작업은 되돌릴 수 없습니다.`,
    );
    if (!confirmed) return;

    const key = documentKey(doc);
    setDeletingKey(key);
    setError(null);
    try {
      await api.deleteDocumentsDocument(filename, kind);
      setDocuments((prev) =>
        prev.filter((d) => (d.filename || "").trim() !== filename),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingKey(null);
    }
  }

  return createPortal(
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="documents-doc-list-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal documents-doc-list-modal">
        <h2 id="documents-doc-list-title">{title}</h2>
        {loading ? (
          <p className="documents-configure-muted">문서 목록을 불러오는 중…</p>
        ) : documents.length === 0 ? (
          error ? (
            <p className="modal-error" role="alert">
              {error}
            </p>
          ) : (
            <p className="documents-configure-docs-empty">{emptyHint}</p>
          )
        ) : (
          <>
            {error ? (
              <p className="modal-error" role="alert">
                {error}
              </p>
            ) : null}
            <ul className="documents-doc-list">
            {documents.map((doc) => {
              const key = documentKey(doc);
              const itemTitle =
                doc.display_name ||
                doc.title ||
                doc.original_filename ||
                doc.filename ||
                doc.md_file ||
                "document";
              const canDelete = Boolean((doc.filename || "").trim());
              const isDeleting = deletingKey === key;
              const canMd = Boolean(doc.md_available && doc.md_viewer_url);
              const canPdf = Boolean(
                doc.pdf_available && (doc.pdf_api_url || doc.pdf_url),
              );
              const mdCopyUrl = markdownCopyUrl(doc);
              const mdChatPath = markdownChatPath(doc);
              const canCopy = Boolean(mdChatPath || mdCopyUrl);
              return (
                <li key={key} className="documents-doc-list-item">
                  <div className="documents-doc-list-meta">
                    <span className="documents-doc-list-name" title={itemTitle}>
                      {itemTitle}
                    </span>
                    <span className="documents-doc-list-sub">
                      {documentSublineParts(doc, [doc.status || "—"])}
                    </span>
                  </div>
                  <div className="documents-doc-list-actions">
                    <button
                      type="button"
                      className="documents-doc-list-btn"
                      disabled={!canMd || isDeleting}
                      title={
                        canMd
                          ? "Markdown viewer (새 탭)"
                          : "Markdown 없음 (Sync 필요)"
                      }
                      onClick={() => openMarkdown(doc)}
                    >
                      Markdown
                    </button>
                    <button
                      type="button"
                      className="documents-doc-list-btn"
                      disabled={!canPdf || isDeleting}
                      title={
                        canPdf ? "PDF (새 탭)" : "PDF 파일을 찾을 수 없습니다"
                      }
                      onClick={() => openPdf(doc)}
                    >
                      PDF
                    </button>
                    <button
                      type="button"
                      className="documents-doc-list-btn documents-doc-list-btn-success"
                      disabled={!canCopy || isDeleting}
                      title={
                        canCopy
                          ? mdChatPath
                            ? `입력창에 Markdown 첨부 + Runtime 경로 복사\n${mdChatPath}`
                            : `입력창에 Markdown 첨부 + CloudFront URL 복사\n${mdCopyUrl}`
                          : "Markdown 경로 없음 (Sync 후 sharing_url 설정 확인)"
                      }
                      onClick={() => void copyMarkdownPath(doc)}
                    >
                      복사
                    </button>
                    <button
                      type="button"
                      className="documents-doc-list-btn documents-doc-list-btn-danger"
                      disabled={!canDelete || isDeleting}
                      title={
                        canDelete
                          ? "원본·JSON·Markdown 및 목록에서 삭제"
                          : "삭제할 파일명이 없습니다"
                      }
                      onClick={() => void deleteDocument(doc)}
                    >
                      {isDeleting ? "삭제 중…" : "삭제"}
                    </button>
                  </div>
                </li>
              );
            })}
            </ul>
          </>
        )}
        <div className="modal-actions">
          <button
            type="button"
            className="modal-btn-secondary"
            onClick={onClose}
          >
            닫기
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
