import type { AppConfig, Message, StreamEvent, Task } from "./types";
import { uiError, uiLog } from "./debug";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  uiLog(`api:${method} ${path}`);
  const res = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    uiError(`api:${method} ${path} failed`, { status: res.status, body: text });
    let message = text || res.statusText;
    try {
      const parsed = JSON.parse(text) as { detail?: string | { msg?: string }[] };
      if (typeof parsed.detail === "string" && parsed.detail) {
        message = parsed.detail;
      }
    } catch {
      // keep raw text
    }
    throw new Error(message);
  }
  if (res.status === 204) {
    uiLog(`api:${method} ${path} -> 204`);
    return undefined as T;
  }
  const text = await res.text();
  if (!text) {
    uiLog(`api:${method} ${path} -> empty`);
    return undefined as T;
  }
  const data = JSON.parse(text) as T;
  uiLog(`api:${method} ${path} -> ok`);
  return data;
}

export type GraphPattern = "pattern1" | "pattern2" | "pattern3";

export interface SessionInfo {
  user_id: string;
  name?: string | null;
  picture?: string | null;
  llm_gateway_ready?: boolean;
  knowledge_graph_enabled?: boolean;
  graph_pattern?: GraphPattern | string;
}

export interface GraphStatus {
  user_id: string;
  exists: boolean;
  path: string | null;
  status:
    | "idle"
    | "queued"
    | "running"
    | "ready"
    | "error"
    | "skipped_cooldown"
    | "disabled"
    | string;
  enabled?: boolean;
  error?: string | null;
  last_success_at?: string | null;
  cooldown_seconds?: number;
  next_eligible_at?: string | null;
}

export interface FileUploadResult {
  ok: boolean;
  file_name: string;
  s3_key: string;
  url: string;
  content_type?: string;
}

export interface RagUploadResult {
  ok: boolean;
  file_name: string;
  s3_key: string;
  url?: string;
  message: string;
}

export interface DocumentsStatus {
  documents_dir: string;
  projects_dir?: string;
  drawings_dir?: string;
  files?: Array<{ name: string; path: string; bytes: number; mtime?: number }>;
  exists?: boolean;
  status: "idle" | "queued" | "running" | "ready" | "error" | "unchanged" | string;
  foundation_model_parser_enabled?: boolean;
  parallel_processing_enabled?: boolean;
  error?: string | null;
  message?: string | null;
  last_success_at?: string | null;
  progress?: {
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    page?: number | null;
    page_n?: number | null;
    pct?: number | null;
    aggregated?: boolean | null;
  } | null;
}

export interface DocumentsConfig {
  documents_dir: string;
  projects_dir?: string;
  drawings_dir?: string;
  files?: Array<{ name: string; path: string; bytes: number; mtime?: number }>;
  foundation_model_parser_enabled?: boolean;
  parallel_processing_enabled?: boolean;
}

export interface DocumentsDocument {
  filename?: string;
  original_filename?: string;
  display_name?: string;
  md_file?: string;
  md_path?: string;
  source_path?: string;
  status?: string;
  bytes?: number;
  title?: string;
  pdf_available?: boolean;
  md_available?: boolean;
  md_bytes?: number | null;
  pdf_url?: string | null;
  pdf_api_url?: string | null;
  md_url?: string | null;
  md_workspace_path?: string | null;
  md_local_artifacts?: string | null;
  md_viewer_url?: string | null;
  md_published?: boolean;
  kind?: string;
  created_at?: string;
  updated_at?: string;
  extracted_at?: string;
}

export interface DocumentsListResult {
  documents_dir?: string;
  docs_dir?: string;
  projects_dir?: string;
  drawings_dir?: string;
  documents: DocumentsDocument[];
  doc_count?: number;
  doc_list?: string;
  doc_list_updated_at?: string | null;
  sharing_url?: string | null;
}

export interface DocumentsPresignResult {
  ok?: boolean;
  file_name: string;
  original_filename?: string;
  sanitized?: boolean;
  s3_key: string;
  content_type?: string;
  upload_url: string;
  headers?: Record<string, string>;
  expires_in?: number;
  docs_dir?: string;
}

export interface DocumentsUploadResult {
  documents_dir: string;
  docs_dir?: string;
  projects_dir?: string;
  drawings_dir?: string;
  raw_dir?: string;
  saved: {
    name: string;
    original_filename?: string;
    sanitized?: boolean;
    path: string;
    bytes: number;
    overwritten?: boolean;
  };
  count: number;
  files?: Array<{ name: string; path: string; bytes: number; mtime?: number }>;
  documents?: Array<Record<string, unknown>>;
  doc_count?: number;
  s3_key?: string;
}

