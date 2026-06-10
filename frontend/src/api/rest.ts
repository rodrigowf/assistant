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

async function postJson<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json())?.detail || "";
    } catch {
      // ignore
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export function duplicateSession(id: string): Promise<{ session_id: string }> {
  return postJson(`${BASE}/sessions/${id}/duplicate`);
}

export function truncateSession(id: string, dropLastN: number): Promise<{ session_id: string }> {
  return postJson(`${BASE}/sessions/${id}/truncate`, { drop_last_n: dropLastN });
}

export function forkSession(id: string, dropLastN: number): Promise<{ session_id: string }> {
  return postJson(`${BASE}/sessions/${id}/fork`, { drop_last_n: dropLastN });
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
  ssh_key?: string | null;          // Path to private key on the local machine
  claude_config_dir?: string | null; // Override CLAUDE_CONFIG_DIR on the remote machine
}

// Session-harness id — the canonical list comes from /api/config/providers
// at runtime, so this is plain `string` rather than a closed union.  Use
// `listSessionProviders()` to populate UI pickers; never hardcode the set.
export type AssistantProvider = string;

export interface SessionProviderSpec {
  id: string;            // registry id (e.g. "claude", "qwen")
  label: string;         // human-readable picker label
  description: string;   // one-line description shown under the picker
}

export interface SessionProvidersResponse {
  providers: SessionProviderSpec[];
}

export function listSessionProviders(): Promise<SessionProvidersResponse> {
  return json(`${BASE}/config/providers`);
}

export interface AssistantConfig {
  working_directory: string;                     // active entry id
  working_directory_history: WorkingDirectoryEntry[];
  enabled_mcps: string[];
  chrome_extension: boolean;
  provider: AssistantProvider;                   // session provider for new chats
  default_model: string;
  summarizer_model: string;                       // "" = use backend default
  harness_model: Partial<Record<AssistantProvider, string>>; // per-provider; "" = CLI default
  default_voice_provider: string;
  default_voice_model: string;
  default_voice_name: string;
  default_voice_transcription_language: string;  // "" = auto-detect
  default_voice_endpoint: string;                // "vertex" | "aistudio" — google provider only
  voice_recording_enabled: boolean;              // save raw audio from voice sessions
  // Increment B (voice subsystem refactor): user-tunable VAD knobs.
  // Defaults equal documented Silero constants exactly.
  voice_vad_threshold: number;                   // 0.15–0.50, default 0.28
  voice_vad_min_silence_ms: number;              // 800–5000, default 2500
  voice_mic_gain: number;                        // 0.5–2.0, default 1.0
}

export interface ConfigUpdate {
  working_directory?: string;                            // entry id to activate
  working_directory_history?: WorkingDirectoryEntry[];   // full list replacement
  enabled_mcps?: string[];
  chrome_extension?: boolean;
  provider?: AssistantProvider;
  default_model?: string;
  summarizer_model?: string;
  harness_model?: Partial<Record<AssistantProvider, string>>; // shallow-merged server-side
  default_voice_provider?: string;
  default_voice_model?: string;
  default_voice_name?: string;
  default_voice_transcription_language?: string;
  default_voice_endpoint?: string;
  voice_recording_enabled?: boolean;
  voice_vad_threshold?: number;
  voice_vad_min_silence_ms?: number;
  voice_mic_gain?: number;
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

// Harness model catalog (currently Qwen only — Claude has no programmatic
// equivalent of ~/.qwen/settings.json, so we leave that picker hidden).

export interface QwenModelInfo {
  id: string;
  display_name: string;
  provider: string;             // key under modelProviders in settings.json
  base_url: string | null;
  context_window: number | null;
  supports_vision: boolean;
  supports_video: boolean;
  supports_thinking: boolean;
}

export interface QwenModelsResponse {
  models: QwenModelInfo[];
}

export function listQwenHarnessModels(): Promise<QwenModelsResponse> {
  return json(`${BASE}/config/harness/qwen/models`);
}

// Voice Provider Configuration

export interface VoiceEntry {
  id: string;
  label: string;
  description: string;
}

export interface TranscriptionLanguageEntry {
  id: string;          // ISO code, or "" for auto-detect
  label: string;
  description: string;
}

export interface VoiceModelEntry {
  id: string;
  label: string;
  voice: string;             // default voice
  voices: VoiceEntry[];      // selectable voices
  transcription_languages: TranscriptionLanguageEntry[];
  default_transcription_language: string;
  default: boolean;
}

export interface VoiceModelsResponse {
  providers: Record<string, VoiceModelEntry[]>;
  default_provider: string;
  default_model: string;
}

export function listVoiceModels(): Promise<VoiceModelsResponse> {
  return json(`${BASE}/orchestrator/voice/models`);
}

// Dynamic Gemini Live model listing. The Google provider has two
// backends — Vertex AI (default, stable) and AI Studio (legacy, prone
// to 1008 denials). Pass ``endpoint`` to pick which catalog to query;
// omit to use the backend's default. Backend caches per-endpoint for
// 60s. Returns ``{models: []}`` on any failure; callers should fall
// back to the static ``VOICE_MODELS["google"]`` from
// ``listVoiceModels()`` in that case.
export interface GoogleVoiceModelsResponse {
  models: VoiceModelEntry[];
}

export function listGoogleVoiceModels(
  endpoint?: string,
): Promise<GoogleVoiceModelsResponse> {
  const qs = endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : "";
  return json(`${BASE}/config/voice/google/models${qs}`);
}

// Per-session config

export interface SessionConfig {
  working_directory: string | null;  // null = inherit active from global
  enabled_mcps: string[] | null;     // null = inherit from global
  chrome_extension: boolean | null;  // null = inherit from global
  // Per-session provider pin.  null = inherit from global; otherwise the
  // resume path treats this as authoritative (you can't safely switch the
  // CLI behind an existing JSONL).
  provider: AssistantProvider | null;
  // Per-session harness model override.  null = inherit from global
  // harness_model[provider];  "" = explicit "CLI default" for this session;
  // any other string is the model id passed to the CLI.
  harness_model: string | null;
}

export type SessionConfigUpdate = Partial<SessionConfig>;

export function getSessionConfig(sessionId: string): Promise<SessionConfig> {
  return json(`${BASE}/sessions/${sessionId}/config`);
}

export function updateSessionConfig(sessionId: string, update: SessionConfigUpdate): Promise<SessionConfig> {
  return json(`${BASE}/sessions/${sessionId}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
}
