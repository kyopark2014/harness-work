export interface Task {
  id: string;
  user_id: string;
  title: string;
  runtime_session_id: string;
  model_name: string;
  skills: string[];
  mcp_servers: string[];
  guardrail_enabled: boolean;
  llm_gateway_enabled: boolean;
  memory_enabled: boolean;
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface ToolEvent {
  type: "text" | "tool" | "tool_result" | "info";
  tool?: string;
  input?: unknown;
  toolUseId?: string;
  data?: string;
}

export interface Message {
  id: string;
  task_id: string;
  role: "user" | "assistant";
  content: string;
  images: string[];
  tool_events: ToolEvent[];
  created_at: string;
}

export interface AppConfig {
  projectName: string;
  google_client_id?: string;
  local_auth_bypass?: boolean;
  is_admin?: boolean;
  skills: string[];
  mcp_servers: string[];
  models: string[];
  gateway_models?: string[];
  default_model: string;
  default_gateway_model?: string;
  default_skills: string[];
  default_mcp_servers: string[];
  llm_gateway_configured?: boolean;
}

export interface StreamEvent {
  type: "token" | "text" | "tool" | "tool_result" | "info" | "done" | "error";
  data?: string;
  content?: string;
  images?: string[];
  tool_events?: ToolEvent[];
  tool?: string;
  input?: unknown;
  toolUseId?: string;
}
