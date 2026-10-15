"""tests/integration/test_agents_api.py"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
async def env(env_with_provider):
    yield env_with_provider


async def test_create_and_list(env):
    c, _, auth = env
    r = await c.post(
        "/api/agents",
        headers=auth,
        json={
            "name": "bot",
            "config": {"providers": ["openai"], "default_model": "openai:gpt-4o"},
        },
    )
    assert r.status_code == 201
    agent_id = r.json()["id"]

    r = await c.get("/api/agents", headers=auth)
    assert any(a["id"] == agent_id for a in r.json())


async def test_agent_runtime_fields_are_first_class_api_fields(env, monkeypatch):
    from deepagents.middleware.filesystem import FilesystemMiddleware

    c, srv, auth = env
    registry = srv.app_runtime.agent_registry
    reload_finished = asyncio.Event()
    original_reload = registry._reload_agent

    async def track_reload(agent_id):
        await original_reload(agent_id)
        reload_finished.set()

    monkeypatch.setattr(registry, "_reload_agent", track_reload)

    def filesystems(agent_id):
        config = registry._harness_manager.get_agent(agent_id).config
        return [m for m in config.middleware or [] if isinstance(m, FilesystemMiddleware)]

    r = await c.post(
        "/api/agents",
        headers=auth,
        json={
            "name": "runtime-config",
            "max_iters": 17,
            "max_input_length": 64_000,
            "temperature": 0.4,
            "top_p": 0.9,
            "max_tokens": 2048,
            "tool_result_token_limit": 50_000,
        },
    )
    assert r.status_code == 201
    created = r.json()
    agent_id = created["agent_id"]
    assert created["max_iters"] == 17
    assert created["max_input_length"] == 64_000
    assert created["temperature"] == 0.4
    assert created["top_p"] == 0.9
    assert created["max_tokens"] == 2048
    assert created["tool_result_token_limit"] == 50_000
    assert "tool_result_token_limit" not in created["config"]
    assert "max_iters" not in created["config"]
    assert filesystems(agent_id)[0]._tool_token_limit_before_evict == 50_000

    r = await c.patch(
        f"/api/agents/{agent_id}",
        headers=auth,
        json={"max_iters": 23, "temperature": None, "tool_result_token_limit": 32_000},
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["max_iters"] == 23
    assert updated["temperature"] is None
    assert updated["tool_result_token_limit"] == 32_000
    assert "max_iters" not in updated["config"]
    assert "temperature" not in updated["config"]
    await asyncio.wait_for(reload_finished.wait(), timeout=5)
    assert filesystems(agent_id)[0]._tool_token_limit_before_evict == 32_000

    reload_finished.clear()
    r = await c.patch(
        f"/api/agents/{agent_id}", headers=auth, json={"tool_result_token_limit": None}
    )
    assert r.status_code == 200
    assert r.json()["tool_result_token_limit"] is None
    await asyncio.wait_for(reload_finished.wait(), timeout=5)
    assert filesystems(agent_id) == []
    r = await c.get(f"/api/agents/{agent_id}", headers=auth)
    assert r.json()["tool_result_token_limit"] is None


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
async def test_tool_result_limit_rejects_invalid_values(env, limit):
    c, _, auth = env
    r = await c.post(
        "/api/agents",
        headers=auth,
        json={"name": "invalid-limit", "tool_result_token_limit": limit},
    )
    assert r.status_code == 422


async def test_expert_edit_payload_preserves_tool_result_limit(env):
    c, _, auth = env
    created = await c.post(
        "/api/agents", headers=auth, json={"name": "expert-edit", "tool_result_token_limit": 50_000}
    )
    assert created.status_code == 201
    agent_id = created.json()["agent_id"]
    updated = await c.patch(
        f"/api/agents/{agent_id}",
        headers=auth,
        json={
            "name": "renamed-expert",
            "config": created.json()["config"],
            "is_shared": False,
            "welcome_message": "",
            "knowledge_base_ids": [],
            "mcp_servers": [],
            "chat_selectors": [],
            "tool_result_token_limit": 60_000,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["tool_result_token_limit"] == 60_000
    reopened = await c.get(f"/api/agents/{agent_id}", headers=auth)
    assert reopened.status_code == 200
    assert reopened.json()["tool_result_token_limit"] == 60_000


async def test_create_keeps_legacy_runtime_values_from_config(env):
    c, _, auth = env
    r = await c.post(
        "/api/agents",
        headers=auth,
        json={"name": "legacy-runtime-config", "config": {"max_iters": 19}},
    )
    assert r.status_code == 201
    created = r.json()
    assert created["max_iters"] == 19
    assert "max_iters" not in created["config"]


async def test_start_stop(env):
    """Stop unloads harness runtime; start brings it back."""
    c, _, auth = env
    r = await c.post(
        "/api/agents",
        headers=auth,
        json={
            "name": "bot",
            "config": {"providers": ["openai"], "default_model": "openai:gpt-4o"},
        },
    )
    body = r.json()
    agent_id = body["agent_id"]

    r = await c.get(f"/api/agents/{agent_id}/status", headers=auth)
    assert r.json()["state"] == "running"

    r = await c.post(f"/api/agents/{agent_id}/stop", headers=auth)
    assert r.status_code == 204

    r = await c.get(f"/api/agents/{agent_id}/status", headers=auth)
    assert r.json()["state"] == "stopped"

    r = await c.post(f"/api/agents/{agent_id}/start", headers=auth)
    assert r.status_code == 204

    r = await c.get(f"/api/agents/{agent_id}/status", headers=auth)
    assert r.json()["state"] == "running"

    rows = (await c.get("/api/agents", headers=auth)).json()
    assert any(a["agent_id"] == agent_id for a in rows)


async def test_status_endpoint(env):
    c, _, auth = env
    r = await c.post(
        "/api/agents",
        headers=auth,
        json={
            "name": "bot",
            "config": {"providers": ["openai"], "default_model": "openai:gpt-4o"},
        },
    )
    agent_id = r.json()["agent_id"]
    r = await c.get(f"/api/agents/{agent_id}/status", headers=auth)
    assert r.status_code == 200
    assert "state" in r.json()


async def test_icon_and_color_round_trip(env):
    """icon_name and color stored as first-class fields must appear in GET /agents."""
    c, _srv, auth = env
    r = await c.post(
        "/api/agents",
        headers=auth,
        json={
            "name": "icon-test",
            "config": {"icon_name": "zap", "color": "#6366f1"},
        },
    )
    assert r.status_code == 201
    agent_id = r.json()["id"]

    rows = (await c.get("/api/agents", headers=auth)).json()
    agent = next(a for a in rows if a["id"] == agent_id)
    assert agent["icon_name"] == "zap"
    assert agent["color"] == "#6366f1"


async def test_regular_user_can_list_storage_backends(env):
    """Regular authenticated users can GET /api/storage-backends."""
    c, _srv, auth = env
    r = await c.get("/api/storage-backends", headers=auth)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
