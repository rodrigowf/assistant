"""Settings → Accounts: the .env manager, the CLI-output scanners, the login-flow driver, the
services' file handling and the /api/accounts + /api/env routes.

Everything runs against temp files: the real context/.env and credential files are never read
or written (``envfile.env_path`` and every service's paths are monkeypatched).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.deps import get_accounts
from manager.accounts import AccountError, AccountService, AccountsManager, EnvField, Method, envfile
from manager.accounts import claude as claude_acc
from manager.accounts import codex as codex_acc
from manager.accounts import gemini as gemini_acc
from manager.accounts import qwen as qwen_acc
from manager.accounts.files import atomic_write, jwt_claims, move_aside
from manager.accounts.flows import FlowManager, FlowSpec, LoginFlow, Scan, clean, find_url, redact

FIX = Path(__file__).parent / "fixtures" / "accounts"
FAKE = str(FIX / "fake_login.py")


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture(autouse=True)
def _restore_environ():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def env_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda: path)
    monkeypatch.setattr(envfile, "backups_dir", lambda: tmp_path / ".env.backups")
    return path


SAMPLE = """# Archie secrets
OPENAI_API_KEY=sk-openai-0123456789abcdef


export VOICE_DEBUG_VAD=1
QUOTED="has space and \\"quotes\\" and $dollar"
SINGLE='it'\\''s single'
MULTI="line one
line two"
TRAILING=value # a comment
DUP=first
DUP=second
"""


def bash_value(path: Path, name: str) -> str:
    """What `set -a; source <file>` gives *name* — the ground truth run.sh sees."""
    out = subprocess.run(
        ["bash", "-c", f'set -a; source "$0" >/dev/null 2>&1; printf %s "${{{name}}}"', str(path)],
        capture_output=True, text=True, check=True, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    return out.stdout


# ─────────────────────────────── env file ───────────────────────────────


class TestEnvFile:
    def test_parse_matches_bash(self, env_file):
        env_file.write_text(SAMPLE)
        for name in ("OPENAI_API_KEY", "VOICE_DEBUG_VAD", "SINGLE", "MULTI", "TRAILING", "DUP"):
            assert envfile.get_value(name) == bash_value(env_file, name), name
        # "$dollar" is expanded by bash (to nothing); the manager shows the literal text instead.
        assert envfile.get_value("QUOTED") == 'has space and "quotes" and $dollar'

    def test_list_is_masked_and_ordered(self, env_file):
        env_file.write_text(SAMPLE)
        keys = {k.name: k for k in envfile.list_keys()}
        assert list(keys) == ["OPENAI_API_KEY", "VOICE_DEBUG_VAD", "QUOTED", "SINGLE", "MULTI", "TRAILING", "DUP"]
        assert keys["OPENAI_API_KEY"].preview == "••••cdef"
        assert keys["VOICE_DEBUG_VAD"].preview == "••••"
        assert keys["VOICE_DEBUG_VAD"].exported is True
        assert keys["DUP"].duplicates == 1 and keys["DUP"].line == 12
        dumped = json.dumps([k.to_dict() for k in keys.values()])
        assert "sk-openai" not in dumped and "line one" not in dumped

    def test_update_touches_only_that_line(self, env_file):
        env_file.write_text(SAMPLE)
        envfile.set_value("TRAILING", "new value with 'quote'")
        text = env_file.read_text()
        before, after = SAMPLE.splitlines(), text.splitlines()
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        assert changed == [9] and len(before) == len(after)
        assert after[9].endswith(" # a comment")
        assert bash_value(env_file, "TRAILING") == "new value with 'quote'"
        assert os.environ["TRAILING"] == "new value with 'quote'"

    def test_update_multiline_and_export(self, env_file):
        env_file.write_text(SAMPLE)
        envfile.set_value("MULTI", "single line now")
        envfile.set_value("VOICE_DEBUG_VAD", "0")
        assert "export VOICE_DEBUG_VAD=0\n" in env_file.read_text()
        assert bash_value(env_file, "MULTI") == "single line now"
        assert bash_value(env_file, "TRAILING") == "value"

    def test_duplicates_update_last_delete_all(self, env_file):
        env_file.write_text(SAMPLE)
        envfile.set_value("DUP", "third")
        assert bash_value(env_file, "DUP") == "third"
        assert "DUP=first" in env_file.read_text()
        assert envfile.delete("DUP") == 2
        assert "DUP" not in env_file.read_text()
        assert "DUP" not in os.environ

    @pytest.mark.parametrize("value", ["", "plain", "a b", "it's", 'say "hi"', "$HOME", "back\\slash", "multi\nline", "#hash", "ünïcode ✓", "~/x", "a:~/b", "x=~"])
    def test_create_round_trips_through_bash(self, env_file, value):
        env_file.write_text("A=1")  # no trailing newline
        envfile.set_value("NEW_KEY", value, create=True)
        assert env_file.read_text().startswith("A=1\n")
        assert bash_value(env_file, "NEW_KEY") == value
        assert envfile.get_value("NEW_KEY") == value

    def test_create_conflict_and_missing(self, env_file):
        env_file.write_text("A=1\n")
        with pytest.raises(envfile.EnvError) as e:
            envfile.set_value("A", "2", create=True)
        assert e.value.status == 409
        with pytest.raises(envfile.EnvError) as e:
            envfile.set_value("B", "2", create=False)
        assert e.value.status == 404
        with pytest.raises(envfile.EnvError) as e:
            envfile.delete("B")
        assert e.value.status == 404

    @pytest.mark.parametrize("name", ["1ABC", "A-B", "A B", "", "A=B", "é"])
    def test_invalid_names(self, env_file, name):
        with pytest.raises(envfile.EnvError):
            envfile.set_value(name, "x")

    def test_backup_mode_and_new_file(self, env_file, tmp_path):
        envfile.set_value("FIRST", "1")  # creates the file
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        os.chmod(env_file, 0o664)
        for i in range(envfile.ENV_BACKUPS + 3):
            envfile.set_value("FIRST", str(i))
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600  # forced: it holds every secret
        folder = tmp_path / ".env.backups"
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        backups = list(folder.glob(".env.bak-*"))
        assert len(backups) == envfile.ENV_BACKUPS
        assert all(stat.S_IMODE(b.stat().st_mode) == 0o600 for b in backups)

    def test_backups_live_outside_context(self):
        from utils.paths import PROJECT_ROOT, get_context_dir

        assert envfile.backups_dir() == PROJECT_ROOT / ".backups" / "env"
        assert not envfile.backups_dir().is_relative_to(get_context_dir())

    def test_unterminated_quote_stays_one_line(self, env_file):
        env_file.write_text("A=1\nFOO=it's\nB=2\nC=3\n")
        assert envfile.get_value("FOO") == "it's"
        assert [k.name for k in envfile.list_keys()] == ["A", "FOO", "B", "C"]
        envfile.set_value("FOO", "fixed")
        assert env_file.read_text() == "A=1\nFOO=fixed\nB=2\nC=3\n"

    def test_lowercase_names_are_usable(self, env_file):
        env_file.write_text("lower_key=1\n")
        assert envfile.get_value("lower_key") == "1"
        envfile.set_value("lower_key", "2")
        assert envfile.delete("lower_key") == 1

    def test_restart_scoped_keys_leave_the_process_alone(self, env_file, monkeypatch):
        monkeypatch.setenv("HOME", "/home/original")
        envfile.set_value("HOME", "/somewhere/else")
        assert os.environ["HOME"] == "/home/original"
        assert envfile.get_value("HOME") == "/somewhere/else"

    def test_restart_scope(self):
        assert envfile.restart_scope("OPENAI_API_KEY") == "now"
        assert envfile.restart_scope("CLAUDE_CONFIG_DIR") == "backend_restart"

    def test_mask(self):
        assert envfile.mask("") == ""
        assert envfile.mask("short") == "••••"
        assert envfile.mask("x" * 15 + "WXYZ") == "••••WXYZ"


class TestFiles:
    def test_atomic_write_backup_and_symlink(self, tmp_path):
        real = tmp_path / "real.json"
        link = tmp_path / "link.json"
        real.write_text("old")
        link.symlink_to(real)
        backup = atomic_write(link, "new")
        assert link.is_symlink() and real.read_text() == "new"
        assert backup is not None and backup.read_text() == "old"
        assert stat.S_IMODE(real.stat().st_mode) == 0o600

    def test_move_aside(self, tmp_path):
        p = tmp_path / "creds.json"
        assert move_aside(p) is None
        p.write_text("{}")
        moved = move_aside(p)
        assert not p.exists() and moved and moved.exists()

    def test_jwt_claims(self):
        payload = base64.urlsafe_b64encode(json.dumps({"email": "a@b.c"}).encode()).decode().rstrip("=")
        assert jwt_claims(f"h.{payload}.s") == {"email": "a@b.c"}
        assert jwt_claims("nope") == {} and jwt_claims(None) == {}


# ─────────────────────────────── scanners ───────────────────────────────


class TestScanners:
    def test_claude_auth_login(self):
        s = claude_acc.scan_login(fixture("claude_auth_login.txt"))
        assert s.url and s.url.startswith("https://claude.com/cai/oauth/authorize?") and s.url.endswith("state=STATE")
        assert s.error == "Login failed: Request failed with status code 400"

    def test_claude_setup_token_url_from_hyperlink(self):
        s = claude_acc.scan_setup_token(fixture("claude_setup_token.txt"))
        assert s.url and "scope=user%3Ainference" in s.url and s.url.endswith("state=STATE")
        assert s.error is None and s.secret is None

    def test_claude_setup_token_success_and_error(self):
        ok = claude_acc.scan_setup_token(fixture("claude_setup_token_success.txt"))
        assert ok.secret and ok.secret.startswith("sk-ant-oat01-FAKE") and ok.secret.endswith("AA")
        bad = claude_acc.scan_setup_token(fixture("claude_setup_token_error.txt"))
        assert bad.secret is None and bad.error and bad.error.startswith("OAuth error: Request failed")

    def test_codex_device(self):
        s = codex_acc.scan_device(fixture("codex_device.txt"))
        assert s.url == "https://auth.openai.com/codex/device"
        assert s.user_code == "ABCD-EFG12"
        assert s.error is None and not s.success
        assert not codex_acc.scan_device(fixture("codex_device.txt") + "Successfully logged in\n").success  # exit 0 decides

    def test_codex_browser(self):
        s = codex_acc.scan_browser(fixture("codex_browser.txt"))
        assert s.url and s.url.startswith("https://auth.openai.com/oauth/authorize?") and "originator=codex_cli_rs" in s.url
        assert s.error is None

    def test_codex_callback_query(self):
        q = codex_acc.callback_query("http://127.0.0.1:1455/auth/callback?code=AB&state=CD&scope=x")
        assert q == "code=AB&state=CD&scope=x"
        assert codex_acc.callback_query("?code=1&state=2") == "code=1&state=2"
        with pytest.raises(AccountError):
            codex_acc.callback_query("http://127.0.0.1:1455/auth/callback?state=only")
        with pytest.raises(AccountError):
            codex_acc.callback_query("http://127.0.0.1:1455/auth/callback?error=access_denied")

    def test_gemini_google(self):
        s = gemini_acc.scan_google(fixture("gemini_google.txt"))
        assert s.url and s.url.startswith("https://accounts.google.com/o/oauth2/v2/auth?") and s.url.endswith(".apps.googleusercontent.com")
        err = gemini_acc.scan_google(fixture("gemini_google_error.txt"))
        assert err.error and "invalid_grant" in err.error

    def test_terminal_helpers(self):
        assert clean("a\x1b[9Gb\r\nc\x1b[0m") == "a b\nc"
        assert find_url("see https://x.test/a?b=1.\n", contains="/a") == "https://x.test/a?b=1"
        assert redact("token sk-ant-oat01-" + "z" * 50) == "token sk-ant-oa…"
        assert "…" in redact("x" * 60)


# ─────────────────────────────── flow driver ───────────────────────────────


def fake_spec(mode: str, **kw) -> FlowSpec:
    kw.setdefault("scan", claude_acc.scan_login)
    return FlowSpec(service=kw.pop("service", "fake"), method=kw.pop("method", mode),
                    argv=[sys.executable, FAKE, mode], env=dict(os.environ), **kw)


def _generic_scan(raw: str) -> Scan:
    text = clean(raw)
    err = re.search(r"(Login failed[^\n]*|OAuth error[^\n]*|stdin is not a terminal)", text)
    return Scan(url=find_url(raw, "https://example.test/"),
                user_code="WXYZ-12345" if "WXYZ-12345" in text else None,
                error=err.group(1) if err else None,
                success="Login successful" in text or "Successfully logged in" in text)


class TestFlows:
    @pytest.mark.parametrize("use_pty", [False, True])
    async def test_code_good(self, use_pty, tmp_path):
        marker = tmp_path / "marker"
        spec = fake_spec("code", scan=_generic_scan, pty=use_pty, needs_code=True)
        spec.env["FAKE_LOGIN_MARKER"] = str(marker)
        flow = LoginFlow(spec)
        await flow.start()
        await flow.wait_ready(5)
        assert flow.status == "waiting" and flow.url == "https://example.test/oauth/authorize?state=abc&x=1"
        assert flow.to_dict()["needs_code"] is True
        await flow.submit_code("good")
        await flow.wait_done(5)
        assert flow.status == "succeeded" and marker.read_text() == "ok"
        assert flow.to_dict()["needs_code"] is False
        await flow.wait_closed()

    @pytest.mark.parametrize("use_pty", [False, True])
    async def test_code_bad(self, use_pty):
        flow = LoginFlow(fake_spec("code", scan=_generic_scan, pty=use_pty, needs_code=True))
        await flow.start()
        await flow.wait_ready(5)
        await flow.submit_code("nope")
        await flow.wait_done(5)
        assert flow.status == "failed" and flow.message.startswith("Login failed: bad code")
        with pytest.raises(AccountError):
            await flow.submit_code("again")
        await flow.wait_closed()

    async def test_tty_only_cli_needs_pty(self):
        piped = LoginFlow(fake_spec("tty", scan=_generic_scan))
        await piped.start()
        await piped.wait_done(5)
        assert piped.status == "failed" and "not a terminal" in piped.message
        await piped.wait_closed()
        tty = LoginFlow(fake_spec("tty", scan=_generic_scan, pty=True, needs_code=True))
        await tty.start()
        await tty.wait_ready(5)
        assert tty.status == "waiting"
        await tty.cancel()
        assert tty.status == "cancelled" and tty._proc.returncode is not None

    async def test_device_finishes_by_itself(self):
        flow = LoginFlow(fake_spec("device", scan=_generic_scan))
        await flow.start()
        await flow.wait_done(5)
        assert flow.status == "succeeded" and flow.user_code == "WXYZ-12345"
        await flow.wait_closed()

    async def test_printed_secret_is_stored_and_process_killed(self):
        stored: list[str] = []
        flow = LoginFlow(fake_spec("token", scan=claude_acc.scan_setup_token, pty=True, needs_code=True,
                                   on_secret=stored.append, success_message="saved"))
        flow.spec.scan = lambda raw: Scan(url=find_url(raw, "https://example.test/"), **{
            k: v for k, v in vars(claude_acc.scan_setup_token(raw)).items() if k in ("secret", "error")})
        await flow.start()
        await flow.wait_ready(5)
        await flow.submit_code("good")
        await flow.wait_done(5)
        assert flow.status == "succeeded" and flow.message == "saved"
        assert stored == ["sk-ant-oat01-" + "T" * 40]
        await flow.wait_closed()
        assert flow._proc.returncode is not None
        assert "sk-ant" not in json.dumps(flow.to_dict())

    async def test_timeout_expires_and_kills(self):
        flow = LoginFlow(fake_spec("hang", scan=_generic_scan, timeout_s=1.5))
        await flow.start()
        await flow.wait_done(6)
        assert flow.status == "expired"
        await flow.wait_closed()
        assert flow._proc.returncode is not None

    async def test_watch_success(self, tmp_path):
        landed = tmp_path / "creds"
        flow = LoginFlow(fake_spec("hang", scan=_generic_scan, needs_code=True, watch=landed.exists, exit_ok_is_success=False))
        await flow.start()
        await flow.wait_ready(5)
        await flow.submit_code("anything")
        landed.write_text("{}")
        await flow.wait_done(5)
        assert flow.status == "succeeded"
        await flow.wait_closed()

    async def test_missing_binary(self):
        flow = LoginFlow(FlowSpec(service="x", method="y", argv=["/nonexistent/cli"], env={}, scan=_generic_scan))
        await flow.start()
        assert flow.status == "failed" and "Couldn't run" in flow.message

    async def test_manager_one_flow_per_service(self):
        mgr = FlowManager()
        a = await mgr.start(fake_spec("hang", scan=_generic_scan, service="s", method="m1"))
        assert a.status == "waiting"
        assert await mgr.start(fake_spec("hang", scan=_generic_scan, service="s", method="m1")) is a
        with pytest.raises(AccountError) as e:
            await mgr.start(fake_spec("hang", scan=_generic_scan, service="s", method="m2"))
        assert e.value.status == 409
        await mgr.cancel("s")
        b = await mgr.start(fake_spec("hang", scan=_generic_scan, service="s", method="m2"))
        assert b is not a
        await mgr.shutdown()
        assert b.status == "cancelled"


# ─────────────────────────────── services ───────────────────────────────


class TestServices:
    async def test_claude_credentials_paste(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        svc = claude_acc.ClaudeAccount()
        with pytest.raises(AccountError):
            await svc.save_credentials("credentials", "{not json")
        with pytest.raises(AccountError):
            await svc.save_credentials("credentials", '{"claudeAiOauth": {}}')
        (tmp_path / ".credentials.json").write_text("old")
        msg = await svc.save_credentials("credentials", '{"claudeAiOauth": {"accessToken": "x"}}')
        assert "backup" in msg
        assert json.loads((tmp_path / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "x"
        assert list(tmp_path.glob(".credentials.json.bak-*"))

    async def test_claude_status_from_cli_json(self, tmp_path, monkeypatch, env_file):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "refreshTokenExpiresAt": 1893456000000, "subscriptionType": "max"}}))
        svc = claude_acc.ClaudeAccount()

        async def cli_status():
            return {"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.test", "subscriptionType": "max"}

        monkeypatch.setattr(svc, "_cli_status", cli_status)
        st = await svc.status()
        assert (st.state, st.plan, st.account, st.expires_at) == ("signed_in", "Claude Max", "me@example.test", "2030-01-01T00:00:00Z")
        assert next(m for m in st.methods if m.id == "signout").available

        async def token_status():
            return {"loggedIn": True, "authMethod": "oauth_token", "apiKeySource": "ANTHROPIC_API_KEY"}

        monkeypatch.setattr(svc, "_cli_status", token_status)
        st = await svc.status()
        assert "CLAUDE_CODE_OAUTH_TOKEN" in st.method and any("ANTHROPIC_API_KEY" in w for w in st.warnings)

    def test_claude_store_token_goes_to_env(self, env_file):
        claude_acc.store_token("sk-ant-oat01-abc")
        assert envfile.get_value("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-abc"
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-abc"

    async def test_codex_status_and_paste(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ARCHIE_CODEX_HOME", str(tmp_path))
        svc = codex_acc.CodexAccount()
        assert (await svc.status()).state == "signed_out"
        claims = {"email": "me@example.test", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
        idt = "h." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".s"
        with pytest.raises(AccountError):
            await svc.save_credentials("auth_json", '{"auth_mode": "chatgpt"}')
        await svc.save_credentials("auth_json", json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": idt, "refresh_token": "r"}, "last_refresh": "2026-10-08T00:00:00Z"}))
        assert stat.S_IMODE((tmp_path / "auth.json").stat().st_mode) == 0o600
        st = await svc.status()
        assert (st.state, st.account, st.plan) == ("signed_in", "me@example.test", "ChatGPT Plus")
        with pytest.raises(AccountError):
            await svc.save_credentials("apikey", "two words")

    async def test_gemini_status_by_auth_type(self, tmp_path, monkeypatch, env_file):
        monkeypatch.setenv("GEMINI_HOME", str(tmp_path))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ARCHIE_GEMINI_AUTH_TYPE", raising=False)
        svc = gemini_acc.GeminiAccount()
        assert (await svc.status()).state == "signed_out"
        envfile.set_value("GEMINI_API_KEY", "AIzaFAKE")
        assert (await svc.status()).state == "signed_in"
        monkeypatch.setenv("ARCHIE_GEMINI_AUTH_TYPE", "oauth-personal")
        st = await svc.status()
        assert st.state == "signed_out" and st.warnings
        await svc.save_credentials("oauth_creds", json.dumps({"refresh_token": "r", "expiry_date": 1893456000000}))
        (tmp_path / "google_accounts.json").write_text(json.dumps({"active": "me@example.test", "old": []}))
        st = await svc.status()
        assert (st.state, st.account) == ("signed_in", "me@example.test")
        assert await svc.sign_out()
        assert not (tmp_path / "oauth_creds.json").exists()

    async def test_qwen_keys_from_settings(self, tmp_path, monkeypatch, env_file):
        monkeypatch.setenv("QWEN_HOME", str(tmp_path))
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        (tmp_path / "settings.json").write_text(json.dumps({
            "security": {"auth": {"selectedType": "openai"}},
            "modelProviders": {"openai": [{"id": "a", "envKey": "DASHSCOPE_API_KEY"}, {"id": "b", "envKey": "OPENROUTER_API_KEY"}, {"id": "c", "envKey": "bad name"}]},
        }))
        st = await qwen_acc.QwenAccount().status()
        assert st.state == "signed_out"
        assert [f.name for f in st.methods[0].fields] == ["DASHSCOPE_API_KEY", "OPENROUTER_API_KEY"]
        assert st.methods[1].available is False
        envfile.set_value("OPENROUTER_API_KEY", "sk-or")
        assert (await qwen_acc.QwenAccount().status()).state == "signed_in"

    async def test_registry_degrades_and_fills_fields(self, env_file):
        class Broken(AccountService):
            id, label = "broken", "Broken"

            async def status(self):
                raise RuntimeError("boom")

        class Keyed(AccountService):
            id, label = "keyed", "Keyed"

            async def status(self):
                return self.new_status(state="signed_out", methods=[Method(id="k", kind="env", label="Key", fields=[
                    EnvField(name="SECRET_KEY", label="s"), EnvField(name="MODE_KEY", label="m", secret=False)])])

        envfile.set_value("SECRET_KEY", "supersecretvalue1234")
        envfile.set_value("MODE_KEY", "vertex-ai")
        mgr = AccountsManager([Broken(), Keyed()])
        broken, keyed = await mgr.status_all()
        assert broken["state"] == "unknown" and "boom" in broken["warnings"][0]
        secret, mode = keyed["methods"][0]["fields"]
        assert secret["set"] and secret["preview"] == "••••1234" and secret["value"] is None
        assert mode["value"] == "vertex-ai"
        assert "supersecretvalue" not in json.dumps(keyed)


# ─────────────────────────────── routes ───────────────────────────────


class FakeService(AccountService):
    id, label, group = "fake", "Fake", "harness"

    def __init__(self) -> None:
        self.saved: list[str] = []

    async def status(self):
        return self.new_status(state="signed_in" if self.saved else "signed_out")

    def flow_spec(self, method):
        if method != "code":
            return super().flow_spec(method)
        return fake_spec("code", scan=_generic_scan, service="fake", needs_code=True)

    async def save_credentials(self, method, content):
        if content != "ok":
            raise AccountError("bad")
        self.saved.append(content)
        return "Saved."

    async def sign_out(self):
        self.saved.clear()
        return "Signed out."

    async def verify(self):
        return {"ok": True, "message": "works", "checked_at": "now"}


@pytest.fixture
async def client(env_file):
    app = create_app()
    mgr = AccountsManager([FakeService()])
    app.dependency_overrides[get_accounts] = lambda: mgr
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    await mgr.flows.shutdown()


class TestRoutes:
    async def test_env_crud_never_lists_values(self, client, env_file):
        env_file.write_text("# keep me\nAPI_KEY=supersecretvalue1234\n")
        r = await client.get("/api/env")
        assert r.status_code == 200 and "supersecret" not in r.text
        assert r.json()["keys"][0]["preview"] == "••••1234"
        r = await client.post("/api/env/API_KEY/reveal")
        assert r.json() == {"name": "API_KEY", "value": "supersecretvalue1234"}
        assert r.headers["cache-control"] == "no-store"
        assert (await client.post("/api/env/NOPE/reveal")).status_code == 404
        r = await client.put("/api/env/API_KEY", json={"value": "changed value"})
        assert r.status_code == 200 and r.json()["applies"] == "now" and "changed" not in r.text
        assert (await client.post("/api/env", json={"name": "API_KEY", "value": "x"})).status_code == 409
        assert (await client.post("/api/env", json={"name": "bad-name", "value": "x"})).status_code == 400
        assert (await client.post("/api/env", json={"name": "NEW_ONE", "value": "x"})).status_code == 200
        r = await client.delete("/api/env/API_KEY")
        assert r.json()["removed"] == 1
        assert (await client.delete("/api/env/API_KEY")).status_code == 404
        assert env_file.read_text() == "# keep me\nNEW_ONE=x\n"

    async def test_accounts_list_credentials_signout_verify(self, client):
        r = await client.get("/api/accounts")
        assert [s["id"] for s in r.json()["services"]] == ["fake"]
        assert (await client.get("/api/accounts/nope")).status_code == 404
        assert (await client.post("/api/accounts/fake/credentials", json={"method": "x", "content": "bad"})).status_code == 400
        r = await client.post("/api/accounts/fake/credentials", json={"method": "x", "content": "ok"})
        assert r.json()["service"]["state"] == "signed_in"
        r = await client.post("/api/accounts/fake/verify")
        assert r.json()["verified"]["ok"] is True
        r = await client.post("/api/accounts/fake/logout")
        assert r.json()["service"]["state"] == "signed_out" and r.json()["service"]["verified"] is None

    async def test_login_flow_over_http(self, client):
        assert (await client.get("/api/accounts/fake/login")).status_code == 404
        assert (await client.post("/api/accounts/fake/login", json={"method": "nope"})).status_code == 404
        r = await client.post("/api/accounts/fake/login", json={"method": "code"})
        flow = r.json()
        assert flow["status"] == "waiting" and flow["url"].startswith("https://example.test/") and flow["needs_code"]
        assert (await client.get("/api/accounts/fake")).json()["flow"]["id"] == flow["id"]
        r = await client.post("/api/accounts/fake/login/code", json={"code": "good"})
        assert r.json()["status"] == "succeeded"
        assert (await client.get("/api/accounts/fake/login")).json()["status"] == "succeeded"
        r = await client.post("/api/accounts/fake/login", json={"method": "code"})
        assert r.json()["id"] != flow["id"]
        r = await client.delete("/api/accounts/fake/login")
        assert r.json()["status"] == "cancelled"


# ─────────────────────────────── security review fixes ───────────────────────────────


class TestGuard:
    @pytest.mark.parametrize("host,ok", [
        ("192.168.0.200", True), ("127.0.0.1:8765", True), ("[::1]:8765", True), ("localhost:5450", True),
        ("server.local", True), ("jetson", True), ("archie.tail1234.ts.net", True),
        ("evil.example.com", False), ("192.168.0.200.nip.io", False),
    ])
    async def test_host_allowlist(self, client, host, ok):
        r = await client.get("/api/env", headers={"host": host})
        assert (r.status_code == 200) is ok, r.text

    @pytest.mark.parametrize("origin,host,ok", [
        ("https://192.168.0.200", "192.168.0.200", True),          # nginx: Host $host, no port
        ("http://192.168.0.200:8765", "192.168.0.200:8765", True),
        ("http://192.168.0.28:5450", "192.168.0.200", True),       # vite dev server proxying
        ("http://localhost:5451", "127.0.0.1:8765", True),
        ("https://evil.example.com", "192.168.0.200", False),
        ("http://192.168.0.99", "192.168.0.200", False),           # another LAN host's page
        ("http://evil.example.com:5450", "192.168.0.200", False),  # dev port, untrusted host
        ("null", "192.168.0.200", False),
    ])
    async def test_origin(self, client, origin, host, ok):
        r = await client.post("/api/env/X/reveal", headers={"origin": origin, "host": host})
        assert (r.status_code != 403) is ok, r.text

    async def test_cross_site_fetch_metadata_and_trusted_origin_env(self, client, monkeypatch):
        r = await client.get("/api/accounts", headers={"sec-fetch-site": "cross-site"})
        assert r.status_code == 403
        monkeypatch.setenv("ARCHIE_TRUSTED_ORIGINS", "https://my.dashboard.example")
        r = await client.get("/api/accounts", headers={"origin": "https://my.dashboard.example"})
        assert r.status_code == 200

    async def test_no_origin_passes_and_auth_writes_are_guarded(self, client):
        assert (await client.get("/api/accounts")).status_code == 200
        r = await client.post("/api/auth/credentials", json={"credentials_json": "{}"}, headers={"origin": "https://evil.example.com"})
        assert r.status_code == 403
        r = await client.post("/api/auth/login", headers={"origin": "https://evil.example.com"})
        assert r.status_code == 403


class TestReviewFixes:
    def test_token_needs_a_terminator(self):
        tok = "sk-ant-oat01-" + "A" * 40
        assert claude_acc.scan_setup_token(f"Your token: {tok}").secret is None  # may still be arriving
        assert claude_acc.scan_setup_token(f"Your token: {tok}\r\n").secret == tok

    def test_plain_url_needs_a_terminator(self):
        assert find_url("visit https://x.test/a?b=1") is None
        assert find_url("visit https://x.test/a?b=1\n") == "https://x.test/a?b=1"
        assert find_url("\x1b]8;;https://x.test/link\x07text\x1b]8;;\x07") == "https://x.test/link"  # hyperlink: complete

    def test_token_flow_exit_without_token_fails(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(claude_acc, "claude_cli", lambda: "/bin/true")
        assert claude_acc.ClaudeAccount().flow_spec("token").exit_ok_is_success is False

    async def test_start_failure_never_leaves_a_stuck_flow(self, monkeypatch):
        import pty as pty_mod

        def boom():
            raise OSError(24, "out of pty devices")

        monkeypatch.setattr(pty_mod, "openpty", boom)
        mgr = FlowManager()
        flow = await mgr.start(fake_spec("hang", scan=_generic_scan, service="s", method="m", pty=True))
        assert flow.status == "failed" and "out of pty devices" in flow.message
        monkeypatch.undo()
        again = await mgr.start(fake_spec("hang", scan=_generic_scan, service="s", method="other"))
        assert again.status == "waiting"  # no 409 from the failed one
        await mgr.shutdown()

    async def test_on_close_runs_once(self):
        calls = []
        flow = LoginFlow(fake_spec("device", scan=_generic_scan, on_close=lambda: calls.append(1)))
        await flow.start()
        await flow.wait_done(5)
        await flow.wait_closed()
        await flow.cancel()
        assert calls == [1]

    async def test_codex_signout_never_touches_the_shared_home(self, tmp_path, monkeypatch):
        dedicated = tmp_path / "dedicated"
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"refresh_token":"r"}}')
        monkeypatch.delenv("ARCHIE_CODEX_HOME", raising=False)
        monkeypatch.setenv("CODEX_HOME", str(shared))
        monkeypatch.setattr(codex_acc.codex_home_mod, "dedicated_home", lambda: dedicated)
        monkeypatch.setattr(codex_acc, "codex_cli", lambda: "/bin/true")
        svc = codex_acc.CodexAccount()
        st = await svc.status()
        signout = next(m for m in st.methods if m.id == "signout")
        assert st.state == "signed_in" and not signout.available and "borrowing" in signout.unavailable_reason
        with pytest.raises(AccountError) as e:
            await svc.sign_out()
        assert e.value.status == 409
        assert (shared / "auth.json").exists()

    def test_redact_in_cli_errors(self):
        assert "sk-ant-oa…" in redact("failed: token sk-ant-oat01-" + "x" * 40)


# ─────────────────────────── logins never destroy the current login ───────────────────────────


@pytest.fixture
def login_backups(tmp_path, monkeypatch):
    import manager.accounts.flows as flows_mod

    monkeypatch.setattr(flows_mod, "login_backups_dir", lambda service: tmp_path / "backups" / service)
    return tmp_path / "backups"


class TestLoginSnapshots:
    def _spec(self, mode, cred, **kw):
        spec = fake_spec(mode, scan=_generic_scan, service="svc", protect=(cred,), **kw)
        spec.env["FAKE_LOGIN_CLOBBER"] = str(cred)
        return spec

    @pytest.mark.parametrize("ending", ["cancel", "fail", "timeout", "shutdown"])
    async def test_restored_when_the_login_does_not_succeed(self, tmp_path, login_backups, ending):
        cred = tmp_path / "auth.json"
        cred.write_text('{"old": true}')
        os.chmod(cred, 0o600)
        if ending == "timeout":
            spec = self._spec("hang", cred, timeout_s=1.5)
        else:
            spec = self._spec("code", cred, needs_code=True, pty=ending == "fail")
        mgr = FlowManager()
        flow = await mgr.start(spec)
        assert not cred.exists()  # the CLI logged the old login out at start
        if ending == "cancel":
            await mgr.cancel("svc")
        elif ending == "fail":
            await flow.submit_code("bad")
            await flow.wait_done(5)
        elif ending == "timeout":
            await flow.wait_done(6)
        else:
            await mgr.shutdown()
        await flow.wait_closed()
        assert flow.status != "succeeded"
        assert cred.read_text() == '{"old": true}'
        assert stat.S_IMODE(cred.stat().st_mode) == 0o600
        assert "previous login was restored" in flow.message
        assert list((login_backups / "svc").glob("auth.json.bak-*"))  # snapshot kept, 0600
        await mgr.shutdown()

    async def test_kept_after_success(self, tmp_path, login_backups):
        cred = tmp_path / "auth.json"
        cred.write_text('{"old": true}')
        spec = self._spec("code", cred, needs_code=True)
        spec.env["FAKE_LOGIN_WRITE"] = str(cred)
        flow = LoginFlow(spec)
        await flow.start()
        await flow.wait_ready(5)
        await flow.submit_code("good")
        await flow.wait_done(5)
        await flow.wait_closed()
        assert flow.status == "succeeded"
        assert cred.read_text() == '{"new": true}'
        assert list((login_backups / "svc").glob("auth.json.bak-*"))

    async def test_promote_failure_is_a_failure_and_restores(self, tmp_path, login_backups):
        cred = tmp_path / "auth.json"
        cred.write_text('{"old": true}')

        def promote():
            raise AccountError("nothing staged")

        spec = self._spec("code", cred, needs_code=True, promote=promote)
        flow = LoginFlow(spec)
        await flow.start()
        await flow.wait_ready(5)
        await flow.submit_code("good")
        await flow.wait_done(5)
        await flow.wait_closed()
        assert flow.status == "failed" and "saving the login failed" in flow.message
        assert cred.read_text() == '{"old": true}'

    async def test_absent_before_and_created_by_a_failed_login_is_moved_aside(self, tmp_path, login_backups):
        cred = tmp_path / "auth.json"
        spec = fake_spec("hang", scan=_generic_scan, service="svc", protect=(cred,))
        flow = LoginFlow(spec)
        await flow.start()
        await flow.wait_ready(5)
        cred.write_text("{}")
        await flow.cancel()
        assert not cred.exists() and list(tmp_path.glob("auth.json.bak-*"))


class TestStagedLogins:
    def test_codex_runs_against_a_staging_home_and_promotes(self, tmp_path, monkeypatch):
        home = tmp_path / "codex-archie"
        home.mkdir()
        (home / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"refresh_token":"old"}}')
        monkeypatch.setenv("ARCHIE_CODEX_HOME", str(home))
        monkeypatch.setattr(codex_acc, "codex_cli", lambda: "/bin/true")
        spec = codex_acc.CodexAccount().flow_spec("device")
        staging = Path(spec.env["CODEX_HOME"])
        assert staging != home and staging.is_dir()
        assert spec.protect == (home / "auth.json",)
        with pytest.raises(AccountError):
            spec.promote()  # nothing staged yet
        (staging / "auth.json").write_text('{"auth_mode":"chatgpt","tokens":{"refresh_token":"new"}}')
        spec.promote()
        assert json.loads((home / "auth.json").read_text())["tokens"]["refresh_token"] == "new"
        assert list(home.glob("auth.json.bak-*"))
        spec.on_close()
        assert not staging.exists()

    def test_claude_auth_login_is_staged_and_merges_only_login_keys(self, tmp_path, monkeypatch):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"old"}}')
        (cfg / ".claude.json").write_text('{"projects":{"x":1},"oauthAccount":{"emailAddress":"old@x"}}')
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        monkeypatch.setattr(claude_acc, "claude_cli", lambda: "/bin/true")
        spec = claude_acc.ClaudeAccount().flow_spec("login")
        staging = Path(spec.env["CLAUDE_CONFIG_DIR"])
        assert staging != cfg and spec.protect == (cfg / ".credentials.json",)
        (staging / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"new"}}')
        (staging / ".claude.json").write_text('{"oauthAccount":{"emailAddress":"new@x"},"machineID":"m"}')
        spec.promote()
        assert json.loads((cfg / ".credentials.json").read_text())["claudeAiOauth"]["accessToken"] == "new"
        state = json.loads((cfg / ".claude.json").read_text())
        assert state == {"projects": {"x": 1}, "oauthAccount": {"emailAddress": "new@x"}}
        spec.on_close()

    def test_gemini_login_is_staged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_HOME", str(tmp_path / "real"))
        monkeypatch.setattr(gemini_acc, "gemini_cli", lambda: "/bin/true")
        spec = gemini_acc.GeminiAccount().flow_spec("google")
        staging = Path(spec.env["GEMINI_CLI_HOME"])
        assert spec.env["HOME"] == str(staging)
        assert not spec.watch()
        (staging / ".gemini").mkdir()
        (staging / ".gemini" / "oauth_creds.json").write_text('{"refresh_token":"r"}')
        assert spec.watch()
        spec.promote()
        assert json.loads((tmp_path / "real" / "oauth_creds.json").read_text()) == {"refresh_token": "r"}
        spec.on_close()
        assert not staging.exists()

    async def test_codex_status_has_no_expiry_for_a_refreshing_login(self, tmp_path, monkeypatch):
        claims = {"email": "me@x", "https://api.openai.com/auth": {"chatgpt_plan_type": "free", "chatgpt_subscription_active_until": "2025-09-10T00:00:00Z"}}
        idt = "h." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".s"
        (tmp_path / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": idt, "refresh_token": "r"}, "last_refresh": "2026-10-08T01:02:03Z"}))
        monkeypatch.setenv("ARCHIE_CODEX_HOME", str(tmp_path))
        st = await codex_acc.CodexAccount().status()
        assert st.expires_at is None
        assert "refreshes automatically" in st.detail and "last refresh 2026-10-08" in st.detail
        device = next(m for m in st.methods if m.id == "device")
        assert "replaces" in device.warning
