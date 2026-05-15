"""Tests for the HarnessRegistry — the single dispatch point for everything
provider-axis (session-manager class, JSONL adapter, kill helper, SSH
ControlPath prefix, install hints, …).

These tests assert the *contract*, not the specific harnesses that happen
to be registered today.  When a fourth harness lands the existing tests
should still pass without modification — that's the whole point of the
refactor.
"""

from __future__ import annotations

import pytest

from manager.protocol import ProviderAdapter
from manager.registry import (
    HarnessRegistry,
    HarnessSpec,
    ensure_all_registered,
    get_registry,
    registered_provider_names,
)


def test_ensure_all_registered_populates_known_harnesses():
    """After ensure_all_registered, both shipped harnesses are present."""
    ensure_all_registered()
    names = set(registered_provider_names())
    # At minimum the two shipped harnesses.  Don't hardcode equality —
    # a future fourth harness shouldn't break this test.
    assert {"claude", "qwen"}.issubset(names)


def test_spec_fields_are_complete_for_every_harness():
    """Every registered spec exposes the full contract surface."""
    ensure_all_registered()
    for name, spec in get_registry().all().items():
        assert spec.name == name, f"spec.name {spec.name!r} != key {name!r}"
        assert spec.label, f"{name}: label empty"
        assert spec.description, f"{name}: description empty"
        assert spec.comm_prefix, f"{name}: comm_prefix empty"
        assert spec.ssh_control_path_prefix, f"{name}: ssh prefix empty"
        # Loaders are callables (lazy-resolved).
        assert callable(spec.session_class_loader)
        assert callable(spec.adapter_loader)
        assert callable(spec.kill_helper_loader)
        assert callable(spec.jsonl_path_resolver)


def test_adapter_loader_returns_provideradapter_instance():
    """Each spec's adapter loader hands back a ProviderAdapter whose
    provider_name matches the spec's name."""
    ensure_all_registered()
    for name, spec in get_registry().all().items():
        adapter = spec.adapter_loader()
        assert isinstance(adapter, ProviderAdapter), f"{name}: not a ProviderAdapter"
        assert adapter.provider_name == name, (
            f"{name}: adapter.provider_name {adapter.provider_name!r} mismatch"
        )


def test_jsonl_path_resolver_returns_nonempty_list():
    """Every harness must propose at least one candidate path for a
    given session id, so the chat-resume sniffer has something to probe."""
    ensure_all_registered()
    for name, spec in get_registry().all().items():
        paths = spec.jsonl_path_resolver("some-session-id")
        assert isinstance(paths, list)
        assert len(paths) >= 1, f"{name}: resolver returned no candidates"
        for p in paths:
            assert "some-session-id" in str(p), (
                f"{name}: resolver dropped the session id from {p!r}"
            )


def test_ssh_control_path_prefixes_are_unique():
    """Two harnesses sharing the same SSH ControlPath would share a
    multiplex socket lifetime, so the prefixes must be distinct."""
    ensure_all_registered()
    prefixes = [s.ssh_control_path_prefix for s in get_registry().all().values()]
    assert len(prefixes) == len(set(prefixes)), (
        f"duplicate ssh_control_path_prefix: {prefixes}"
    )


def test_require_raises_for_unknown_name():
    reg = HarnessRegistry()
    with pytest.raises(ValueError, match="Unknown session harness"):
        reg.require("nonexistent")


def test_get_returns_none_for_unknown_name():
    reg = HarnessRegistry()
    assert reg.get("nope") is None


def test_register_is_idempotent_last_wins():
    """A test fixture (or a future hot-reload) replacing a spec should
    work without having to clear the registry first."""
    reg = HarnessRegistry()
    spec_a = HarnessSpec(
        name="x",
        label="X",
        description="first",
        session_class_loader=lambda: None,  # type: ignore[return-value]
        adapter_loader=lambda: None,  # type: ignore[return-value]
        comm_prefix="x",
        kill_helper_loader=lambda: (lambda pid: False),
        ssh_control_path_prefix="x",
        jsonl_path_resolver=lambda sid: [],
    )
    spec_b = HarnessSpec(
        name="x",
        label="X2",
        description="second",
        session_class_loader=lambda: None,  # type: ignore[return-value]
        adapter_loader=lambda: None,  # type: ignore[return-value]
        comm_prefix="x",
        kill_helper_loader=lambda: (lambda pid: False),
        ssh_control_path_prefix="x",
        jsonl_path_resolver=lambda sid: [],
    )
    reg.register(spec_a)
    reg.register(spec_b)
    assert reg.require("x").description == "second"


def test_kill_tracked_pid_dispatches_via_registry(monkeypatch):
    """The orphan reaper's ``_kill_tracked_pid`` used to dispatch on hardcoded
    ``looks_like(pid, 'claude')`` / ``'qwen'`` / ``'node'``.  Now it walks the
    registry and dispatches on each spec's comm_prefix — verify that the
    iteration is registry-driven by injecting a fake spec and watching its
    kill helper get called."""
    from api import pool
    import manager.registry as registry
    from manager.registry import HarnessRegistry, HarnessSpec

    calls: list[str] = []

    fake_spec = HarnessSpec(
        name="fake",
        label="Fake",
        description="-",
        session_class_loader=lambda: None,  # type: ignore[return-value]
        adapter_loader=lambda: None,  # type: ignore[return-value]
        comm_prefix="fakeprefix",
        kill_helper_loader=lambda: (
            lambda pid: calls.append(f"fake:{pid}") or True
        ),
        ssh_control_path_prefix="fake",
        jsonl_path_resolver=lambda sid: [],
    )
    fake_registry = HarnessRegistry()
    fake_registry.register(fake_spec)

    monkeypatch.setattr(registry, "_registry", fake_registry)
    monkeypatch.setattr(registry, "get_registry", lambda: fake_registry)
    monkeypatch.setattr(registry, "ensure_all_registered", lambda: None)
    # Make every pid look like the fake spec's process.
    monkeypatch.setattr(
        pool, "looks_like", lambda pid, prefix: prefix == "fakeprefix",
    )

    assert pool._kill_tracked_pid(9999) is True
    assert calls == ["fake:9999"]


def test_kill_tracked_pid_returns_false_for_unknown_comm(monkeypatch):
    """Unknown comm prefixes are left alone — we never signal a PID that
    doesn't look like one of ours."""
    from api import pool
    monkeypatch.setattr(pool, "looks_like", lambda pid, prefix: False)
    assert pool._kill_tracked_pid(9999) is False


def test_names_preserves_registration_order():
    reg = HarnessRegistry()
    for nm in ("beta", "alpha", "gamma"):
        reg.register(HarnessSpec(
            name=nm,
            label=nm.upper(),
            description="-",
            session_class_loader=lambda: None,  # type: ignore[return-value]
            adapter_loader=lambda: None,  # type: ignore[return-value]
            comm_prefix=nm,
            kill_helper_loader=lambda: (lambda pid: False),
            ssh_control_path_prefix=nm,
            jsonl_path_resolver=lambda sid: [],
        ))
    assert reg.names() == ("beta", "alpha", "gamma")
