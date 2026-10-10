---
name: authentication
category: archie/harnesses
tags: [auth, accounts, oauth, api-key, env, credentials, headless, settings, claude-code, codex, gemini, qwen, model-studio]
created: 2026-10-09
modified: 2026-10-09
summary: How every service Archie uses signs in — methods per harness and API, where credentials live for the way Archie runs each CLI, how status is detected, how the backend drives each login on a headless server (Settings → Accounts), and the context/.env key manager.
source: curated (researched and probed against Claude Code 2.1.295, Codex CLI 0.161.0, Gemini CLI 0.63.0 and Qwen Code 0.25.0 on 2026-10-09)
references:
  - claude-code.md
  - codex-cli.md
  - gemini-cli.md
  - qwen-code.md
  - model-studio.md
  - registry.md
  - ../specs/12-client-protocol.md
  - ../clients/web.md
  - ../clients/android.md
  - ../architecture/backend.md
---

# Authentication (Settings → Accounts)

Settings → **Accounts** (web and Android) shows every service Archie signs in to, with its state
(signed in, method, account, plan, expiry) and every sign-in method the service supports, then the
`context/.env` key manager. Backend: `backend/manager/accounts/` (one `AccountService` per service,
the login-flow driver and the env manager) behind `backend/api/routes/accounts.py`
(`/api/accounts`, `/api/env`; spec 12 §8.1). The first-run AuthGate's "Sign in with Claude" uses the same link
sign-in (Claude `token` method); the Claude-only `/api/auth/*` routes stay for older clients
(`/api/auth/login` is refused while a link sign-in runs).

The backend runs 24/7 on a server **without a screen**; the user is on a phone or laptop. Every
method therefore works without a browser on the server: the backend runs the CLI's own login,
hands the user the URL (and a device code), and takes back whatever the provider's page shows.

## Method kinds

