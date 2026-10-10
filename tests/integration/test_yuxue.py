"""Managed Yuxue queries use the acting user's trusted conversation selections."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError

from octop.infra import qixuebao_courses, xm_store, yuxue
from octop.infra.connectors.builder import inject_missing_gateway_tools
from octop.infra.connectors.crypto import decrypt_credentials
from octop.infra.connectors.gateway.protocol import handle_mcp_request
from octop.infra.connectors.gateway.registry import mcp_tools_for_kind
from octop.infra.connectors.service import ConnectorService
from octop.infra.errors import ErrorCode, OctopError


@pytest.fixture
async def course_chat(env_with_main_agent, monkeypatch):
    client, server, admin_auth, agent_id = env_with_main_agent
    monkeypatch.setattr(
        xm_store,
        "login_account",
        lambda login_name, _password: {
            "platformUserId": login_name,
            "platformUserName": login_name,
            "token": f"company-{login_name}",
        },
    )
    response = await client.post(
        "/api/auth/xm-store/login", json={"login_name": "alice", "password": "secret"}
    )
    assert response.status_code == 200, response.text
    user_id = response.json()["user"]["id"]
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    configured = await client.patch(
        f"/api/agents/{agent_id}",
        headers=admin_auth,
        json={"is_shared": True, "chat_selectors": ["xm_store", "qixuebao_course"]},
    )
    assert configured.status_code == 200, configured.text
    thread = await client.post(f"/api/agents/{agent_id}/threads", headers=auth)
    assert thread.status_code == 201, thread.text
    thread_id = thread.json()["thread_id"]
    server.services.thread_repo.set_xm_store_id(thread_id, "alice-store")
    server.services.thread_repo.set_chat_context_ids(
        thread_id, "qixuebao_course", ["alice-course-1", "alice-course-2"]
    )

    def stores(token):
        assert token == "company-alice"
        return [{"store_id": "alice-store", "store_name": "门店", "store_no": "001"}]

    def courses(token, *, keyword="", page_num=1):
        assert token == "company-alice"
        return {
            "courses": [{"course_id": f"alice-course-{page_num}", "course_name": "课程"}],
            "pages": 2,
        }

    monkeypatch.setattr(xm_store, "list_authorized_stores_with_oa_id", stores)
    monkeypatch.setattr(qixuebao_courses, "query_authorized_courses", courses)
    monkeypatch.setattr(
        yuxue,
        "get_config",
        lambda: {"configurable": {"user": str(user_id), "thread_id": thread_id}},
    )
    return client, server, auth, agent_id, user_id, thread_id


@pytest.mark.asyncio
async def test_managed_connector_lifecycle_and_gateway_injection(course_chat):
    client, server, auth, agent_id, user_id, _thread_id = course_chat
    repos = server.services.repos
    company = repos.connector_repo.get_by_user_kind(user_id, "xm-store")
    managed = repos.connector_repo.get_by_user_kind(user_id, yuxue.CONNECTOR_KIND)
    assert company is not None and managed is not None
    assert managed.user_id == user_id and managed.shared is False
    assert json.loads(managed.config_json) == {"default_open": True}
    credentials = decrypt_credentials(repos.secret_repo, managed.credential_blob)
    assert credentials["linked_company_connector"] == company.instance_id
    assert "token" not in credentials
    yuxue.ensure_yuxue_connector_for_user(server.services, user_id)
    assert (
        repos.connector_repo.get_by_user_kind(user_id, yuxue.CONNECTOR_KIND).instance_id
        == managed.instance_id
    )
    listed = await client.get("/api/connector-instances", headers=auth)
    assert any(row["instance_id"] == managed.instance_id for row in listed.json())
    catalog = await client.get("/api/connectors/catalog", headers=auth)
    entry = next(row for row in catalog.json() if row["kind"] == "yuxue")
    assert entry["name"] == "遇学" and entry["auth_kind"] == "managed"
    route = f"/api/connector-instances/{managed.instance_id}"
    for update in (
        {"shared": True},
        {"credentials": {"token": "forged"}},
        {"display_name": "fake"},
    ):
        assert (await client.patch(route, headers=auth, json=update)).status_code == 400
    assert (await client.delete(route, headers=auth)).status_code == 400
    assert (
        await client.post(
            "/api/connector-instances", headers=auth, json={"kind": "yuxue", "display_name": "fake"}
        )
    ).status_code == 400
    assert (
        await client.patch(route, headers=auth, json={"default_open": False})
    ).status_code == 200
    svc = ConnectorService(
        repo=repos.connector_repo,
        secret_repo=repos.secret_repo,
        settings_repo=repos.settings_repo,
        config=server.services.config,
    )
    agent = MagicMock(_mcp_tools=[])
    inject_missing_gateway_tools(
        agent,
        svc=svc,
        connector_repo=repos.connector_repo,
        user_id=user_id,
        agent_id=agent_id,
        mcp_server_configs={managed.mcp_server_name: {}},
        repos=repos,
        config=server.services.config,
    )
    tools = agent.inject_mcp_tools.call_args.args[0]
    assert len(tools) == 1 and tools[0].name.endswith(f"_{yuxue.TOOL_NAME}")
    assert tools[0].args_schema is yuxue.CourseDetailQuery
    repos.connector_repo.delete(managed.instance_id)
    yuxue.ensure_yuxue_connector_for_user(server.services, user_id)
    assert repos.connector_repo.get_by_user_kind(user_id, yuxue.CONNECTOR_KIND).has_credentials


@pytest.mark.asyncio
async def test_query_uses_current_actor_selections_and_filters(course_chat, monkeypatch):
    _client, server, _auth, agent_id, user_id, thread_id = course_chat
    assert server.services.agent_repo.get(agent_id).user_id != user_id
    requests = []
    page = {
        "current": 2,
        "size": 25,
        "pages": 3,
        "total": 60,
        "records": [
            {
                "courseName": "第一课",
                "completeStatus": "LS_PROGRESS",
                "courseProgress": "50%",
                "staffInfoResult": {
                    "baseId": "person-1",
                    "staffName": "测试人员",
                    "storeId": "alice-store",
                    "postName": "店员",
                },
            }
        ],
    }

    def handle(request):
        requests.append(request)
        assert str(request.url) == yuxue.COURSE_DETAILS_URL
        assert request.headers["token"] == "company-alice"
        assert request.headers["systemtype"] == "TALENTS"
        return httpx.Response(200, json={"code": 200, "data": page})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        yuxue.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    tool = yuxue.build_yuxue_tools(
        mcp_server_name="yuxue__test", repos=server.services.repos, agent_id=agent_id
    )[0]
    filters = {
        "pageNum": 2,
        "pageSize": 25,
        "courseStatus": "CLOSED",
        "completeStatus": ["LS_PROGRESS", "LS_INCOMPLETE"],
        "courseName": "第一课",
        "staffName": "测试人员",
        "baseIdList": ["person-1"],
        "completeTimeStart": "2026-10-01T00:00:00",
        "completeTimeEnd": "2026-10-09T23:59:59",
        "joinTimeStart": "2026-09-01T00:00:00",
        "joinTimeEnd": "2026-09-30T23:59:59",
    }
    result = json.loads(await tool.ainvoke(filters))
    assert result["status"] == "ok" and result["data"] == page
    assert json.loads(requests[-1].content) == {
        **filters,
        "chooseType": "STORE",
        "storeIdList": ["alice-store"],
        "courseIdList": ["alice-course-1", "alice-course-2"],
    }
    assert "company-alice" not in json.dumps(result)
    server.services.thread_repo.set_chat_context_ids(thread_id, "qixuebao_course", [])
    assert json.loads(await tool.ainvoke({}))["status"] == "ok"
    assert json.loads(requests[-1].content) == {
        "pageNum": 1,
        "pageSize": 100,
        "courseStatus": "PUBLISHED",
        "chooseType": "STORE",
        "storeIdList": ["alice-store"],
        "courseIdList": [],
    }
    with pytest.raises(ValidationError):
        await tool.ainvoke({"storeIdList": ["not-authorized"]})
    server.services.thread_repo.set_xm_store_id(thread_id, "not-authorized")
    assert json.loads(await tool.ainvoke({}))["error"] == "STORE_FORBIDDEN"
    server.services.thread_repo.set_xm_store_id(thread_id, "")
    assert json.loads(await tool.ainvoke({}))["error"] == "STORE_REQUIRED"
    server.services.thread_repo.set_xm_store_id(thread_id, "alice-store")
    server.services.thread_repo.set_chat_context_ids(
        thread_id, "qixuebao_course", ["revoked-course"]
    )
    assert json.loads(await tool.ainvoke({}))["error"] == "FORBIDDEN"
    owner_id = server.services.agent_repo.get(agent_id).user_id
    monkeypatch.setattr(
        yuxue,
        "get_config",
        lambda: {"configurable": {"user": str(owner_id), "thread_id": thread_id}},
    )
    assert json.loads(await tool.ainvoke({}))["error"] == "THREAD_FORBIDDEN"
    monkeypatch.setattr(yuxue, "get_config", lambda: {"configurable": {}})
    assert json.loads(await tool.ainvoke({}))["error"] == "THREAD_FORBIDDEN"
    monkeypatch.setattr(
        yuxue,
        "get_config",
        lambda: {"configurable": {"user": str(user_id), "thread_id": thread_id}},
    )
    company = server.services.connector_repo.get_by_user_kind(user_id, "xm-store")
    server.services.connector_repo.update_status(company.instance_id, "disabled")
    assert json.loads(await tool.ainvoke({}))["error"] == "COMPANY_LOGIN_REQUIRED"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "scope", ["chooseType", "storeIdList", "courseIdList", "user", "thread_id", "token"]
)
def test_model_cannot_supply_authorization_scope(scope):
    with pytest.raises(ValidationError):
        yuxue.CourseDetailQuery.model_validate({scope: "forged"})


@pytest.mark.parametrize(
    "filters",
    [
        {"pageNum": 0},
        {"pageSize": 0},
        {"pageSize": 1001},
        {"courseStatus": "开放中"},
        {"completeStatus": ["unknown"]},
    ],
)
def test_invalid_filters_are_rejected(filters):
    with pytest.raises(ValidationError):
        yuxue.CourseDetailQuery.model_validate(filters)


def test_discovery_retains_array_and_enum_schema_but_contextless_calls_fail():
    definition = mcp_tools_for_kind("yuxue")[0]
    schema = definition["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["courseStatus"]["default"] == "PUBLISHED"
    assert schema["properties"]["completeStatus"]["anyOf"][0]["type"] == "array"
    response = handle_mcp_request(
        kind="yuxue",
        creds={"linked_company_connector": "company"},
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": yuxue.TOOL_NAME},
        },
    )
    assert response["result"]["isError"] is True


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {}, "TOKEN_EXPIRED"),
        (200, {"code": 401}, "TOKEN_EXPIRED"),
        (403, {}, "COURSE_FORBIDDEN"),
        (200, {"code": 403}, "COURSE_FORBIDDEN"),
        (500, {}, "YUXUE_UNAVAILABLE"),
        (200, {"code": 500}, "YUXUE_UPSTREAM_ERROR"),
        (200, {"records": "invalid"}, "YUXUE_BAD_RESPONSE"),
        (200, {"records": [{"courseName": "x" * (256 * 1024)}]}, "YUXUE_RESULT_TOO_LARGE"),
    ],
)
@pytest.mark.asyncio
async def test_upstream_failure_contract(status, body, error):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=body))
    ) as client:
        with pytest.raises((OctopError, yuxue.YuxueQueryError)) as failed:
            await yuxue.fetch_course_detail_page(client, token="test-token", query={})
    code = failed.value.code
    assert (code.value if isinstance(code, ErrorCode) else code) == error


@pytest.mark.asyncio
async def test_bare_page_and_timeout():
    page = {"records": [], "total": 0, "current": 1, "size": 100, "pages": 0}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=page))
    ) as client:
        assert await yuxue.fetch_course_detail_page(client, token="test-token", query={}) == page

    def timeout(request):
        raise httpx.ReadTimeout("test", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(yuxue.YuxueQueryError, match="YUXUE_TIMEOUT"):
            await yuxue.fetch_course_detail_page(client, token="test-token", query={})
