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

export async function deleteSession(id: string): Promise<void> {
  const res = await fetch(`${BASE}/sessions/${id}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) throw new Error(`${res.status}`);
}

export function authStatus(): Promise<{ authenticated: boolean }> {
  return json(`${BASE}/auth/status`);
}

export function authLogin(): Promise<{ authenticated: boolean }> {
  return json(`${BASE}/auth/login`, { method: "POST" });
}
