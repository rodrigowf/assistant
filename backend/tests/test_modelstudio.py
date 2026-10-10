"""Model Studio harness (Claude Code → Alibaba Model Studio): registration,
catalog, subprocess env (no Anthropic credential leak, locally or over SSH),
model mapping, thinking option, cost suppression — plus the generic
provider pinning (pool, first TurnComplete) and the SessionStore overlay
that tell ``modelstudio`` sessions apart from ``claude`` ones (same JSONL).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from manager.config import ManagerConfig
from manager.modelstudio import catalog as mc
from manager.types import SessionStatus, TextDelta, TurnComplete

KEY = "sk-dashscope-test"
OAUTH = "sk-ant-oat01-SECRET"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-SECRET")
    monkeypatch.setenv("CLAUDE_CODE_EXTRA_BODY", '{"stray": true}')
    monkeypatch.delenv("MODELSTUDIO_ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MCP_STARTUP_WAIT_MS", raising=False)


def _sm(model: str | None = "glm-5.1", opts: dict | None = None, **cfg):
    from manager.modelstudio.session import ModelStudioSessionManager

    return ModelStudioSessionManager(
        config=ManagerConfig(provider="modelstudio", model=model, harness_options=opts, **cfg),
    )


def _argv(options) -> list[str]:
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    t = SubprocessCLITransport(prompt="", options=options)
    t._cli_path = "/bin/true"
    return t._build_command()


# ── registration ──────────────────────────────────────────────────────


class TestRegistration:
    def test_spec(self):
        from manager.registry import ensure_all_registered, get_registry

        ensure_all_registered()
        spec = get_registry().require("modelstudio")
        assert spec.label == "Claude Code · Model Studio"
        assert spec.env_keys == ("DASHSCOPE_API_KEY",)
        assert spec.comm_prefix == "claude"
        assert spec.ssh_control_path_prefix == "modelstudio"
        assert spec.requirements_file == "requirements-claude.txt"
        from manager.modelstudio.session import ModelStudioSessionManager
        assert spec.session_class_loader() is ModelStudioSessionManager
        # Same storage as claude.
        claude = get_registry().require("claude")
        assert spec.jsonl_path_resolver("abc") == claude.jsonl_path_resolver("abc")

    def test_detection_still_says_claude(self, tmp_path: Path):
        from manager.protocol import detect_provider, get_registry as providers
        from manager.registry import ensure_all_registered

        ensure_all_registered()
        assert "modelstudio" not in providers().all()
        f = tmp_path / "s.jsonl"
        f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        assert detect_provider(f).provider_name == "claude"

    def test_adapter_is_claude_adapter(self, tmp_path: Path):
        from manager.claude.adapter import ClaudeAdapter
        from manager.registry import get_registry

        adapter = get_registry().require("modelstudio").adapter_loader()
        assert isinstance(adapter, ClaudeAdapter)
        assert adapter.provider_name == "modelstudio"
        assert adapter.detect_provider(tmp_path / "missing.jsonl") is False


# ── catalog ───────────────────────────────────────────────────────────


_LIVE_IDS = [
    "qwen3.8-omni-flash-realtime", "glm-5.1", "deepseek-v4-pro", "deepseek-v4-pro-0813",
    "kimi/kimi-k3", "ZHIPU/GLM-5.3", "wan2.7-image", "text-embedding-v4", "qwen3-coder-plus",
    "qwen3-tts-flash", "kimi-k3", "qwen-plus", "qwq-plus", "glm-5.3", "qvq-max",
]


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("x", request=MagicMock(), response=self)

    def json(self):
        return self._payload


class TestCatalog:
    def test_filter_keeps_anthropic_families_builtin_order_first(self):
        ids = mc.filter_model_ids(_LIVE_IDS)
        assert ids[:4] == ["glm-5.1", "glm-5.3", "deepseek-v4-pro", "qwen3-coder-plus"]
        assert "kimi-k3" in ids and "qwq-plus" in ids
        for bad in ("deepseek-v4-pro-0813", "kimi/kimi-k3", "ZHIPU/GLM-5.3", "wan2.7-image",
                    "text-embedding-v4", "qwen3-tts-flash", "qwen3.8-omni-flash-realtime",
                    "qwen-plus", "qvq-max"):
            assert bad not in ids, bad

    def test_live_catalog(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", KEY)
        seen = {}

        def fake_get(url, headers=None, timeout=None, **_):
            seen.update(url=url, headers=headers)
            return _Resp({"data": [{"id": i} for i in _LIVE_IDS]})

        with patch("httpx.get", fake_get):
            cat = mc.load_modelstudio_catalog()
        assert seen["url"].endswith("/compatible-mode/v1/models")
        assert seen["headers"]["Authorization"] == f"Bearer {KEY}"
        assert cat.provider == "modelstudio"
        assert cat.default_model == "glm-5.1"
        assert not cat.warnings
        assert all(m.source == "live" for m in cat.models)
        assert not any(m.id.startswith("claude") for m in cat.models)
        keys = [o.key for o in cat.options]
        assert keys == ["thinking", "thinking_budget", "todo_tools"]
        assert "effort" not in keys and "fallback_model" not in keys
        glm = next(m for m in cat.models if m.id == "glm-5.1")
        assert glm.context_window == 202_752 and glm.supports_thinking is True
        coder = next(m for m in cat.models if m.id == "qwen3-coder-plus")
        assert coder.supports_thinking is False
        thinking = cat.option("thinking")
        assert "qwen3-coder-plus" not in thinking.models and "glm-5.1" in thinking.models
        cat.to_dict()  # serializable

    def test_fallback_without_key(self, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        cat = mc.load_modelstudio_catalog()
        assert [m.id for m in cat.models][:2] == ["glm-5.1", "glm-5.3"]
        assert all(m.source == "builtin" for m in cat.models)
        assert cat.warnings and "DASHSCOPE_API_KEY" in cat.warnings[0]

    def test_fallback_on_http_error(self, monkeypatch):
        monkeypatch.setenv("DASHSCOPE_API_KEY", KEY)
        with patch("httpx.get", lambda *a, **k: _Resp({}, status=503)):
            cat = mc.load_modelstudio_catalog()
        assert cat.warnings and "HTTP 503" in cat.warnings[0]
        assert cat.models

    def test_option_validation(self):
        from manager.harness_catalog import validate_options

        cat = mc.build_catalog(["glm-5.1"], source="builtin")
        assert validate_options(cat, {"thinking": True, "thinking_budget": 8000}) == {
            "thinking": True, "thinking_budget": 8000,
        }
        with pytest.raises(ValueError):
            validate_options(cat, {"effort": "high"})
        with pytest.raises(ValueError):
            validate_options(cat, {"thinking_budget": 64_000})

    def test_context_windows(self):
        from manager.context_windows import context_window_for

        assert context_window_for("modelstudio", "glm-5.1") == 202_752
        assert context_window_for("modelstudio", "deepseek-v4-pro") == 1_000_000
        assert context_window_for("modelstudio", None) == 202_752  # default model


# ── env / options ─────────────────────────────────────────────────────


class TestEnv:
    def test_local_env_points_at_model_studio_without_anthropic_creds(self, env):
        options = _sm()._build_options()
        e = options.env
        assert e["ANTHROPIC_BASE_URL"] == "https://dashscope-intl.aliyuncs.com/apps/anthropic"
        assert e["ANTHROPIC_AUTH_TOKEN"] == KEY
        # Blanked, not removed: the SDK merges os.environ under options.env.
        assert e["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert e["ANTHROPIC_API_KEY"] == ""
        assert e["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert e["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "202752"
        assert e["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "1"
        assert e["CLAUDE_CODE_EXTRA_BODY"] == ""  # stray parent value blanked
        assert OAUTH not in json.dumps(e)
        # The parent process env is untouched.
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH

    def test_spawned_process_env_has_no_anthropic_creds(self, env):
        """What the CLI really gets: the SDK spawns it with
        ``{**os.environ, **options.env}`` — the inherited OAuth token must
        end up blank there, not just absent from options.env."""
        options = _sm()._build_options()
        merged = {**os.environ, **options.env}
        assert merged["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert merged["ANTHROPIC_API_KEY"] == ""
        assert OAUTH not in json.dumps(merged)
        assert merged["ANTHROPIC_AUTH_TOKEN"] == KEY

    @pytest.mark.parametrize("model", ["glm-5.1", "deepseek-v4-pro"])
    def test_every_model_slot_maps_to_the_chosen_model(self, env, model):
        options = _sm(model)._build_options()
        for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                    "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            assert options.env[key] == model, key
        assert options.model == model
        argv = _argv(options)
        assert argv[argv.index("--model") + 1] == model

    @pytest.mark.parametrize("model", [None, "", "sonnet", "opus", "claude-opus-5-5", "claude-sonnet-5-5[1m]"])
    def test_claude_or_missing_model_becomes_default(self, env, model):
        sm = _sm(model)
        assert sm._config.model == "glm-5.1"
        assert all(not v.startswith("claude") for k, v in sm._build_options().env.items()
                   if k.endswith("_MODEL"))

    def test_base_url_override(self, env, monkeypatch):
        monkeypatch.setenv("MODELSTUDIO_ANTHROPIC_BASE_URL", "https://example.test/apps/anthropic")
        assert _sm()._build_options().env["ANTHROPIC_BASE_URL"] == "https://example.test/apps/anthropic"

    def test_no_options_no_thinking_flags(self, env):
        options = _sm()._build_options()
        argv = _argv(options)
        assert "--thinking" not in argv and "--effort" not in argv
        assert "--fallback-model" not in argv and "--max-thinking-tokens" not in argv
        # Same summaries request as the claude harness.
        assert argv[argv.index("--thinking-display") + 1] == "summarized"

    def test_thinking_on(self, env):
        options = _sm("deepseek-v4-pro", {"thinking": True})._build_options()
        assert json.loads(options.env["CLAUDE_CODE_EXTRA_BODY"]) == {
            "thinking": {"type": "enabled", "budget_tokens": mc.DEFAULT_THINKING_BUDGET},
        }
        assert "--thinking" not in _argv(options)

    def test_thinking_budget(self, env):
        options = _sm("deepseek-v4-pro", {"thinking": True, "thinking_budget": 4096})._build_options()
        assert json.loads(options.env["CLAUDE_CODE_EXTRA_BODY"])["thinking"]["budget_tokens"] == 4096

    def test_thinking_off(self, env):
        options = _sm("qwen3.6-plus", {"thinking": False})._build_options()
        assert json.loads(options.env["CLAUDE_CODE_EXTRA_BODY"]) == {"thinking": {"type": "disabled"}}
        argv = _argv(options)
        assert argv[argv.index("--thinking") + 1] == "disabled"

    def test_thinking_ignored_for_models_without_thinking(self, env):
        options = _sm("qwen3-coder-plus", {"thinking": True})._build_options()
        assert options.env["CLAUDE_CODE_EXTRA_BODY"] == ""

    def test_claude_only_options_ignored(self, env):
        options = _sm("glm-5.1", {"effort": "high", "fallback_model": "claude-opus-5-5"})._build_options()
        argv = _argv(options)
        assert "--effort" not in argv and "--fallback-model" not in argv

    def test_todo_tools_off(self, env):
        options = _sm("glm-5.1", {"todo_tools": False})._build_options()
        assert "CLAUDE_CODE_ENABLE_TODO_TOOLS" not in options.env

    @pytest.mark.asyncio
    async def test_missing_key_fails_fast(self, env, monkeypatch):
        monkeypatch.delenv("DASHSCOPE_API_KEY")
        with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
            await _sm()._pre_start_check()

    def test_cli_model_picker_not_recorded(self, env):
        from manager.claude import catalog as cc

        cc._reset_for_tests()
        try:
            _sm()._record_cli_models([{"value": "default", "resolvedModel": "glm-5.1"}])
            assert cc._cli_aliases is None
        finally:
            cc._reset_for_tests()


# ── SSH ───────────────────────────────────────────────────────────────


def _wrapper_text(sm) -> str:
    from manager._ssh import clear_remote_cli_path_cache

    clear_remote_cli_path_cache()
    with patch("manager._ssh.subprocess.run", return_value=MagicMock(stdout="/home/a/.local/bin/claude\n")):
        path = sm._write_ssh_wrapper()
    try:
        return Path(path).read_text()
    finally:
        os.unlink(path)


_SSH = dict(project_dir="/remote/p", ssh_host="10.0.0.9", ssh_user="agent",
            ssh_claude_config_dir="/remote/p/.claude_config")


class TestSsh:
    def test_wrapper_forwards_model_studio_env_and_blanks_oauth(self, env):
        text = _wrapper_text(_sm("glm-5.1", **_SSH))
        assert OAUTH not in text
        assert "sk-ant-api-SECRET" not in text
        assert "CLAUDE_CODE_OAUTH_TOKEN=''" in text
        assert "ANTHROPIC_API_KEY=''" in text
        assert f"ANTHROPIC_AUTH_TOKEN='{KEY}'" in text
        assert "ANTHROPIC_BASE_URL='https://dashscope-intl.aliyuncs.com/apps/anthropic'" in text
        assert "ANTHROPIC_DEFAULT_HAIKU_MODEL='glm-5.1'" in text
        assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1'" in text
        assert "modelstudio-ssh-" in text  # own ControlMaster socket

    def test_ssh_local_env_has_no_oauth(self, env):
        with patch("manager._ssh.subprocess.run", return_value=MagicMock(stdout="/x/claude\n")):
            options = _sm("glm-5.1", **_SSH)._build_options()
        try:
            assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
            assert options.env["CLAUDE_CONFIG_DIR"] == "/remote/p/.claude_config"
        finally:
            os.unlink(options.cli_path)

    def test_claude_harness_still_forwards_oauth(self, env):
        from manager.claude.session import ClaudeSessionManager

        text = _wrapper_text(ClaudeSessionManager(config=ManagerConfig(**_SSH)))
        assert f"CLAUDE_CODE_OAUTH_TOKEN='{OAUTH}'" in text
        assert "ANTHROPIC_BASE_URL" not in text
        assert "claude-ssh-" in text


# ── cost ──────────────────────────────────────────────────────────────


class TestCost:
    @pytest.mark.asyncio
    async def test_turn_cost_suppressed(self, env):
        from claude_agent_sdk import ResultMessage

        sm = _sm()
        msg = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-1", total_cost_usd=0.0965, usage={"input_tokens": 3},
            result="hi",
        )
        events = [e async for e in sm._process_message(msg)]
        (tc,) = [e for e in events if isinstance(e, TurnComplete)]
        assert tc.cost is None
        assert tc.usage == {"input_tokens": 3} and tc.result == "hi"
        assert sm.cost == 0.0
        assert sm.sdk_session_id == "s-1"

    @pytest.mark.asyncio
    async def test_claude_cost_unchanged(self, env):
        from claude_agent_sdk import ResultMessage
        from manager.claude.session import ClaudeSessionManager

        sm = ClaudeSessionManager(config=ManagerConfig())
        msg = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-1", total_cost_usd=0.5,
        )
        (tc,) = [e async for e in sm._process_message(msg)]
        assert tc.cost == 0.5 and sm.cost == 0.5


# ── provider pinning (pool, first TurnComplete) ───────────────────────


def _stub_sm(provider: str, sdk_id: str):
    sm = MagicMock()
    sm.local_id = "tab-1"
    sm.sdk_session_id = sdk_id
    sm.provider_name = provider
    sm.status = SessionStatus.IDLE
    sm.subprocess_pid = None
    sm.is_active = True
    sm.last_yielded_seq = None
    sm.stream_id = None
    sm.pending_permission_ids = MagicMock(return_value=[])
    sm._receive_loop_done = asyncio.Event()

    async def _send(text):
        yield TextDelta(text="ok")
        yield TurnComplete(cost=None, num_turns=1, session_id=sdk_id)

    sm.send = _send
    return sm


def _install(pool, sm):
    sid = sm.local_id
    pool._sessions[sid] = sm
    pool._subscribers[sid] = set()
    pool._locks[sid] = asyncio.Lock()
    pool._pending_prompts[sid] = deque()
    pool._pending_locks[sid] = asyncio.Lock()


@pytest.fixture
def ctx(tmp_path: Path):
    with patch("api.routes.session_config.get_context_dir", return_value=tmp_path):
        yield tmp_path


class TestProviderPinning:
    @pytest.mark.asyncio
    async def test_first_turn_pins_provider(self, ctx):
        from api.pool import SessionPool
        from api.routes.session_config import load_session_config

        sdk_id = str(uuid.uuid4())
        pool = SessionPool()
        _install(pool, _stub_sm("modelstudio", sdk_id))
        async for _ in pool.send("tab-1", "hi"):
            pass
        assert load_session_config(sdk_id)["provider"] == "modelstudio"

    @pytest.mark.asyncio
    async def test_existing_pin_is_never_overwritten(self, ctx):
        from api.pool import SessionPool
        from api.routes.session_config import load_session_config, save_session_config

        sdk_id = str(uuid.uuid4())
        save_session_config(sdk_id, {"provider": "claude", "harness_model": "x"})
        pool = SessionPool()
        _install(pool, _stub_sm("modelstudio", sdk_id))
        async for _ in pool.send("tab-1", "hi"):
            pass
        cfg = load_session_config(sdk_id)
        assert cfg["provider"] == "claude" and cfg["harness_model"] == "x"

    @pytest.mark.asyncio
    async def test_keeps_other_keys_and_pins_claude_too(self, ctx):
        from api.pool import SessionPool
        from api.routes.session_config import load_session_config, save_session_config

        sdk_id = str(uuid.uuid4())
        save_session_config(sdk_id, {"enabled_mcps": ["a"]})
        pool = SessionPool()
        _install(pool, _stub_sm("claude", sdk_id))
        async for _ in pool.send("tab-1", "hi"):
            pass
        cfg = load_session_config(sdk_id)
        assert cfg["provider"] == "claude" and cfg["enabled_mcps"] == ["a"]

    @pytest.mark.asyncio
    async def test_skips_test_doubles_and_unknown_providers(self, ctx):
        from api.pool import SessionPool

        pool = SessionPool()
        _install(pool, _stub_sm("modelstudio", "sdk-not-a-uuid"))
        async for _ in pool.send("tab-1", "hi"):
            pass
        pool2 = SessionPool()
        _install(pool2, _stub_sm("no-such-harness", str(uuid.uuid4())))
        async for _ in pool2.send("tab-1", "hi"):
            pass
        assert list(ctx.glob("*.config.json")) == []

    @pytest.mark.asyncio
    async def test_orchestrator_runner_path_pins(self, ctx):
        """The orchestrator drives turns through BackgroundAgentRunner →
        pool.send(); the pin happens there too."""
        from api.pool import SessionPool
        from api.routes.session_config import load_session_config
        from orchestrator.runner import NotificationQueue
        from orchestrator.runner import BackgroundAgentRunner

        sdk_id = str(uuid.uuid4())
        pool = SessionPool()
        _install(pool, _stub_sm("modelstudio", sdk_id))
        runner = BackgroundAgentRunner(pool, MagicMock(), NotificationQueue(), idle_timeout=5.0)
        await runner.spawn("tab-1", "do it")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if (ctx / f"{sdk_id}.config.json").exists():
                break
        await runner.cancel_all()
        assert load_session_config(sdk_id)["provider"] == "modelstudio"

    def test_resume_uses_pinned_provider_not_detection(self, ctx):
        """A pinned modelstudio session resumes as modelstudio even though
        its JSONL is detected as claude."""
        from api.routes.chat import _resolve_session_provider

        sdk_id = str(uuid.uuid4())
        with patch("utils.paths.get_context_dir", return_value=ctx):
            (ctx / f"{sdk_id}.jsonl").write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
            )
            provider, _model, persist = _resolve_session_provider(
                resume_sdk_id=sdk_id,
                session_cfg={"provider": "modelstudio"},
                assistant_cfg={"provider": "claude"},
            )
        assert provider == "modelstudio" and persist is None


# ── SessionStore overlay ──────────────────────────────────────────────


def _claude_jsonl(path: Path, sid: str) -> None:
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hello"},
         "timestamp": "2026-10-07T10:00:00Z", "uuid": "u1", "sessionId": sid},
        {"type": "assistant", "message": {"role": "assistant", "model": "glm-5.1",
                                          "content": [{"type": "text", "text": "hi"}]},
         "timestamp": "2026-10-07T10:00:01Z", "uuid": "a1", "sessionId": sid},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


class TestStoreOverlay:
    def _store(self, tmp_path: Path):
        from manager.store import SessionStore

        (tmp_path / "context" / "chats").mkdir(parents=True)
        return SessionStore(tmp_path), tmp_path / "context"

    def test_listing_reports_pinned_modelstudio(self, tmp_path: Path):
        store, ctxdir = self._store(tmp_path)
        a, b, c = (str(uuid.uuid4()) for _ in range(3))
        for sid in (a, b, c):
            _claude_jsonl(ctxdir / f"{sid}.jsonl", sid)
        (ctxdir / f"{a}.config.json").write_text(json.dumps({"provider": "modelstudio"}))
        (ctxdir / f"{c}.config.json").write_text(json.dumps({"provider": "qwen"}))  # not a sibling
        got = {s.session_id: s.provider for s in store.list_sessions()}
        assert got == {a: "modelstudio", b: "claude", c: "claude"}
        assert store.get_session_info(a).provider == "modelstudio"
        assert store.get_session(a).provider == "modelstudio"
        msgs, _, _ = store.get_messages_paginated(a)
        assert msgs and all(m.provider == "modelstudio" for m in msgs)

    def test_pin_written_after_listing_is_picked_up(self, tmp_path: Path):
        store, ctxdir = self._store(tmp_path)
        sid = str(uuid.uuid4())
        _claude_jsonl(ctxdir / f"{sid}.jsonl", sid)
        assert store.list_sessions()[0].provider == "claude"
        cfg = ctxdir / f"{sid}.config.json"
        cfg.write_text(json.dumps({"provider": "modelstudio"}))
        os.utime(cfg, ns=(1, 1))  # force a distinct mtime
        assert store.list_sessions()[0].provider == "modelstudio"
        cfg.unlink()
        assert store.list_sessions()[0].provider == "claude"
