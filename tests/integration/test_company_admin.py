"""Company accounts retain assigned platform roles and their own connector scope."""

from __future__ import annotations

import json

import httpx
import pytest

from octop.infra import boh, qixuebao_courses, xm_store, yuxue
from octop.infra.connectors.service import ConnectorService
from octop.infra.xm_access import company_credentials


@pytest.mark.parametrize("login_method", ["password", "sms"])
async def test_company_admin_role_persistence_connectors_and_revocation(
    env_with_provider, monkeypatch, login_method
):
    client, server, admin_auth = env_with_provider
    sessions: dict[str, int] = {}

    def profile(name):
        sessions[name] = sessions.get(name, 0) + 1
        return {
            "platformUserId": name,
            "platformUserName": name,
            "qwId": f"qw-{name}",
            "token": f"company-{name}-{sessions[name]}",
        }

    monkeypatch.setattr(xm_store, "login_account", lambda name, _password: profile(name))
    monkeypatch.setattr(xm_store, "login_by_code", lambda _phone, _code: profile("alice"))

    async def sign_in(name="alice", *, method="password"):
        if method == "password":
            response = await client.post(
                "/api/auth/xm-store/login", json={"login_name": name, "password": "secret"}
            )
        else:
            response = await client.post(
                "/api/auth/xm-store/login-by-code",
                json={"country_code": "86", "phone": "13812345678", "code": "123456"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        return body["user"], {"Authorization": f"Bearer {body['access_token']}"}

    alice, alice_auth = await sign_in()
    bob, _bob_auth = await sign_in("bob")
    assert alice["role"] == bob["role"] == "user"
    user_id = alice["id"]
    listed = await client.get("/api/users", headers=admin_auth)
    company_user = next(row for row in listed.json() if row["id"] == user_id)
    assert company_user["sso_linked"] is True and company_user["has_password"] is False

    promoted = await client.patch(
        f"/api/users/{user_id}", headers=admin_auth, json={"role": "admin"}
    )
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
    assert (await client.get("/api/users", headers=alice_auth)).status_code == 200
    assert (await client.post("/api/auth/logout", headers=alice_auth)).status_code == 204
    await server.user_manager.shutdown_all()
    await server.user_manager.boot()
    promoted_user, promoted_auth = await sign_in(method=login_method)
    assert promoted_user["id"] == user_id and promoted_user["role"] == "admin"
    assert (await client.get("/api/auth/me", headers=promoted_auth)).json()["role"] == "admin"
    assert (await client.get("/api/users", headers=promoted_auth)).status_code == 200
    assert server.services.user_repo.get(bob["id"]).role == "user"

    instances = (await client.get("/api/connector-instances", headers=promoted_auth)).json()
    assert {row["kind"] for row in instances} == {"xm-store", "boh", "yuxue"}
    assert all(
        row["owner_user_id"] == user_id
        and row["status"] == "active"
        and row["has_credentials"]
        and not row["shared"]
        for row in instances
    )
    names = [row["mcp_server_name"] for row in instances]
    created = await client.post(
        "/api/agents",
        headers=promoted_auth,
        json={
            "name": "公司管理员专家",
            "config": {"providers": ["openai"], "default_model": "openai:gpt-4o-mini"},
            "chat_selectors": ["xm_store", "qixuebao_course"],
            "mcp_servers": names,
        },
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["user_id"] == user_id and set(agent["mcp_servers"]) == set(names)
    agent_id = agent["agent_id"]
    thread = await client.post(f"/api/agents/{agent_id}/threads", headers=promoted_auth)
    assert thread.status_code == 201, thread.text
    thread_id = thread.json()["thread_id"]
    token = f"company-alice-{sessions['alice']}"
    assert company_credentials(server.services.repos, user_id)[1]["token"] == token

    def stores(current_token):
        assert current_token == token
        return [
            {
                "store_id": "alice-store",
                "store_name": "门店",
                "store_no": "001",
                "store_oa_id": "oa-alice",
            }
        ]

    def courses(current_token, *, keyword="", page_num=1):
        assert current_token == token
        return {
            "courses": [{"course_id": "alice-course", "course_name": "课程"}],
            "page_num": 1,
            "page_size": 1000,
            "pages": 1,
            "total": 1,
        }

    monkeypatch.setattr(xm_store, "list_authorized_stores", stores)
    monkeypatch.setattr(xm_store, "list_authorized_stores_with_oa_id", stores)
    monkeypatch.setattr(qixuebao_courses, "query_authorized_courses", courses)
    store_list = await client.get("/api/xm-store/stores", headers=promoted_auth)
    assert (
        store_list.status_code == 200
        and store_list.json()["stores"][0]["store_id"] == "alice-store"
    )
    for store_id, status in (("bob-store", 403), ("alice-store", 200)):
        selected = await client.put(
            "/api/xm-store/selection",
            headers=promoted_auth,
            json={"thread_id": thread_id, "store_id": store_id},
        )
        assert selected.status_code == status
    selection = await client.put(
        f"/api/agents/{agent_id}/chat-context/courses/selection",
        headers=promoted_auth,
        json={"thread_id": thread_id, "course_ids": ["alice-course"]},
    )
    assert selection.status_code == 200, selection.text

    async def login_code(_client, *, company_token, qw_id, store_oa_id):
        assert (company_token, qw_id, store_oa_id) == (token, "qw-alice", "oa-alice")
        return "alice-code"

    async def exchange(_client, code):
        assert code == "alice-code"
        return {
            "token": "boh-alice",
            "loginSysUserVo": {
                "permissionCodes": [boh.REPORT_PERMISSION],
                "storeList": [{"id": 11, "code": "001"}],
            },
        }

    requested: list[str] = []

    def upstream(request):
        body = json.loads(request.content)
        requested.append(str(request.url))
        if str(request.url) == yuxue.COURSE_DETAILS_URL:
            assert request.headers["token"] == token
            assert request.headers["systemtype"] == "TALENTS"
            assert body["chooseType"] == "STORE" and body["storeIdList"] == ["alice-store"]
            assert body["courseIdList"] == ["alice-course"] and body["courseStatus"] == "PUBLISHED"
        else:
            assert str(request.url) == boh.REPORT_SPECS["cos"]["url"]
            assert request.headers["token"] == "boh-alice" and body["storeId"] == ["11"]
        return httpx.Response(200, json={"code": 200, "data": {"records": [], "total": 0}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(upstream), **kwargs),
    )
    monkeypatch.setattr(boh, "create_login_code", login_code)
    monkeypatch.setattr(boh, "exchange_login_code", exchange)
    repos = server.services.repos
    connector_service = ConnectorService(
        repo=repos.connector_repo,
        secret_repo=repos.secret_repo,
        settings_repo=repos.settings_repo,
        config=server.services.config,
    )
    report_service = boh.BohReportService(
        repos=repos, config=server.services.config, connector_service=connector_service
    )
    report = await report_service.query(
        user_id=user_id,
        agent_id=agent_id,
        thread_id=thread_id,
        start_date="2026-10-01",
        end_date="2026-10-02",
        finance_category_names=[],
        page_index=1,
    )
    assert report["data"]["records"] == []
    details = await yuxue.query_course_details(
        repos,
        user_id=user_id,
        agent_id=agent_id,
        thread_id=thread_id,
        filters=yuxue.CourseDetailQuery(),
    )
    assert details["data"]["records"] == [] and len(requested) == 2

    demoted = await client.patch(f"/api/users/{user_id}", headers=admin_auth, json={"role": "user"})
    assert demoted.status_code == 200 and demoted.json()["role"] == "user"
    assert (await client.get("/api/users", headers=promoted_auth)).status_code == 403
    assert (await client.get("/api/auth/me", headers=promoted_auth)).json()["role"] == "user"
    remaining = (await client.get("/api/connector-instances", headers=promoted_auth)).json()
    assert {row["instance_id"] for row in remaining} == {row["instance_id"] for row in instances}
    demoted_user, _ = await sign_in(method=login_method)
    assert demoted_user["id"] == user_id and demoted_user["role"] == "user"
