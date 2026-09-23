import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api";

type DocKind = "project" | "drawing";

interface Props {
  onClose: () => void;
  /** Called after a file is uploaded so the parent can open Documents Sync. */
  onFileUploaded?: () => void;
}

export function DocumentsConfigureModal({ onClose, onFileUploaded }: Props) {
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [pendingKind, setPendingKind] = useState<DocKind | null>(null);
  const [uploadProgress, setUploadProgress] = useState<{
    current: number;
    total: number;
    name: string;
  } | null>(null);
  const [foundationModelParser, setFoundationModelParser] = useState(true);
  const [parallelProcessing, setParallelProcessing] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const projectInputRef = useRef<HTMLInputElement | null>(null);
  const drawingInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.getDocumentsConfig();
        if (cancelled) return;
        setFoundationModelParser(
          data.foundation_model_parser_enabled !== false,
        );
        setParallelProcessing(data.parallel_processing_enabled !== false);
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
  }, []);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && !busy) onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  async function uploadDoc(file: File, kind: DocKind) {
    if (kind === "drawing") {
      await api.uploadDocumentsDrawingFile(file);
    } else {
      await api.uploadDocumentsProjectFile(file);
    }
  }

  async function uploadDocs(files: File[], kind: DocKind) {
    await api.putDocumentsConfig({
      foundation_model_parser_enabled: foundationModelParser,
      parallel_processing_enabled: parallelProcessing,
    });
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      setUploadProgress({
        current: i + 1,
        total: files.length,
        name: file.name,
      });
      await uploadDoc(file, kind);
    }
  }

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await api.putDocumentsConfig({
        foundation_model_parser_enabled: foundationModelParser,
        parallel_processing_enabled: parallelProcessing,
      });

      if (pendingFiles.length > 0 && pendingKind) {
        await uploadDocs(pendingFiles, pendingKind);
        setPendingFiles([]);
        setPendingKind(null);
        setUploadProgress(null);
        if (projectInputRef.current) projectInputRef.current.value = "";
        if (drawingInputRef.current) drawingInputRef.current.value = "";
        onClose();
        onFileUploaded?.();
        return;
      }

      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handlePickFile(fileList: FileList | null, kind: DocKind) {
    if (!fileList || fileList.length === 0) return;
    const files = Array.from(fileList);
    const inputRef =
      kind === "drawing" ? drawingInputRef : projectInputRef;
    if (inputRef.current) inputRef.current.value = "";

    setPendingFiles(files);
    setPendingKind(kind);
    setError(null);
    setBusy(true);
    try {
      await uploadDocs(files, kind);
      setPendingFiles([]);
      setPendingKind(null);
      setUploadProgress(null);
      onClose();
      onFileUploaded?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setUploadProgress(null);
    }
  }

  function renderDocSection(kind: DocKind) {
    const label =
      kind === "drawing" ? "Drawing 문서 추가" : "Project 문서 추가";
    const inputRef =
      kind === "drawing" ? drawingInputRef : projectInputRef;
    const sectionFiles =
      pendingKind === kind ? pendingFiles : [];
    const isPending = sectionFiles.length > 0;

    return (
      <div className="documents-configure-doc-section">
        <div className="documents-configure-section-label">{label}</div>
        <div className="documents-configure-docs">
          <input
            ref={inputRef}
            type="file"
            multiple
            className="documents-configure-file-input"
            accept=".pdf,.md,.txt,.markdown,.rst,.docx,.pptx,.csv,.json,.html,.htm,application/pdf,text/plain,text/markdown"
            disabled={busy}
            onChange={(e) => {
              void handlePickFile(e.target.files, kind);
            }}
          />
          <div className="documents-configure-docs-actions">
            <button
              type="button"
              className="modal-btn-secondary"
              disabled={busy}
              onClick={() => inputRef.current?.click()}
            >
              파일 선택…
            </button>
          </div>
          {isPending ? (
            <ul className="documents-configure-docs-list">
              {sectionFiles.map((file) => (
                <li key={`${file.name}-${file.size}-${file.lastModified}`}>
                  <span className="documents-configure-docs-name">{file.name}</span>
                  <span className="documents-configure-docs-meta">
                    {(file.size / 1024).toFixed(1)} KB
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="documents-configure-docs-empty">
              파일을 선택하면 저장 후 Sync를 수행합니다. (Ctrl/Cmd 또는 Shift로
              여러 파일 선택 가능)
            </p>
          )}
        </div>
      </div>
    );
  }

  function primaryButtonLabel(): string {
    if (!busy) return "저장";
    if (uploadProgress) {
      return `업로드 중 (${uploadProgress.current}/${uploadProgress.total})…`;
    }
    return "저장 중…";
  }

  return createPortal(
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="documents-configure-title"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="modal documents-configure-modal">
        <h2 id="documents-configure-title">Documents Configure</h2>
        {loading ? (
          <p className="documents-configure-muted">불러오는 중…</p>
        ) : (
          <>
            <label className="documents-configure-toggle">
              <span className="documents-configure-toggle-title">
                Foundation Model Parser
              </span>
              <input
                type="checkbox"
                checked={foundationModelParser}
                disabled={busy}
                onChange={(e) => setFoundationModelParser(e.target.checked)}
              />
            </label>

            <label className="documents-configure-toggle">
              <span className="documents-configure-toggle-title">
                Parallel Processing
              </span>
              <input
                type="checkbox"
                checked={parallelProcessing}
                disabled={busy}
                onChange={(e) => setParallelProcessing(e.target.checked)}
              />
            </label>

            {renderDocSection("project")}
            {renderDocSection("drawing")}
          </>
        )}
        {error ? (
          <p className="modal-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="modal-actions">
          <button
            type="button"
            className="modal-btn-secondary"
            disabled={busy}
            onClick={onClose}
          >
            닫기
          </button>
          <button
            type="button"
            className="modal-btn-primary"
            disabled={busy || loading}
            onClick={() => void handleSave()}
          >
            {primaryButtonLabel()}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