| Kind | What the user does | What the backend does |
|---|---|---|
| `link` | Opens the URL on any device, signs in, pastes back the code the page shows (or just approves a device code) | Spawns the CLI login (pipe or pseudo-terminal), parses URL / device code from its output, writes the pasted code to its stdin, watches for the verdict; one flow per service, 15 min timeout (5 for Gemini), process group killed on cancel / timeout / shutdown |
| `link` (cont.) | — | **The current login is never at risk**: `codex login` deletes `auth.json` and `claude auth login` blanks `.credentials.json` the moment they start (verified on 0.161.0 / 2.1.295), so every file-writing login runs against a throwaway home (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`, the Gemini CLI's `HOME`/`GEMINI_CLI_HOME` in a `mkdtemp` dir) and the new login is moved into place only on success (atomic, 0600, previous file kept as a backup). The real files are also snapshotted to `<repo>/.backups/login/<service>/` before the CLI starts and restored if the flow ends any other way (cancel, failure, timeout, backend shutdown) |
| `credentials` | Pastes a credentials file copied from a signed-in machine (or a secret the CLI's own login takes) | Validates the JSON shape, writes atomically with mode 0600, keeps `<file>.bak-<UTC stamp>` (newest 5) |
| `env` | Types an API key / token / mode | `PUT /api/env/{name}`: rewrites that line of `context/.env`, updates `os.environ` |
| `signout` | Confirms | Runs the CLI's logout, or moves the credentials file aside as a backup |

API-key services also get **Test**: a request to the provider's free "list models" endpoint.

## Per service

| Service | Methods (✔ = in Accounts) | Where the credential lives (as Archie runs it) | Status from |
|---|---|---|---|
| Claude Code | ✔ link `claude setup-token` → 1-year token saved as `CLAUDE_CODE_OAUTH_TOKEN` in `context/.env` (recommended); ✔ link `claude auth login --claudeai` → this server's refreshing login; ✔ link `claude auth login --console` (API billing); ✔ paste `.credentials.json`; ✔ env `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY`; ✔ sign out `claude auth logout` | `CLAUDE_CONFIG_DIR=<repo>/.claude_config` → `.credentials.json` (per machine); token in `context/.env` (synced, forwarded to SSH remotes) | `claude auth status --json` (`loggedIn`, `authMethod` = `claude.ai` / `oauth_token` / `api_key` / `none`, `email`, `orgName`, `subscriptionType`, `apiKeySource`) + `refreshTokenExpiresAt` of the file |
| Codex | ✔ link `codex login --device-auth` (URL + one-time code, recommended); ✔ link `codex login` with the redirect address pasted back; ✔ API key via `codex login --with-api-key`; ✔ paste `auth.json`; ✔ sign out `codex logout` — only Archie's own home, never the shared `~/.codex` (unavailable while borrowing it) | `CODEX_HOME` = `~/.codex-archie` (dedicated, logins from Accounts go here) or the shared `~/.codex` fallback; `ARCHIE_CODEX_HOME` overrides | `auth.json`: `auth_mode`, `tokens.id_token` claims (`email`, `https://api.openai.com/auth.chatgpt_plan_type`), `last_refresh` |
| Gemini CLI | ✔ env `GEMINI_API_KEY` (recommended; the only option for personal accounts since 2026-06-18); ✔ env `ARCHIE_GEMINI_AUTH_TYPE` (`""`/`oauth-personal`/`vertex-ai`); ✔ link Google sign-in (the TUI's `NO_BROWSER` user-code flow); ✔ paste `oauth_creds.json`; ✔ env Vertex (`GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`); ✔ sign out (moves `oauth_creds.json` aside) | `context/.env`; Google login in `~/.gemini/oauth_creds.json` (`GEMINI_HOME` overrides), account in `google_accounts.json` | The auth type Archie selects plus the credential it needs |
| Qwen Code | ✔ env: `DASHSCOPE_API_KEY` and every `envKey` named by `~/.qwen/settings.json` `modelProviders`; ✗ Qwen OAuth (discontinued 2026-04-15; `qwen auth` is "(removed)" in 0.25) | `context/.env` (or the `env` block of `~/.qwen/settings.json`, flagged as a warning) | `security.auth.selectedType` + whether each `envKey` is set |
| Model Studio | ✔ env `DASHSCOPE_API_KEY`, `MODELSTUDIO_ANTHROPIC_BASE_URL` | `context/.env` | Key set |
| OpenAI | ✔ env `OPENAI_API_KEY` (voice, talk mode, re-rank, summaries, OpenAI orchestrator models — **not** Codex) | `context/.env` | Key set; Test = `GET /v1/models` |
| Google Gemini API | ✔ env `GEMINI_API_KEY`, `GCP_PROJECT_ID`, `GCP_LOCATION` (Vertex voice uses application-default credentials) | `context/.env` | Key set; Test = `GET v1beta/models` |
| Alibaba DashScope | ✔ env `DASHSCOPE_API_KEY` (Qwen voice, Qwen Code, Model Studio) | `context/.env` | Key set; Test = `GET compatible-mode/v1/models` |
| Anthropic API | ✔ env `ANTHROPIC_API_KEY` (orchestrator's Claude models) | `context/.env` | Key set; Test = `GET /v1/models` |
| Browser extension | ✔ env `BROWSER_CONTROL_TOKEN` | `context/.env` | Key set |
| Google OAuth tokens | Read-only (the personal scripts' `context/secrets/*_tokens.json`) | `context/secrets/` | `refresh_token` present; Test refreshes each token (Google refresh tokens don't rotate) |

### What the CLIs print (verified by probing in throwaway config dirs)

- **`claude auth login`** works over plain pipes: `If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&…&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback…` then `Paste code here if prompted > `. The code (`<code>#<state>`) is read from stdin; a bad one prints `Login failed: Request failed with status code 400`. Success writes `.credentials.json` and exits 0.
- **`claude setup-token`** is an Ink TUI: it prints nothing useful without a terminal. In a PTY it shows the URL as an OSC 8 hyperlink (the driver reads the link target, so wrapping doesn't matter; the PTY is 1000 columns anyway), takes the code on the keyboard, and prints the `sk-ant-oat01-…` token, which the backend saves to `context/.env` — only once the token is followed by whitespace (output arrives in chunks), and the flow succeeds only from that save. Plain-text URLs likewise count only once terminated. A bad code: `OAuth error: … Press Enter to retry.`
- **`claude auth status`** precedence seen: `CLAUDE_CODE_OAUTH_TOKEN` reports `oauth_token` even with a credentials file or `ANTHROPIC_API_KEY` present (the latter shows as `apiKeySource`). Exit 1 when signed out, JSON either way.
- **Logins log out first**: started against a home holding a login, `codex login` (device and browser) deletes its `auth.json` at once (its log says "… during logout"), and `claude auth login` empties the tokens in `.credentials.json`; `claude setup-token` and the Gemini TUI leave existing files alone (the TUI just uses a valid existing login and asks for nothing, another reason it runs in a fresh home). Hence the staging homes above.
- **Expiry shown**: Claude's `refreshTokenExpiresAt` (the login's real lifetime). Codex logins refresh by themselves: the card shows "refreshes automatically · last refresh <date>", not the id_token's or `chatgpt_subscription_active_until`'s date (which read "expired" on a working login). Gemini's access token expiry is only mentioned as the current token's.
- **`codex login --device-auth`**: `https://auth.openai.com/codex/device` + a code like `ABCD-EFG12` ("expires in 15 minutes"); exits 0 with `Successfully logged in` once approved.
- **`codex login`** (browser) starts `http://localhost:1455` and redirects to `http://127.0.0.1:1455/auth/callback?code=…&state=…` — unreachable from the user's device when the CLI runs on the server. The user pastes that address; the backend sends the same GET to the CLI's local server, which finishes the login (a wrong `state` answers 400).
- **`codex login --with-api-key`** reads the key from stdin and writes `auth.json` (`auth_mode: apikey`); `codex login status` / `codex logout` honour `CODEX_HOME`. Under `/tmp` the CLI warns that it won't create helper binaries; harmless.
- **Gemini CLI** has no login subcommand. With `NO_BROWSER=true` and `GEMINI_CLI_AUTH_OVERRIDE=oauth-personal` (no settings file is touched) the interactive TUI prints `Please visit the following URL…` (`accounts.google.com`, redirect `codeassist.google.com/authcode`) and `Enter the authorization code:`; it needs a TTY (non-interactive mode refuses: "Manual authorization is required…"). After a good code it writes `oauth_creds.json`; the driver watches that file and stops the TUI. A bad code: `Failed to authenticate with authorization code:invalid_grant`.
- **Qwen Code 0.25**: `qwen auth` — "Configure authentication (removed)".

## The env manager

`backend/manager/accounts/envfile.py`. `context/.env` is `source`d by `run.sh` (`set -a`), so it
is treated as a **bash** file: `NAME=value`, `export NAME=value`, `'single'` / `"double"` quotes
(also multi-line), backslash escapes and comments are parsed the way bash reads them (tests compare
against `bash -c 'source …'`). Edits rewrite only the edited key's line(s) — comments, blank lines,
ordering, `export` prefixes and trailing `# comments` stay byte-identical; a duplicated key has its
last assignment updated (bash keeps the last) and all removed on delete. Values are written bare
when safe, otherwise single-quoted (`'\''` for quotes). Names must match `^[A-Z_][A-Z0-9_]*$`.
Writes are atomic, set the file to 0600, and leave a backup in `<repo>/.backups/env/` (newest 10;
directory 0700, files 0600, gitignored — deliberately not under `context/`, which is synced to the
other machine and is a git repo). An unterminated quote (`FOO=it's`) is read as a one-line value,
never as a range to the end of the file. Values containing `~` are quoted (no tilde expansion).
Names follow bash (`^[A-Za-z_][A-Za-z0-9_]*$`); the UIs suggest capitals for new keys.

The running backend's `os.environ` is updated for the edited key: code that reads the variable at
call time (voice providers, catalogs, re-rank, the harness managers when they start a session)
sees it at once; agent sessions already running keep the value they started with (Save and
Restart them). A handful of variables are only read at startup (`CLAUDE_CONFIG_DIR`, `HEADLESS`,
`PATH`, `HOME`, `PYTHONPATH`, `LD_PRELOAD`, `DISPLAY`): for those only the file changes (the
running process's environment is left alone) and the API answers `applies: "backend_restart"`. `context/.env` syncs to the other machine within seconds, but that
machine's running backend keeps its old values until restarted (its list shows `not loaded`).

## Trust model

The API has no authentication of its own; anyone who can reach it can already run commands through
an agent session. The accounts routes add no new capability, but like the rest of the API they are
closed to **other web sites**: the API-wide browser-origin guard (`backend/api/guard.py`, described
in [backend.md](../architecture/backend.md#auth-and-the-browser-origin-guard)) refuses cross-site
writes, cross-site WebSocket handshakes and untrusted `Host`s (DNS rebinding), and the CORS policy
echoes only trusted origins, so no other page can read a response. `/api/accounts/*` and
`/api/env/*` add the same origin checks on **reads** too (`require_trusted_origin`): a request
with `Sec-Fetch-Site: cross-site`, or an `Origin` that is not this server, the web dev servers or
`ARCHIE_TRUSTED_ORIGINS`, gets 403 whatever its method. Requests without `Origin` (Android, curl)
pass.

Secrets also stay off the wire by default: lists and statuses carry masked previews only (`••••` +
the last 4 characters of values of 16+ characters), a full value is returned only by `POST /api/env/{name}/reveal` (`Cache-Control:
no-store`), and raw CLI output — which can hold tokens — never leaves the server (clients get the
URL, the device code and one-line, redacted messages). Credentials files are written 0600.

## Rules

- Never copy a Claude `.credentials.json` or a Codex `auth.json` to a second machine that keeps
  using the original: both rotate refresh tokens and the two copies break each other. The
  Accounts paste panels say so; for Claude on several machines use the 1-year token.
- Setting `ANTHROPIC_API_KEY` (e.g. for the orchestrator) makes Claude Code sessions able to bill
  it instead of the subscription — the Claude card warns when it is set.
- `OPENAI_API_KEY` is stripped from Codex's environment (`codex_env()`), so it never becomes the
  Codex login.
