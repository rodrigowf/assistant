import type { SessionInfo, SessionDetail, MessagePreview } from "../types";

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
