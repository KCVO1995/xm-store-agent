"""BOH managed connector, scoped SSO, and raw MCP data contracts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from octop.infra import boh, xm_store
from octop.infra.connectors.builder import inject_missing_gateway_tools
from octop.infra.connectors.crypto import decrypt_credentials
from octop.infra.connectors.gateway.protocol import handle_mcp_request
from octop.infra.connectors.gateway.registry import mcp_tools_for_kind
from octop.infra.connectors.service import ConnectorService
from octop.infra.errors import ErrorCode, OctopError


@pytest.fixture(autouse=True)
def _without_proxy(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def _service(server):
    services = server.services
    connector_service = ConnectorService(
        repo=services.connector_repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    return boh.BohReportService(
        repos=services.repos, config=services.config, connector_service=connector_service
    )


async def _login(client, monkeypatch, name):
    monkeypatch.setattr(
        xm_store,
        "login_account",
        lambda login_name, _password: {
            "platformUserId": login_name,
            "platformUserName": login_name,
            "qwId": f"qw-{login_name}",
            "token": f"company-{login_name}",
        },
    )
    response = await client.post(
        "/api/auth/xm-store/login", json={"login_name": name, "password": "secret"}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]["id"], response.json()["access_token"]


def _thread(server, *, user_id, agent_id, name, store_id):
    server.services.thread_repo.insert(
        thread_id=name,
        agent_id=agent_id,
        user_id=user_id,
        channel_type="dashboard",
        session_key=f"session-{name}",
    )
    server.services.thread_repo.set_xm_store_id(name, store_id)


def _stores(token):
    owner = token.removeprefix("company-")
    return [
        {
            "store_id": f"{owner}-store-{index}",
            "store_name": f"{owner} 门店 {index}",
            "store_no": f"{owner}-{index}",
            "store_oa_id": f"oa-{owner}-{index}",
        }
        for index in (1, 2)
    ]


def _stub_boh_login(monkeypatch, *, permission=True, string_store_id=False):
    codes = []

    async def code(_client, *, company_token, qw_id, store_oa_id):
        assert qw_id == f"qw-{company_token.removeprefix('company-')}"
        codes.append(store_oa_id)
        return store_oa_id

    async def exchange(_client, code):
        owner = code.removeprefix("oa-").rsplit("-", 1)[0]
        return {
            "token": f"boh-{code}",
            "loginSysUserVo": {
                "permissionCodes": [boh.REPORT_PERMISSION] if permission else [],
                "storeList": [
                    {"id": "100" if string_store_id else 100, "code": f"{owner}-1"},
                    {"id": "200" if string_store_id else 200, "code": f"{owner}-2"},
                ],
            },
        }

    monkeypatch.setattr(boh, "create_login_code", code)
    monkeypatch.setattr(boh, "exchange_login_code", exchange)
    return codes


@pytest.mark.asyncio
async def test_boh_managed_connector_is_created_and_cannot_be_shared_or_edited(
    env_with_main_agent, monkeypatch
):
    client, server, _admin_auth, _agent_id = env_with_main_agent
    user_id, token = await _login(client, monkeypatch, "alice")
    repo = server.services.connector_repo
    company = repo.get_by_user_kind(user_id, xm_store.CONNECTOR_KIND)
    managed = repo.get_by_user_kind(user_id, boh.BOH_CONNECTOR_KIND)
    assert company is not None and managed is not None
    assert managed.user_id == user_id and managed.shared is False
    assert managed.has_credentials
    assert json.loads(managed.config_json or "{}") == {"default_open": True}
    credentials = decrypt_credentials(server.services.secret_repo, managed.credential_blob)
    assert credentials["linked_company_connector"] == company.instance_id
    assert "token" not in credentials
    headers = {"Authorization": f"Bearer {token}"}
    listed = await client.get("/api/connector-instances", headers=headers)
    assert listed.status_code == 200
    assert any(row["mcp_server_name"] == managed.mcp_server_name for row in listed.json())
    rejected = await client.patch(
        f"/api/connector-instances/{managed.instance_id}",
        headers=headers,
        json={"shared": True},
    )
    assert rejected.status_code == 400
    rejected = await client.patch(
        f"/api/connector-instances/{managed.instance_id}",
        headers=headers,
        json={"credentials": {"token": "forged"}},
    )
    assert rejected.status_code == 400
    rejected = await client.delete(
        f"/api/connector-instances/{managed.instance_id}", headers=headers
    )
    assert rejected.status_code == 400
    rejected = await client.post(
        "/api/connector-instances",
        headers=headers,
        json={"kind": "boh", "display_name": "fake", "credentials": {"token": "forged"}},
    )
    assert rejected.status_code == 400
    repo.delete(managed.instance_id)
    boh.ensure_boh_connector_for_user(server.services, user_id)
    assert repo.get_by_user_kind(user_id, boh.BOH_CONNECTOR_KIND) is not None


@pytest.mark.parametrize("string_store_id", [False, True])
@pytest.mark.asyncio
async def test_boh_raw_page_is_scoped_cached_and_separate_mcp_tool(
    env_with_main_agent, monkeypatch, string_store_id
):
    client, server, _admin_auth, agent_id = env_with_main_agent
    alice, _ = await _login(client, monkeypatch, "alice")
    bob, _ = await _login(client, monkeypatch, "bob")
    _thread(server, user_id=alice, agent_id=agent_id, name="alice-boh", store_id="alice-store-1")
    _thread(server, user_id=bob, agent_id=agent_id, name="bob-boh", store_id="bob-store-1")
    monkeypatch.setattr(boh, "list_authorized_stores_with_oa_id", _stores)
    codes = _stub_boh_login(monkeypatch, string_store_id=string_store_id)
    calls = []
    raw = {
        "total": 2,
        "records": [{"rawName": "面粉", "lossCost": "0.10", "actualQuantity": 3}],
        "pageIndex": 2,
        "pageSize": 500,
        "extra": {"keep": True},
    }

    async def report(_client, **kwargs):
        calls.append(kwargs)
        return raw

    monkeypatch.setattr(boh, "fetch_report_page", report)
    service = _service(server)
    params = {
        "user_id": alice,
        "thread_id": "alice-boh",
        "agent_id": agent_id,
        "start_date": "2026-09-17",
        "end_date": "2026-09-23",
        "finance_category_names": ["食材成本"],
        "page_index": 2,
    }
    result = await service.query(**params)
    assert result["data"] == raw
    assert result["query"]["storeId"] == ["100"]
    assert result["query"]["pageSize"] == 500
    assert (await service.query(**params))["data"] == raw
    assert codes == ["oa-alice-1"]
    assert len(calls) == 2
    assert calls[0]["store_id"] == "100" and calls[0]["page_index"] == 2
    assert "amount_summary" not in await service.query(**params)
    company = server.services.connector_repo.get_by_user_kind(alice, xm_store.CONNECTOR_KIND)
    assert company is not None and company.credential_blob is not None
    creds = decrypt_credentials(server.services.secret_repo, company.credential_blob)
    creds["boh_sessions"]["alice-store-1"]["store_id"] = 100
    service._connectors.encrypt_and_store(instance_id=company.instance_id, payload=creds)
    assert (await service.query(**params))["query"]["storeId"] == ["100"]
    assert codes == ["oa-alice-1"]
    server.services.thread_repo.set_xm_store_id("alice-boh", "alice-store-2")
    assert (await service.query(**params))["query"]["storeId"] == ["200"]
    assert calls[-1]["store_id"] == "200"
    assert codes == ["oa-alice-1", "oa-alice-2"]
    with pytest.raises(boh.BohQueryError, match="THREAD_FORBIDDEN"):
        await service.query(**{**params, "user_id": bob})
    server.services.thread_repo.set_xm_store_id("alice-boh", "bob-store-1")
    with pytest.raises(boh.BohQueryError, match="STORE_FORBIDDEN"):
        await service.query(**params)

    company = server.services.connector_repo.get_by_user_kind(alice, xm_store.CONNECTOR_KIND)
    managed = server.services.connector_repo.get_by_user_kind(alice, boh.BOH_CONNECTOR_KIND)
    assert company is not None and managed is not None
    agent = MagicMock()
    agent._mcp_tools = []
    inject_missing_gateway_tools(
        agent,
        svc=service._connectors,
        connector_repo=server.services.connector_repo,
        user_id=alice,
        agent_id=agent_id,
        mcp_server_configs={company.mcp_server_name: {}, managed.mcp_server_name: {}},
        repos=server.services.repos,
        config=server.services.config,
    )
    names = {tool.name for tool in agent.inject_mcp_tools.call_args.args[0]}
    assert any(name.endswith("_list_my_stores") for name in names)
    assert any(name.endswith("_query_store_cos_page") for name in names)
    assert not any("query_food_cost_inventory" in name for name in names)


@pytest.mark.asyncio
async def test_boh_tool_schema_runtime_identity_and_input_validation(
    env_with_main_agent, monkeypatch
):
    client, server, _admin_auth, agent_id = env_with_main_agent
    alice, _ = await _login(client, monkeypatch, "alice")
    _thread(server, user_id=alice, agent_id=agent_id, name="alice-boh", store_id="alice-store-1")
    monkeypatch.setattr(boh, "list_authorized_stores_with_oa_id", _stores)
    _stub_boh_login(monkeypatch)

    async def report(_client, **_kwargs):
        return {"total": 0, "records": [], "pageIndex": 1, "pageSize": 500}

    monkeypatch.setattr(boh, "fetch_report_page", report)
    definition = mcp_tools_for_kind("boh")[0]
    schema = definition["inputSchema"]
    assert set(schema["required"]) == {"startDate", "endDate", "financeCategoryNames"}
    assert set(schema["properties"]) == {
        "startDate",
        "endDate",
        "financeCategoryNames",
        "pageIndex",
    }
    assert "500" in definition["description"]
    assert "500" in schema["properties"]["pageIndex"]["description"]
    tool = boh.build_boh_report_tool(
        mcp_server_name="boh__test",
        repos=server.services.repos,
        config=server.services.config,
        connector_service=_service(server)._connectors,
        agent_id=agent_id,
    )
    args = {
        "startDate": "2026-09-17",
        "endDate": "2026-09-23",
        "financeCategoryNames": [],
    }
    monkeypatch.setattr(boh, "get_config", lambda: {"configurable": {}})
    assert json.loads(await tool.ainvoke(args))["error"] == "THREAD_FORBIDDEN"
    monkeypatch.setattr(
        boh, "get_config", lambda: {"configurable": {"user": str(alice), "thread_id": "alice-boh"}}
    )
    assert json.loads(await tool.ainvoke(args))["status"] == "ok"
    service = _service(server)
    base = {
        "user_id": alice,
        "thread_id": "alice-boh",
        "agent_id": agent_id,
        "start_date": "2026-09-17",
        "end_date": "2026-09-23",
        "finance_category_names": [],
        "page_index": 1,
    }
    for updates, code in (
        ({"end_date": "2026-09-16"}, "INVALID_DATE_RANGE"),
        ({"end_date": "2026-12-01"}, "INVALID_DATE_RANGE"),
        ({"start_date": "2026/09/17"}, "INVALID_DATE"),
        ({"page_index": 51}, "INVALID_PAGE"),
        ({"finance_category_names": [""]}, "INVALID_CATEGORY"),
    ):
        with pytest.raises(boh.BohQueryError, match=code):
            await service.query(**{**base, **updates})
    response = handle_mcp_request(
        kind="boh",
        creds={"linked_company_connector": "irrelevant"},
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": boh.TOOL_NAME}},
    )
    assert response["result"]["isError"] is True


@pytest.mark.asyncio
async def test_boh_expiration_permission_and_large_result(env_with_main_agent, monkeypatch):
    client, server, _admin_auth, agent_id = env_with_main_agent
    alice, _ = await _login(client, monkeypatch, "alice")
    _thread(server, user_id=alice, agent_id=agent_id, name="alice-boh", store_id="alice-store-1")
    monkeypatch.setattr(boh, "list_authorized_stores_with_oa_id", _stores)
    _stub_boh_login(monkeypatch, permission=False)
    service = _service(server)
    args = {
        "user_id": alice,
        "thread_id": "alice-boh",
        "agent_id": agent_id,
        "start_date": "2026-09-17",
        "end_date": "2026-09-23",
        "finance_category_names": ["食材成本"],
        "page_index": 1,
    }
    with pytest.raises(boh.BohQueryError, match="BOH_REPORT_FORBIDDEN"):
        await service.query(**args)
    codes = _stub_boh_login(monkeypatch)
    calls = 0

    async def expired_once(_client, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise boh._BohTokenExpired
        return {"total": 1, "records": [{"rawName": "A"}]}

    monkeypatch.setattr(boh, "fetch_report_page", expired_once)
    assert (await service.query(**args))["data"]["total"] == 1
    assert calls == 2 and codes == ["oa-alice-1", "oa-alice-1"]

    async def too_large(_client, **_kwargs):
        return {"total": 1, "records": [{"rawName": "x" * (256 * 1024)}]}

    monkeypatch.setattr(boh, "fetch_report_page", too_large)
    with pytest.raises(boh.BohQueryError, match="BOH_RESULT_TOO_LARGE"):
        await service.query(**args)
    company = server.services.connector_repo.get_by_user_kind(alice, xm_store.CONNECTOR_KIND)
    assert company is not None and company.credential_blob is not None
    creds = decrypt_credentials(server.services.secret_repo, company.credential_blob)
    creds["boh_sessions"]["alice-store-1"]["expires_at"] = 0
    service._connectors.encrypt_and_store(instance_id=company.instance_id, payload=creds)
    with pytest.raises(boh.BohQueryError, match="BOH_RESULT_TOO_LARGE"):
        await service.query(**args)
    assert len(codes) == 3
    await _login(client, monkeypatch, "alice")
    refreshed = server.services.connector_repo.get_by_user_kind(alice, xm_store.CONNECTOR_KIND)
    assert refreshed is not None and refreshed.credential_blob is not None
    assert "boh_sessions" not in decrypt_credentials(
        server.services.secret_repo, refreshed.credential_blob
    )
    monkeypatch.setattr(
        boh,
        "list_authorized_stores_with_oa_id",
        lambda _token: (_ for _ in ()).throw(OctopError(ErrorCode.TOKEN_EXPIRED, "expired")),
    )
    with pytest.raises(OctopError) as expired:
        await service.query(**args)
    assert expired.value.code == ErrorCode.TOKEN_EXPIRED


@pytest.mark.parametrize("report_code", [0, "0", 200, "200"])
@pytest.mark.asyncio
async def test_boh_http_contract_and_success_codes(report_code):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if str(request.url) == boh.SSO_CODE_URL:
            assert request.headers["token"] == "company-secret"
            return httpx.Response(200, json={"code": 0, "data": {"platformLoginCode": "one-time"}})
        if request.url.path.endswith("loginByXMStoreSuiteCode"):
            assert request.url.params["xmLoginCode"] == "one-time"
            return httpx.Response(200, json={"code": 200, "data": {"token": "boh-secret"}})
        assert str(request.url) == boh.BOH_REPORT_URL
        assert request.headers["token"] == "boh-secret"
        assert json.loads(request.content) == {
            "startDate": "2026-09-17",
            "endDate": "2026-09-23",
            "dateType": 1,
            "financeCategoryNames": ["食材成本"],
            "storeId": ["100"],
            "pageIndex": 2,
            "pageSize": 500,
            "bzywlflSonNames": None,
            "isMatchRaw": 0,
            "orderingCategoryList": [],
        }
        data = {
            "total": 1,
            "records": [{"rawName": "面粉", "receiveNet": 3.14}],
            "pageIndex": 2,
            "pageSize": 500,
        }
        return httpx.Response(200, json={"code": report_code, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False) as client:
        code = await boh.create_login_code(
            client, company_token="company-secret", qw_id="qw-1", store_oa_id="oa-1"
        )
        login = await boh.exchange_login_code(client, code)
        page = await boh.fetch_report_page(
            client,
            token=login["token"],
            store_id="100",
            start_date="2026-09-17",
            end_date="2026-09-23",
            finance_category_names=["食材成本"],
            page_index=2,
        )
    assert len(seen) == 3
    assert page["records"] == [{"rawName": "面粉", "receiveNet": 3.14}]


@pytest.mark.parametrize("report_code", [401, 500])
def test_boh_report_rejects_auth_and_business_errors(report_code):
    response = httpx.Response(200, json={"code": report_code, "data": {}})
    expected = boh._BohTokenExpired if report_code == 401 else boh.BohQueryError
    with pytest.raises(expected):
        boh._upstream_data(response, report=True)
