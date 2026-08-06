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

export interface SessionInfo {
  user_id: string;
  name?: string | null;
  picture?: string | null;
  llm_gateway_ready?: boolean;
  knowledge_graph_enabled?: boolean;
}

export const api = {
  getSession: () => request<SessionInfo | null>("/api/session"),
  setSession: (credential: string) =>
    request<SessionInfo>("/api/session", {
      method: "POST",
      body: JSON.stringify({ credential }),
    }),
  setSessionWithAccessToken: (access_token: string) =>
    request<SessionInfo>("/api/session", {
      method: "POST",
      body: JSON.stringify({ access_token }),
    }),
  setLocalSession: (user_id: string) =>
    request<SessionInfo>("/api/session", {
      method: "POST",
      body: JSON.stringify({ user_id }),
    }),
  clearSession: () => request<void>("/api/session", { method: "DELETE" }),
  getConfig: () => request<AppConfig>("/api/config"),
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