export const api = {
  getSession: () => request<SessionInfo | null>("/api/session"),
  login: (username: string, password: string) =>
    request<SessionInfo>("/api/session/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  clearSession: () => request<void>("/api/session", { method: "DELETE" }),
  patchSessionSettings: (body: {
    knowledge_graph_enabled?: boolean;
    graph_pattern?: GraphPattern | string;
  }) =>
    request<SessionInfo>("/api/session/settings", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  getGraphStatus: () => request<GraphStatus>("/api/graph/status"),
  rebuildGraph: (force = false) =>
    request<GraphStatus>(`/api/graph/rebuild${force ? "?force=1" : ""}`, {
      method: "POST",
    }),
  getConfig: () => request<AppConfig>("/api/config"),
  getDocumentsStatus: () => request<DocumentsStatus>("/api/documents/status"),
  getDocumentsConfig: () => request<DocumentsConfig>("/api/documents/config"),
  putDocumentsConfig: (body: {
    foundation_model_parser_enabled?: boolean;
    parallel_processing_enabled?: boolean;
  }) =>
    request<DocumentsConfig>("/api/documents/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getDocumentsProjectList: (publishMd = true) =>
    request<DocumentsListResult>(
      `/api/documents/project-list${publishMd ? "" : "?publish_md=0"}`,
    ),
  getDocumentsDrawingList: (publishMd = true) =>
    request<DocumentsListResult>(
      `/api/documents/drawing-list${publishMd ? "" : "?publish_md=0"}`,
    ),
  deleteDocumentsDocument: (
    filename: string,
    kind: "project" | "drawing" = "project",
  ) =>
    request<{ ok: boolean }>(
      `/api/documents/documents/${encodeURIComponent(filename)}?kind=${encodeURIComponent(kind)}`,
      { method: "DELETE" },
    ),
  uploadDocumentsProjectFile: async (
    file: File,
  ): Promise<DocumentsUploadResult> => {
    uiLog("documents:project-upload start", { name: file.name, size: file.size });
    const presign = await request<DocumentsPresignResult>(
      "/api/documents/projects/presign",
      {
        method: "POST",
        body: JSON.stringify({
          file_name: file.name,
          size: file.size,
          content_type: file.type || undefined,
        }),
      },
    );
    if (!presign.upload_url || !presign.s3_key) {
      throw new Error("Presign succeeded but no upload URL was returned");
    }
    uiLog("documents:project-upload put start", {
      name: presign.file_name,
      s3_key: presign.s3_key,
      size: file.size,
    });
    let putRes: Response;
    try {
      putRes = await fetch(presign.upload_url, {
        method: "PUT",
        headers: presign.headers || { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      uiError("documents:project-upload put network error", { detail });
      throw new Error(`S3 upload failed: ${detail}`);
    }
    if (!putRes.ok) {
      const text = await putRes.text().catch(() => "");
      uiError("documents:project-upload put failed", {
        status: putRes.status,
        body: text,
      });
      const msgMatch = text.match(/<Message>([^<]+)<\/Message>/i);
      throw new Error(
        msgMatch?.[1] || `S3 upload failed (HTTP ${putRes.status})`,
      );
    }
    const data = await request<DocumentsUploadResult>(
      "/api/documents/projects/complete",
      {
        method: "POST",
        body: JSON.stringify({
          file_name: presign.file_name,
          s3_key: presign.s3_key,
          size: file.size,
          original_filename: file.name,
        }),
      },
    );
    uiLog("documents:project-upload ok", {
      name: data.saved?.name,
      s3_key: data.s3_key,
    });
    return data;
  },
  uploadDocumentsDrawingFile: async (
    file: File,
  ): Promise<DocumentsUploadResult> => {
    uiLog("documents:drawing-upload start", { name: file.name, size: file.size });
    const presign = await request<DocumentsPresignResult>(
      "/api/documents/drawings/presign",
      {
        method: "POST",
        body: JSON.stringify({
          file_name: file.name,
          size: file.size,
          content_type: file.type || undefined,
        }),
      },
    );
    if (!presign.upload_url || !presign.s3_key) {
      throw new Error("Presign succeeded but no upload URL was returned");
    }
    uiLog("documents:drawing-upload put start", {
      name: presign.file_name,
      s3_key: presign.s3_key,
      size: file.size,
    });
    let putRes: Response;
    try {
      putRes = await fetch(presign.upload_url, {
        method: "PUT",
        headers: presign.headers || { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      uiError("documents:drawing-upload put network error", { detail });
      throw new Error(`S3 upload failed: ${detail}`);
    }
    if (!putRes.ok) {
      const text = await putRes.text().catch(() => "");
      uiError("documents:drawing-upload put failed", {
        status: putRes.status,
        body: text,
      });
      const msgMatch = text.match(/<Message>([^<]+)<\/Message>/i);
      throw new Error(
        msgMatch?.[1] || `S3 upload failed (HTTP ${putRes.status})`,
      );
    }
    const data = await request<DocumentsUploadResult>(
      "/api/documents/drawings/complete",
      {
        method: "POST",
        body: JSON.stringify({
          file_name: presign.file_name,
          s3_key: presign.s3_key,
          size: file.size,
          original_filename: file.name,
        }),
      },
    );
    uiLog("documents:drawing-upload ok", {
      name: data.saved?.name,
      s3_key: data.s3_key,
    });
    return data;
  },
  syncDocuments: (full = false, model?: string) => {
    const params = new URLSearchParams();
    if (full) params.set("full", "1");
    if (model) params.set("model", model);
    const qs = params.toString();
    return request<DocumentsStatus>(`/api/documents/sync${qs ? `?${qs}` : ""}`, {
      method: "POST",
    });
  },
  listTasks: () => request<{ tasks: Task[] }>("/api/tasks"),
  createTask: (body: Partial<Task>) =>
    request<Task>("/api/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getTask: (id: string) => request<Task>(`/api/tasks/${id}`),
  patchTask: (id: string, body: Partial<Task>) =>
    request<Task>(`/api/tasks/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteTask: (id: string) =>
    request<{ ok: boolean }>(`/api/tasks/${id}`, { method: "DELETE" }),
  getMessages: (id: string) =>
    request<{ messages: Message[] }>(`/api/tasks/${id}/messages`),
  uploadToRag: async (file: File): Promise<RagUploadResult> => {
    uiLog("rag:upload start", { name: file.name, size: file.size });
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/rag/upload", {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const text = await res.text();
      uiError("rag:upload failed", { status: res.status, body: text });
      let message = text || res.statusText;
      try {
        const parsed = JSON.parse(text) as { detail?: string };
        if (typeof parsed.detail === "string" && parsed.detail) {
          message = parsed.detail;
        }
      } catch {
        // keep raw text
      }
      throw new Error(message);
    }
    const data = (await res.json()) as RagUploadResult;
    uiLog("rag:upload complete", data);
    return data;
  },
  uploadFile: async (file: File): Promise<FileUploadResult> => {
    uiLog("file:upload start", { name: file.name, size: file.size, type: file.type });
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/api/files/upload", {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!res.ok) {
      const text = await res.text();
      uiError("file:upload failed", { status: res.status, body: text });
      let message = text || res.statusText;
      try {
        const parsed = JSON.parse(text) as { detail?: string };
        if (typeof parsed.detail === "string" && parsed.detail) {
          message = parsed.detail;
        }
      } catch {
        // keep raw text
      }
      throw new Error(message);
    }
    const data = (await res.json()) as FileUploadResult;
    uiLog("file:upload complete", data);
    return data;
  },
  streamChat: async function* (
    taskId: string,
    prompt: string,
    files: string[] = [],
    signal?: AbortSignal,
  ): AsyncGenerator<StreamEvent> {
    uiLog("chat:stream start", { taskId, prompt, files });
    const res = await fetch(`/api/tasks/${taskId}/chat`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, files }),
      signal,
    });
    if (!res.ok || !res.body) {
      const body = await res.text();
      uiError("chat:stream request failed", { status: res.status, body });
      throw new Error("Chat request failed. Please try again.");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventCount = 0;

    try {
      while (true) {
        if (signal?.aborted) {
          throw new DOMException("Aborted", "AbortError");
        }
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";
        for (const part of parts) {
          const line = part
            .split("\n")
            .find((l) => l.startsWith("data:"));
          if (!line) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          const event = JSON.parse(payload) as StreamEvent;
          eventCount += 1;
          if (event.type === "token") {
            const text = event.data ?? "";
            uiLog("chat:sse token", { chars: text.length, preview: text.slice(0, 80) });
          } else if (event.type === "error") {
            uiError("chat:sse error", event);
          } else {
            uiLog(`chat:sse ${event.type}`, event);
          }
          yield event;
        }
      }
    } catch (err) {
      try {
        await reader.cancel();
      } catch {
        /* ignore cancel errors */
      }
      throw err;
    }

    uiLog("chat:stream end", { taskId, eventCount });
  },
};
