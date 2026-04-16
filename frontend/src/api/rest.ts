import type { SessionInfo, SessionDetail, MessagePreview, PaginatedMessages } from "../types";

const BASE = "/api";

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export function listSessions(): Promise<SessionInfo[]> {
  return json(`${BASE}/sessions`);
}

export function getSession(id: string): Promise<SessionDetail> {
  return json(`${BASE}/sessions/${id}`);
}

export function getPreview(id: string, max = 5): Promise<MessagePreview[]> {
  return json(`${BASE}/sessions/${id}/preview?max=${max}`);
}

export function getMessagesPaginated(id: string, limit = 50, before?: number): Promise<PaginatedMessages> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (before !== undefined) params.set("before", String(before));
  return json(`${BASE}/sessions/${id}/messages?${params}`);
}

export async function renameSession(id: string, title: string): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${id}/rename`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok && res.status !== 404) throw new Error(`${res.status}`);
}

export async function deleteSession(id: string): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${id}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) throw new Error(`${res.status}`);
}

export async function closePoolSession(localId: string): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${localId}/close`, { method: "POST" });
  if (!res.ok && res.status !== 404) throw new Error(`${res.status}`);
}

export interface PoolSession {
  local_id: string;
  sdk_session_id: string | null;
  status: string;
  cost: number;
  turns: number;
  title: string | null;
  is_orchestrator: boolean;
}

export function listPoolSessions(): Promise<PoolSession[]> {
  return json(`${BASE}/sessions/pool/live`);
}

export interface AuthStatusResponse {
  authenticated: boolean;
  auth_url?: string;
  headless: boolean;
}

export function authStatus(): Promise<AuthStatusResponse> {
  return json(`${BASE}/auth/status`);
}

export function authLogin(): Promise<AuthStatusResponse> {
  return json(`${BASE}/auth/login`, { method: "POST" });
}

export function authSetCredentials(credentialsJson: string): Promise<AuthStatusResponse> {
  return json(`${BASE}/auth/credentials`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credentials_json: credentialsJson }),
  });
}

// MCP Server Management

export interface McpServerConfig {
  type?: string;
  command: string;
  args?: string[];
  env?: Record<string, string>;
}

export interface McpServersResponse {
  servers: Record<string, McpServerConfig>;
  project_dir: string;
}

export function listMcpServers(): Promise<McpServersResponse> {
  return json(`${BASE}/mcp/servers`);
}

// Global config

export interface WorkingDirectoryEntry {
  id: string;           // Unique id: local path, or "host:path" for SSH
  path: string;         // Absolute path on the target machine
  label?: string | null;
  ssh_host?: string | null;
  ssh_user?: string | null;
  ssh_key?: string | null;  // Path to private key on the local machine
}

export interface AssistantConfig {
  working_directory: string;                     // active entry id
  working_directory_history: WorkingDirectoryEntry[];
  enabled_mcps: string[];
  disabled_skills: string[];
  disabled_agents: string[];
  chrome_extension: boolean;
  default_model: string;
}

export interface ConfigUpdate {
  working_directory?: string;                            // entry id to activate
  working_directory_history?: WorkingDirectoryEntry[];   // full list replacement
  enabled_mcps?: string[];
  disabled_skills?: string[];
  disabled_agents?: string[];
  chrome_extension?: boolean;
  default_model?: string;
}

export function getConfig(): Promise<AssistantConfig> {
  return json(`${BASE}/config`);
}

export function updateConfig(update: ConfigUpdate): Promise<AssistantConfig> {
  return json(`${BASE}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
}

// Model Configuration

export interface ModelInfo {
  provider: string;
  model_id: string;
  display_name: string;
  supports_audio: boolean;
  supports_vision: boolean;
  supports_tools: boolean;
  max_tokens: number;
}

export interface ModelsResponse {
  models: ModelInfo[];
  audio_capable_models: string[];
  default_model: string;
}

export function listModels(): Promise<ModelsResponse> {
  return json(`${BASE}/orchestrator/models`);
}

// Skills

export interface SkillInfo {
  name: string;
  description: string;
  dir: string;
}

export interface SkillsResponse {
  skills: SkillInfo[];
}

export function listSkills(): Promise<SkillsResponse> {
  return json(`${BASE}/skills`);
}

// Agents

export interface AgentInfo {
  name: string;
  description: string;
  file: string;
}

export interface AgentsResponse {
  agents: AgentInfo[];
}

export function listAgents(): Promise<AgentsResponse> {
  return json(`${BASE}/agents`);
}
