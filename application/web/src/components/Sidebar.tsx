import { useEffect, useRef, useState } from "react";
import { formatBrandTitle } from "../formatBrandTitle";
import { useTheme } from "../hooks/useTheme";
import type { Theme } from "../theme";
import type { AppConfig, Task } from "../types";
import { ConfigDrawer } from "./ConfigDrawer";
import { TaskListItem } from "./TaskListItem";
import {
  AppearanceIcon,
  ChevronIcon,
  LogoutIcon,
  McpIcon,
  ModelIcon,
  NewTaskIcon,
  SettingsIcon,
  SkillIcon,
  CloseIcon,
} from "./SidebarIcons";

type DrawerKind = "skill" | "mcp" | "model" | "appearance" | null;

const THEME_OPTIONS = ["Light", "Dark"] as const;

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
  onClose: () => void;
  onNewTask: () => void;
  onSelectTask: (id: string) => void;
  onOpenDrawer: (kind: DrawerKind) => void;
  onCloseDrawer: () => void;
  onPatchTask: (taskId: string, patch: Partial<Task>) => void | Promise<void>;
  onDeleteTask: (taskId: string) => void;
  onLogout: () => void;
}

export function Sidebar({
  userId,
  tasks,
  activeTask,
  config,
  drawer,
  open,
  onClose,
  onNewTask,
  onSelectTask,
  onOpenDrawer,
  onCloseDrawer,
  onPatchTask,
  onDeleteTask,
  onLogout,
}: Props) {
  const skillBtnRef = useRef<HTMLButtonElement>(null);
  const mcpBtnRef = useRef<HTMLButtonElement>(null);
  const modelBtnRef = useRef<HTMLButtonElement>(null);
  const appearanceBtnRef = useRef<HTMLButtonElement>(null);
  const settingsSectionRef = useRef<HTMLDivElement>(null);
  const [settingsExpanded, setSettingsExpanded] = useState(false);
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
      collapseSettings();
    }

    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [settingsExpanded, onCloseDrawer]);

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

  return (
    <>
      <aside className={`sidebar${open ? " sidebar-panel-open" : ""}`}>
        <div className="sidebar-header">
          <div className="brand-row">
            <div className="brand" title={brandTitle} aria-label={brandTitle}>
              {brandTitle}
            </div>
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
    </>
  );
}
