import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../api";
import { formatBrandTitle } from "../formatBrandTitle";
import { useTheme } from "../hooks/useTheme";
import type { Theme } from "../theme";
import type { AppConfig, Task } from "../types";
import { ConfigDrawer } from "./ConfigDrawer";
import { DocumentsConfigureModal } from "./DocumentsConfigureModal";
import { DocumentsListModal } from "./DocumentsListModal";
import { KnowledgeGraphModal } from "./KnowledgeGraphModal";
import { SyncProgressModal } from "./SyncProgressModal";
import { TaskListItem } from "./TaskListItem";
import {
  AppearanceIcon,
  ChevronIcon,
  DocumentsIcon,
  KnowledgeGraphIcon,
  LogoutIcon,
  McpIcon,
  ModelIcon,
  NewTaskIcon,
  SettingsIcon,
  SkillIcon,
  CloseIcon,
  GuardrailIcon,
} from "./SidebarIcons";

type DrawerKind = "skill" | "mcp" | "model" | "appearance" | "documents" | null;

const THEME_OPTIONS = ["Light", "Dark"] as const;
const DOCUMENTS_OPTIONS = ["Projects", "Drawings", "Configure"] as const;

function themeToLabel(theme: Theme): string {
  return theme === "light" ? "Light" : "Dark";
}

function labelToTheme(label: string): Theme {
  return label === "Light" ? "light" : "dark";
}

interface Props {
  userId: string;
  tasks: Task[];
  activeTask: Task | null;
  config: AppConfig | null;
  drawer: DrawerKind;
  open: boolean;
  knowledgeGraphEnabled?: boolean;
  onClose: () => void;
  onNewTask: () => void;
  onSelectTask: (id: string) => void;
  onOpenDrawer: (kind: DrawerKind) => void;
  onCloseDrawer: () => void;
  onPatchTask: (taskId: string, patch: Partial<Task>) => void | Promise<void>;
  onDeleteTask: (taskId: string) => void;
  onLogout: () => void;
  onPatchKnowledgeGraphEnabled?: (enabled: boolean) => void | Promise<void>;
  sidebarResizing?: boolean;
  onSidebarResizeStart?: (e: ReactPointerEvent<HTMLDivElement>) => void;
  onSidebarResizeReset?: () => void;
}

