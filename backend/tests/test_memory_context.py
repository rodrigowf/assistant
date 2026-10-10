"""Archie's memory index for every harness (manager/memory_context.py) and its
wiring into the Qwen, Codex and Claude / Model Studio session managers."""

from __future__ import annotations

from pathlib import Path

import pytest

from manager import memory_context as mcx
from manager.config import ManagerConfig
from utils.paths import PROJECT_ROOT

INDEX = "# Memory Index\n\nRoot index.\n\n## Ontology\n\n| a | b |\n\n## Rules\n\n## Key files\n"


@pytest.fixture
def memdir(tmp_path, monkeypatch):
    """A fake context/memory with a MEMORY.md, used by every caller."""
    d = tmp_path / "memory"
    d.mkdir()
    (d / "MEMORY.md").write_text(INDEX)
    monkeypatch.setattr(mcx, "get_memory_dir", lambda: d)
    return d


class TestIsArchieProject:
    def test_repo_root_and_inside(self):
        assert mcx.is_archie_project(PROJECT_ROOT)
        assert mcx.is_archie_project(str(PROJECT_ROOT) + "/")
        assert mcx.is_archie_project(PROJECT_ROOT / "backend")

    def test_other_dirs(self, tmp_path):
        assert not mcx.is_archie_project(tmp_path)
        assert not mcx.is_archie_project(None)
        assert not mcx.is_archie_project("")
        assert not mcx.is_archie_project(str(PROJECT_ROOT) + "-other")

    def test_remote_path_that_does_not_exist_here(self, monkeypatch, tmp_path):
        # SSH sessions name the remote dir; both machines use the same path.
        fake_root = tmp_path / "nowhere" / "assistant"
        monkeypatch.setattr(mcx, "PROJECT_ROOT", fake_root)
        assert mcx.is_archie_project(str(fake_root))
        assert not mcx.is_archie_project("/somewhere/else")


class TestReadIndex:
    def test_reads_live_file(self, memdir):
        assert mcx.read_memory_index() == INDEX.strip()
        (memdir / "MEMORY.md").write_text("# Memory Index\n\n## Changed\n")
        assert "## Changed" in mcx.read_memory_index()

    def test_missing_or_empty(self, tmp_path):
        assert mcx.read_memory_index(tmp_path) is None
        (tmp_path / "MEMORY.md").write_text("  \n")
        assert mcx.read_memory_index(tmp_path) is None

    def test_line_cap_like_claude(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("\n".join(f"line {i}" for i in range(300)))
        text = mcx.read_memory_index(tmp_path)
        assert "line 199" in text and "line 200" not in text
        assert "truncated" in text

    def test_byte_cap(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("x" * (mcx.MAX_BYTES * 2))
        text = mcx.read_memory_index(tmp_path)
        assert len(text.encode()) < mcx.MAX_BYTES + 200


class TestInstructions:
    def test_block_for_the_repo(self, memdir):
        block = mcx.memory_instructions(PROJECT_ROOT)
        assert block.startswith(mcx.HEADER)
        assert f"{PROJECT_ROOT}/context/memory/" in block
        assert "AGENTS.md" in block and "INDEX.md" in block
        assert "never write fact text into MEMORY.md" in block
        assert f"<memory_index>\n{INDEX.strip()}\n</memory_index>" in block

    def test_none_elsewhere_or_without_index(self, memdir, tmp_path):
        assert mcx.memory_instructions(tmp_path) is None
        (memdir / "MEMORY.md").unlink()
        assert mcx.memory_instructions(PROJECT_ROOT) is None


# ── Qwen: --append-system-prompt, rebuilt every spawn ──────────────────


class TestQwen:
    def test_argv_appends_memory_in_the_repo(self, memdir):
        from manager.qwen.session import QwenSessionManager

        argv = QwenSessionManager(config=ManagerConfig(project_dir=str(PROJECT_ROOT), provider="qwen"))._build_argv()
        i = argv.index("--append-system-prompt")
        assert argv[i + 1].startswith(mcx.HEADER) and "## Ontology" in argv[i + 1]

    def test_argv_reads_the_file_per_turn(self, memdir):
        from manager.qwen.session import QwenSessionManager

        sm = QwenSessionManager(config=ManagerConfig(project_dir=str(PROJECT_ROOT), provider="qwen"))
        sm._build_argv()
        (memdir / "MEMORY.md").write_text("# Memory Index\n\n## Fresh\n")
        argv = sm._build_argv()
        assert "## Fresh" in argv[argv.index("--append-system-prompt") + 1]

    def test_no_memory_outside_the_repo(self, memdir, tmp_path):
        from manager.qwen.session import QwenSessionManager

        argv = QwenSessionManager(config=ManagerConfig(project_dir=str(tmp_path), provider="qwen"))._build_argv()
        assert "--append-system-prompt" not in argv

    def test_ssh_argv_carries_the_block(self, memdir):
        from unittest.mock import patch

        from manager.qwen.session import QwenSessionManager

        sm = QwenSessionManager(config=ManagerConfig(
            project_dir=str(PROJECT_ROOT), provider="qwen", ssh_host="10.0.0.9", ssh_user="agent",
        ))
        with patch("manager.qwen.session.resolve_remote_cli_path", return_value="/r/qwen"):
            argv, _ = sm._maybe_wrap_with_ssh(sm._build_argv())
        assert "--append-system-prompt" in argv[-1] and "## Ontology" in argv[-1]


# ── Claude / Model Studio: auto-memory off, the same block appended ────


class TestClaude:
    def _sm(self, project_dir, cls="claude"):
        if cls == "claude":
            from manager.claude.session import ClaudeSessionManager as C
        else:
            from manager.modelstudio.session import ModelStudioSessionManager as C
        return C(config=ManagerConfig(project_dir=str(project_dir), provider=cls))

    @pytest.mark.parametrize("cls", ["claude", "modelstudio"])
    def test_repo_session(self, memdir, monkeypatch, cls):
        from manager.claude.session import _PERMISSION_GATING_PROMPT

        monkeypatch.setenv("DASHSCOPE_API_KEY", "k")
        opts = self._sm(PROJECT_ROOT, cls)._build_options()
        append = opts.system_prompt["append"]
        assert append.startswith(_PERMISSION_GATING_PROMPT)
        assert append.count(mcx.HEADER + "\n") == 1 and "## Ontology" in append
        assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    def test_other_project_keeps_cli_defaults(self, memdir, tmp_path, monkeypatch):
        from manager.claude.session import _PERMISSION_GATING_PROMPT

        monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
        opts = self._sm(tmp_path)._build_options()
        assert opts.system_prompt["append"] == _PERMISSION_GATING_PROMPT
        assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in opts.env

    def test_ssh_wrapper_forwards_the_switch(self, memdir):
        import os
        from unittest.mock import MagicMock, patch

        from manager._ssh import clear_remote_cli_path_cache
        from manager.claude.session import ClaudeSessionManager

        clear_remote_cli_path_cache()
        sm = ClaudeSessionManager(config=ManagerConfig(
            project_dir=str(PROJECT_ROOT), ssh_host="10.0.0.9", ssh_user="agent",
        ))
        with patch("manager._ssh.subprocess.run", return_value=MagicMock(stdout="/r/claude\n")):
            path = sm._write_ssh_wrapper()
        try:
            assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY='1'" in Path(path).read_text()
        finally:
            os.unlink(path)
