"""Tests for the harness-model surface of ``api/routes/config.py``.

Covers:
- ``GET /api/config/harness/qwen/models`` returns the parsed catalog
- ``PUT /api/config`` shallow-merges ``harness_model`` per-provider
- Validation rejects unknown providers and non-string model ids
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient with PROJECT_ROOT pointed at a temp dir so the
    test doesn't write to the real assistant_config.json."""
    # Point QWEN_HOME at the temp dir too so harness/qwen/models reads
    # our fixture rather than the dev machine's real settings.
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "qwen"))
    # The config module reads PROJECT_ROOT lazily inside _get_config_path,
    # so patching it via monkeypatch (rather than env) does the job.
    import utils.paths
    monkeypatch.setattr(utils.paths, "PROJECT_ROOT", tmp_path)
    # Also patch the symbol that was already imported into api.routes.config
    import api.routes.config as cfg_module
    monkeypatch.setattr(cfg_module, "PROJECT_ROOT", tmp_path)
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# GET /api/config/harness/qwen/models


def test_qwen_models_endpoint_with_no_settings(client: TestClient) -> None:
    """Fresh install, no ~/.qwen/settings.json yet — endpoint returns
    {"models": []} rather than 404 / 500.  The frontend uses this to know
    when to show the "run qwen once" hint."""
    r = client.get("/api/config/harness/qwen/models")
    assert r.status_code == 200
    assert r.json() == {"models": []}


def test_qwen_models_endpoint_returns_parsed_catalog(
    client: TestClient, tmp_path: Path
) -> None:
    """When settings.json exists, the route returns one row per model
    with the full set of badges propagated."""
    qwen_home = tmp_path / "qwen"
    qwen_home.mkdir()
    (qwen_home / "settings.json").write_text(json.dumps({
        "modelProviders": {
            "openai": [
                {
                    "id": "qwen3.6-plus",
                    "name": "Qwen 3.6 Plus",
                    "baseUrl": "https://dashscope.example.com/v1",
                    "generationConfig": {
                        "extra_body": {"enable_thinking": True},
                        "contextWindowSize": 1_000_000,
                    },
                },
            ],
        },
    }))

    r = client.get("/api/config/harness/qwen/models")
    assert r.status_code == 200
    payload = r.json()
    assert len(payload["models"]) == 1
    [model] = payload["models"]
    assert model["id"] == "qwen3.6-plus"
    assert model["display_name"] == "Qwen 3.6 Plus"
    assert model["context_window"] == 1_000_000
    assert model["supports_thinking"] is True


# ---------------------------------------------------------------------------
# PUT /api/config with harness_model


def test_harness_model_shallow_merge_preserves_other_provider(
    client: TestClient,
) -> None:
    """Patching ``harness_model.qwen`` should NOT clobber the existing
    ``harness_model.claude`` value, and vice versa."""
    # Seed both providers' picks via two separate PUTs so we know each
    # write is isolated.
    r = client.put("/api/config", json={
        "harness_model": {"claude": "claude-sonnet-4-5", "qwen": "qwen3.6-plus"},
    })
    assert r.status_code == 200, r.text

    # Now change just qwen — claude must stick.
    r = client.put("/api/config", json={"harness_model": {"qwen": "deepseek-v4-pro"}})
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["harness_model"]["claude"] == "claude-sonnet-4-5"
    assert cfg["harness_model"]["qwen"] == "deepseek-v4-pro"


def test_harness_model_empty_string_means_cli_default(client: TestClient) -> None:
    """An empty string is the explicit "let the CLI pick" signal — accepted,
    not coerced to absent."""
    r = client.put("/api/config", json={"harness_model": {"qwen": ""}})
    assert r.status_code == 200, r.text
    assert r.json()["harness_model"]["qwen"] == ""


def test_harness_model_rejects_unknown_provider(client: TestClient) -> None:
    """Typo guard: ``harness_model.qwn`` shouldn't silently land in the file."""
    r = client.put("/api/config", json={"harness_model": {"qwn": "qwen3.6-plus"}})
    assert r.status_code == 400
    assert "Unknown harness provider" in r.json()["detail"]


def test_harness_model_rejects_non_string_value(client: TestClient) -> None:
    """Pydantic catches most type errors before we see the request, but the
    validator additionally rejects None/numbers in the inner dict for
    forward-compat."""
    # Pydantic 2 will refuse a non-str value in dict[str, str] up front,
    # so this should come back as a 422 (validation) rather than our 400.
    r = client.put("/api/config", json={"harness_model": {"qwen": 123}})
    assert r.status_code in (400, 422)