export function Sidebar({
  userId,
  tasks,
  activeTask,
  config,
  drawer,
  open,
  knowledgeGraphEnabled = true,
  onClose,
  onNewTask,
  onSelectTask,
  onOpenDrawer,
  onCloseDrawer,
  onPatchTask,
  onDeleteTask,
  onLogout,
  onPatchKnowledgeGraphEnabled,
  sidebarResizing = false,
  onSidebarResizeStart,
  onSidebarResizeReset,
}: Props) {
  const skillBtnRef = useRef<HTMLButtonElement>(null);
  const mcpBtnRef = useRef<HTMLButtonElement>(null);
  const modelBtnRef = useRef<HTMLButtonElement>(null);
  const appearanceBtnRef = useRef<HTMLButtonElement>(null);
  const documentsBtnRef = useRef<HTMLButtonElement>(null);
  const settingsSectionRef = useRef<HTMLDivElement>(null);
  const [settingsExpanded, setSettingsExpanded] = useState(false);
  const [knowledgeGraphOpen, setKnowledgeGraphOpen] = useState(false);
  const [documentsConfigureOpen, setDocumentsConfigureOpen] = useState(false);
  const [documentsListOpen, setDocumentsListOpen] = useState(false);
  const [documentsListKind, setDocumentsListKind] = useState<"project" | "drawing">("project");
  const [documentsSyncBusy, setDocumentsSyncBusy] = useState(false);
  const [documentsSyncMessage, setDocumentsSyncMessage] = useState<string | null>(null);
  const [documentsSyncProgress, setDocumentsSyncProgress] = useState<{
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    page?: number | null;
    page_n?: number | null;
    pct?: number | null;
    aggregated?: boolean | null;
  } | null>(null);
  const [documentsSyncPopupOpen, setDocumentsSyncPopupOpen] = useState(false);
  const { theme, setTheme } = useTheme();
  const skills = activeTask?.skills ?? config?.default_skills ?? [];
  const mcpServers = activeTask?.mcp_servers ?? config?.default_mcp_servers ?? [];
  const modelName = activeTask?.model_name ?? config?.default_model ?? "";
  const brandTitle = formatBrandTitle(config?.projectName ?? "agent", userId);
  const pinnedTasks = tasks.filter((task) => task.pinned);
  const regularTasks = tasks.filter((task) => !task.pinned);
  const modelOptions = config?.models ?? [];

  function collapseSettings() {
    setSettingsExpanded(false);
    onCloseDrawer();
  }

  useEffect(() => {
    if (!settingsExpanded) return;

    function onPointerDown(e: MouseEvent) {
      const target = e.target;
      if (!(target instanceof Element)) return;
      if (settingsSectionRef.current?.contains(target)) return;
      if (target.closest(".config-popover")) return;
      if (
        target.closest(
          ".modal-overlay, .knowledge-graph-modal, .documents-configure-modal, .documents-doc-list-modal, .sync-progress-modal",
        )
      ) {
        return;
      }
      collapseSettings();
    }

    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [settingsExpanded, onCloseDrawer]);

  async function handleDocumentsAction(choice: string) {
    if (choice === "Configure") {
      setDocumentsConfigureOpen(true);
      onCloseDrawer();
      return;
    }
    if (choice === "Projects") {
      setDocumentsListKind("project");
      setDocumentsListOpen(true);
      onCloseDrawer();
      return;
    }
    if (choice === "Drawings") {
      setDocumentsListKind("drawing");
      setDocumentsListOpen(true);
      onCloseDrawer();
      return;
    }
    if (choice !== "Sync") return;

    setDocumentsSyncPopupOpen(true);
    setDocumentsSyncBusy(true);
    setDocumentsSyncMessage("Documents 동기화를 시작합니다…");
    try {
      const result = await api.syncDocuments(false, modelName || undefined);
      const status = result.status;
      // Match agentic-work: only ``unchanged`` means nothing to do.
      // ``ready`` here would incorrectly stop a just-queued job before polling.
      if (status === "error") {
        setDocumentsSyncBusy(false);
        setDocumentsSyncMessage(result.error || "Documents 동기화에 실패했습니다.");
      } else if (status === "unchanged") {
        setDocumentsSyncBusy(false);
        setDocumentsSyncMessage(
          result.message || "No files changed since last run. Nothing to update.",
        );
      } else {
        setDocumentsSyncBusy(true);
        setDocumentsSyncMessage(
          result.message || "Documents 동기화를 백그라운드에서 실행 중입니다.",
        );
      }
    } catch (err) {
      setDocumentsSyncBusy(false);
      setDocumentsSyncMessage(
        err instanceof Error ? err.message : "Documents 동기화에 실패했습니다.",
      );
    }
  }

  useEffect(() => {
    if (!documentsSyncBusy && !documentsSyncPopupOpen) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function pollDocumentsSync() {
      try {
        const next = await api.getDocumentsStatus();
        if (cancelled) return;
        const busy = next.status === "queued" || next.status === "running";
        setDocumentsSyncBusy(busy);
        if (next.progress) {
          setDocumentsSyncProgress(next.progress);
        }
        if (busy) {
          setDocumentsSyncMessage(
            next.message || "Documents 동기화를 백그라운드에서 실행 중입니다.",
          );
          timer = setTimeout(pollDocumentsSync, 1500);
          return;
        }
        if (next.status === "ready" || next.status === "unchanged") {
          setDocumentsSyncMessage(next.message || "Documents 동기화가 완료되었습니다.");
        } else if (next.status === "idle") {
          setDocumentsSyncMessage(
            next.message || documentsSyncMessage || "Documents 동기화가 완료되었습니다.",
          );
        } else {
          setDocumentsSyncMessage(next.error || "Documents 동기화에 실패했습니다.");
        }
      } catch {
        if (cancelled) return;
        if (documentsSyncBusy) {
          timer = setTimeout(pollDocumentsSync, 4000);
        }
      }
    }

    void pollDocumentsSync();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [documentsSyncBusy, documentsSyncPopupOpen]);

  function renderTask(task: Task, hidePinBadge = false) {
    return (
      <TaskListItem
        key={task.id}
        task={task}
        active={activeTask?.id === task.id}
        hidePinBadge={hidePinBadge}
        onSelect={() => {
          collapseSettings();
          onSelectTask(task.id);
        }}
        onDelete={() => onDeleteTask(task.id)}
        onRename={(title) => onPatchTask(task.id, { title })}
        onTogglePin={() => onPatchTask(task.id, { pinned: !task.pinned })}
      />
    );
  }

  function toggleDrawer(kind: Exclude<DrawerKind, null>) {
    onOpenDrawer(drawer === kind ? null : kind);
  }

  function handleDrawerClose() {
    onCloseDrawer();
    setSettingsExpanded(false);
  }

  function handleSettingApplied() {
    setSettingsExpanded(false);
  }

  return (
    <>
      <aside className={`sidebar${open ? " sidebar-panel-open" : ""}`}>
        <div className="sidebar-header">
          <div className="brand-row">
            <button
              type="button"
              className={`brand brand-graph-btn${knowledgeGraphEnabled ? "" : " is-disabled"}`}
              title={
                knowledgeGraphEnabled
                  ? "Knowledge Graph 보기"
                  : "Knowledge Graph가 꺼져 있습니다"
              }
              aria-label={
                knowledgeGraphEnabled
                  ? `${brandTitle} Knowledge Graph 보기`
                  : brandTitle
              }
              aria-disabled={!knowledgeGraphEnabled}
              onClick={() => {
                if (!knowledgeGraphEnabled) return;
                collapseSettings();
                setKnowledgeGraphOpen(true);
              }}
            >
              {brandTitle}
            </button>
            <div className="sidebar-header-actions">
              <button
                type="button"
                className="sidebar-close-btn"
                aria-label="메뉴 닫기"
                onClick={onClose}
              >
                <CloseIcon className="sidebar-icon" />
              </button>
              <button
                type="button"
                className="brand-logout-btn"
                aria-label="나가기"
                title="나가기"
                onClick={onLogout}
              >
                <LogoutIcon className="sidebar-icon" />
              </button>
            </div>
          </div>
        </div>

        <button
          type="button"
          className="sidebar-menu-btn"
          onClick={() => {
            collapseSettings();
            onNewTask();
          }}
        >
          <NewTaskIcon className="sidebar-icon" />
          <span>New task</span>
        </button>

        <div className="task-list">
          {pinnedTasks.length > 0 && (
            <div className="task-list-section">
              <div className="section-label">Pinned</div>
              {pinnedTasks.map((task) => renderTask(task, true))}
            </div>
          )}
          {regularTasks.length > 0 && (
            <div className="task-list-section">
              {pinnedTasks.length > 0 && <div className="section-label">Tasks</div>}
              {regularTasks.map((task) => renderTask(task))}
            </div>
          )}
        </div>

        <button
          ref={modelBtnRef}
          type="button"
          className={`sidebar-menu-btn${drawer === "model" ? " is-active" : ""}`}
          aria-expanded={drawer === "model"}
          aria-haspopup="dialog"
          title={modelName || "Model"}
          disabled={!activeTask}
          onClick={() => {
            setSettingsExpanded(false);
            if (drawer === "model") {
              onCloseDrawer();
            } else {
              onOpenDrawer("model");
            }
          }}
        >
          <ModelIcon className="sidebar-icon" />
          <span>{modelName || "Model"}</span>
        </button>

        <div
          ref={settingsSectionRef}
          className={`sidebar-section${settingsExpanded ? " is-expanded" : ""}`}
        >
          <button
            type="button"
            className="section-toggle"
            aria-expanded={settingsExpanded}
            onClick={() => {
              if (settingsExpanded) {
                collapseSettings();
                return;
              }
              onCloseDrawer();
              setSettingsExpanded(true);
            }}
          >
            <SettingsIcon className="sidebar-icon" />
            <span>Settings</span>
            <ChevronIcon className="section-chevron" />
          </button>
          {settingsExpanded && (
            <div className="sidebar-section-body">
              <button
                ref={skillBtnRef}
                type="button"
                className={`sidebar-menu-btn${drawer === "skill" ? " is-active" : ""}`}
                aria-expanded={drawer === "skill"}
                aria-haspopup="dialog"
                onClick={() => toggleDrawer("skill")}
                disabled={!activeTask}
              >
                <SkillIcon className="sidebar-icon" />
                <span>Skill ({skills.length})</span>
              </button>
              <button
                ref={mcpBtnRef}
                type="button"
                className={`sidebar-menu-btn${drawer === "mcp" ? " is-active" : ""}`}
                aria-expanded={drawer === "mcp"}
                aria-haspopup="dialog"
                onClick={() => toggleDrawer("mcp")}
                disabled={!activeTask}
              >
                <McpIcon className="sidebar-icon" />
                <span>MCP ({mcpServers.length})</span>
              </button>
              <label className="sidebar-menu-btn settings-toggle">
                <GuardrailIcon className="sidebar-icon" />
                <span>Guardrail</span>
                <input
                  type="checkbox"
                  checked={activeTask?.guardrail_enabled ?? false}
                  disabled={!activeTask}
                  onChange={(e) => {
                    if (!activeTask) return;
                    onPatchTask(activeTask.id, {
                      guardrail_enabled: e.target.checked,
                    });
                    handleSettingApplied();
                  }}
                />
              </label>
              <label className="sidebar-menu-btn settings-toggle">
                <KnowledgeGraphIcon className="sidebar-icon" />
                <span>Knowledge Graph</span>
                <input
                  type="checkbox"
                  checked={knowledgeGraphEnabled}
                  onChange={(e) => {
                    const enabled = e.target.checked;
                    void (async () => {
                      try {
                        await onPatchKnowledgeGraphEnabled?.(enabled);
                      } finally {
                        handleSettingApplied();
                      }
                    })();
                  }}
                />
              </label>
              <button
                ref={documentsBtnRef}
                type="button"
                className={`sidebar-menu-btn${drawer === "documents" || documentsSyncBusy ? " is-active" : ""}`}
                aria-expanded={drawer === "documents"}
                aria-haspopup="dialog"
                title={documentsSyncMessage ?? "Documents"}
                onClick={() => toggleDrawer("documents")}
              >
                <DocumentsIcon className="sidebar-icon" />
                <span>{documentsSyncBusy ? "Documents (Syncing…)" : "Documents"}</span>
              </button>
              <button
                ref={appearanceBtnRef}
                type="button"
                className={`sidebar-menu-btn${drawer === "appearance" ? " is-active" : ""}`}
                aria-expanded={drawer === "appearance"}
                aria-haspopup="dialog"
                onClick={() => toggleDrawer("appearance")}
              >
                <AppearanceIcon className="sidebar-icon" />
                <span>Appearance ({themeToLabel(theme)})</span>
              </button>
            </div>
          )}
        </div>

        {onSidebarResizeStart && (
          <div
            className={`sidebar-resizer${sidebarResizing ? " is-active" : ""}`}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize task panel"
            title="Drag to resize · double-click to reset"
            onPointerDown={onSidebarResizeStart}
            onDoubleClick={onSidebarResizeReset}
          />
        )}
      </aside>

      {drawer === "skill" && config && activeTask && (
        <ConfigDrawer
          title="Skill"
          options={config.skills}
          selected={skills}
          anchorEl={skillBtnRef.current}
          onChange={(next) => activeTask && onPatchTask(activeTask.id, { skills: next })}
          onClose={handleDrawerClose}
        />
      )}
      {drawer === "mcp" && config && activeTask && (
        <ConfigDrawer
          title="MCP"
          options={config.mcp_servers}
          selected={mcpServers}
          anchorEl={mcpBtnRef.current}
          onChange={(next) => activeTask && onPatchTask(activeTask.id, { mcp_servers: next })}
          onClose={handleDrawerClose}
        />
      )}
      {drawer === "model" && config && activeTask && (
        <ConfigDrawer
          title="Model"
          options={modelOptions}
          selected={modelName ? [modelName] : []}
          mode="single"
          anchorEl={modelBtnRef.current}
          onChange={(next) =>
            activeTask && next[0] && onPatchTask(activeTask.id, { model_name: next[0] })
          }
          onClose={onCloseDrawer}
        />
      )}
      {drawer === "appearance" && (
        <ConfigDrawer
          title="Appearance"
          options={[...THEME_OPTIONS]}
          selected={[themeToLabel(theme)]}
          mode="single"
          anchorEl={appearanceBtnRef.current}
          onChange={(next) => {
            if (next[0]) setTheme(labelToTheme(next[0]));
          }}
          onClose={handleDrawerClose}
        />
      )}
      {drawer === "documents" && (
        <ConfigDrawer
          title="Documents"
          options={[...DOCUMENTS_OPTIONS]}
          selected={[]}
          mode="single"
          anchorEl={documentsBtnRef.current}
          onChange={(next) => {
            if (next[0]) void handleDocumentsAction(next[0]);
          }}
          onClose={handleDrawerClose}
        />
      )}

      {knowledgeGraphOpen && knowledgeGraphEnabled && (
        <KnowledgeGraphModal
          userId={userId}
          title={`${brandTitle} Knowledge Graph`}
          onClose={() => setKnowledgeGraphOpen(false)}
        />
      )}

      {documentsConfigureOpen && (
        <DocumentsConfigureModal
          onClose={() => setDocumentsConfigureOpen(false)}
          onFileUploaded={() => {
            void handleDocumentsAction("Sync");
          }}
        />
      )}
      {documentsListOpen && (
        <DocumentsListModal
          kind={documentsListKind}
          onClose={() => setDocumentsListOpen(false)}
        />
      )}
      {documentsSyncPopupOpen && (
        <SyncProgressModal
          title="Documents Sync"
          busy={documentsSyncBusy}
          message={documentsSyncMessage}
          progress={documentsSyncProgress}
          onClose={() => setDocumentsSyncPopupOpen(false)}
        />
      )}
    </>
  );
}
