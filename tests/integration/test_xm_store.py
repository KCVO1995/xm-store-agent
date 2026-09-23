"""Company login and deterministic store selection remain user-scoped."""

from __future__ import annotations

import pytest

from octop.api.app import build_app
from octop.infra import xm_store as store_service
from octop.infra.connectors.gateway.adapters import xm_store as store_adapter
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.xm_store import CONNECTOR_KIND


@pytest.mark.asyncio
async def test_company_accounts_store_selection_and_private_threads(
    env_with_main_agent, monkeypatch
):
    client, server, _admin_auth, agent_id = env_with_main_agent
    openapi = build_app(server).openapi()
    assert "/api/auth/xm-store/login" in openapi["paths"]
    assert "/api/xm-store/stores" in openapi["paths"]
    assert "/api/xm-store/selection" in openapi["paths"]

    def fake_login(login_name: str, password: str):
        assert password == "correct-password"
        return {
            "platformUserId": f"id-{login_name}",
            "platformUserName": login_name,
            "token": f"secret-{login_name}",
        }

    def fake_stores(token: str):
        owner = token.removeprefix("secret-")
        return [{"store_id": f"store-{owner}", "store_name": owner, "store_no": "1"}]

    monkeypatch.setattr(store_service, "login_account", fake_login)
    monkeypatch.setattr(store_service, "list_authorized_stores", fake_stores)
    monkeypatch.setattr(store_adapter, "list_authorized_stores", fake_stores)

    async def sign_in(name: str):
        response = await client.post(
            "/api/auth/xm-store/login",
            json={"login_name": name, "password": "correct-password"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["user"]["role"] == "user"
        return response.json()

    alice = await sign_in("alice")
    bob = await sign_in("bob")
    alice_again = await sign_in("alice")
    assert alice["user"]["id"] == alice_again["user"]["id"]
    assert alice["user"]["id"] != bob["user"]["id"]

    alice_auth = {"Authorization": f"Bearer {alice['access_token']}"}
    bob_auth = {"Authorization": f"Bearer {bob['access_token']}"}
    inst = server.services.connector_repo.get_by_user_kind(alice["user"]["id"], CONNECTOR_KIND)
    assert inst is not None and inst.credential_blob is not None
    assert b"secret-alice" not in inst.credential_blob

    alice_stores = await client.get("/api/xm-store/stores", headers=alice_auth)
    bob_stores = await client.get("/api/xm-store/stores", headers=bob_auth)
    assert alice_stores.json()["stores"][0]["store_id"] == "store-alice"
    assert bob_stores.json()["stores"][0]["store_id"] == "store-bob"

    server.services.thread_repo.insert(
        thread_id="alice-thread",
        agent_id=agent_id,
        user_id=alice["user"]["id"],
        channel_type="dashboard",
        session_key="alice-session",
    )
    forged = await client.put(
        "/api/xm-store/selection",
        headers=alice_auth,
        json={"store_id": "store-bob", "thread_id": "alice-thread"},
    )
    assert forged.status_code == 403
    selected = await client.put(
        "/api/xm-store/selection",
        headers=alice_auth,
        json={"store_id": "store-alice", "thread_id": "alice-thread"},
    )
    assert selected.status_code == 200
    restored = await client.get("/api/xm-store/stores?thread_id=alice-thread", headers=alice_auth)
    assert restored.json()["selected_store_id"] == "store-alice"
    server.services.thread_repo.insert(
        thread_id="alice-new-thread",
        agent_id=agent_id,
        user_id=alice["user"]["id"],
        channel_type="dashboard",
        session_key="alice-new-session",
    )
    inherited = await client.get(
        "/api/xm-store/stores?thread_id=alice-new-thread", headers=alice_auth
    )
    assert inherited.json()["selected_store_id"] == "store-alice"
    cleared = await client.put(
        "/api/xm-store/selection",
        headers=alice_auth,
        json={"store_id": None, "thread_id": "alice-thread"},
    )
    assert cleared.status_code == 200
    assert (
        await client.get("/api/xm-store/stores?thread_id=alice-thread", headers=alice_auth)
    ).json()["selected_store_id"] is None
    assert (
        await client.get("/api/xm-store/stores?thread_id=alice-new-thread", headers=alice_auth)
    ).json()["selected_store_id"] == "store-alice"
    forbidden = await client.get("/api/xm-store/stores?thread_id=alice-thread", headers=bob_auth)
    assert forbidden.status_code == 403

    tool_result = store_adapter.call_tool({"token": "secret-alice"}, "list_my_stores", {})
    assert "store-alice" in tool_result
    assert "store-bob" not in tool_result

    def unavailable(_token: str):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "store service unavailable", status=502)

    monkeypatch.setattr(store_service, "list_authorized_stores", unavailable)
    assert (await client.get("/api/xm-store/stores", headers=alice_auth)).status_code == 502
    assert (await client.get("/api/auth/me", headers=alice_auth)).status_code == 200

    def expired(_token: str):
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "company token expired")

    monkeypatch.setattr(store_service, "list_authorized_stores", expired)
    assert (await client.get("/api/xm-store/stores", headers=alice_auth)).status_code == 401
