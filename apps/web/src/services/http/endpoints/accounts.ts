/** Settings → Accounts: sign-in status / methods of every service, and the `context/.env` keys. */
import { encodePath, http } from '../client';
import type {
  AccountActionResponse,
  AccountService,
  AccountsResponse,
  EnvChangeResponse,
  EnvListResponse,
  LoginFlow,
} from '../types';

const svc = (id: string): string => `/api/accounts/${encodePath(id)}`;

export const accounts = {
  /** Every service; runs `claude auth status` on the server (~1 s). */
  list: () => http.get<AccountsResponse>('/api/accounts', { timeoutMs: 30_000 }),
  get: (id: string) => http.get<AccountService>(svc(id), { timeoutMs: 30_000 }),
  /** Starts the CLI login; answers once the URL is known (or after ~12 s with `starting`). */
  startLogin: (id: string, method: string) => http.post<LoginFlow>(`${svc(id)}/login`, { json: { method }, timeoutMs: 30_000 }),
  login: (id: string) => http.get<LoginFlow>(`${svc(id)}/login`),
  /** Waits up to ~15 s for the CLI's verdict. */
  submitCode: (id: string, code: string) => http.post<LoginFlow>(`${svc(id)}/login/code`, { json: { code }, timeoutMs: 40_000 }),
  cancelLogin: (id: string) => http.del<LoginFlow>(`${svc(id)}/login`),
  credentials: (id: string, method: string, content: string) =>
    http.post<AccountActionResponse>(`${svc(id)}/credentials`, { json: { method, content }, timeoutMs: 40_000 }),
  logout: (id: string) => http.post<AccountActionResponse>(`${svc(id)}/logout`, { timeoutMs: 30_000 }),
  /** Tests the API key against the provider's free "list models" endpoint. */
  verify: (id: string) => http.post<AccountService>(`${svc(id)}/verify`, { timeoutMs: 30_000 }),
};

export const env = {
  list: () => http.get<EnvListResponse>('/api/env'),
  /** The only call that returns a full value. */
  reveal: (name: string) => http.post<{ name: string; value: string }>(`/api/env/${encodePath(name)}/reveal`),
  create: (name: string, value: string) => http.post<EnvChangeResponse>('/api/env', { json: { name, value } }),
  /** Create or update. */
  put: (name: string, value: string) => http.put<EnvChangeResponse>(`/api/env/${encodePath(name)}`, { json: { value } }),
  remove: (name: string) => http.del<EnvChangeResponse>(`/api/env/${encodePath(name)}`),
};
