"""BOH managed connector, scoped SSO, and raw MCP data contracts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from octop.infra import boh, xm_store
from octop.infra.agents.manager import AgentCreateSpec
from octop.infra.connectors.builder import inject_missing_gateway_tools
from octop.infra.connectors.crypto import decrypt_credentials
from octop.infra.connectors.gateway.protocol import handle_mcp_request
from octop.infra.connectors.gateway.registry import mcp_tools_for_kind
from octop.infra.connectors.service import ConnectorService
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.skills.boh_managed import PACKAGE_NAME, mount_existing_store_assistants


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


def _stub_boh_login(monkeypatch, *, permission=True, string_store_id=False, permissions=None):
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
                "permissionCodes": (
                    permissions
                    if permissions is not None
                    else ([boh.REPORT_PERMISSION] if permission else [])
                ),
                "storeList": [
                    {"id": "100" if string_store_id else 100, "code": f"{owner}-1"},
                    {"id": "200" if string_store_id else 200, "code": f"{owner}-2"},
                ],
            },
        }

    monkeypatch.setattr(boh, "create_login_code", code)
    monkeypatch.setattr(boh, "exchange_login_code", exchange)
    return codes


@pytest.mark.parametrize(
    ("kind", "expected_extra"),
    [
        (
            "cos",
            {
                "dateType": 1,
                "financeCategoryNames": ["食材成本"],
                "bzywlflSonNames": None,
                "orderingCategoryList": [],
                "riIds": [14766],
            },
        ),
        ("generic", {"dateType": 1, "financeCategoryNames": ["食材成本"], "bzywlflSonNames": None}),
        ("assessment_week", {"count": True, "login": True, "isShowSpecialColumn": True}),
        (
            "generic_week",
            {
                "count": True,
                "login": True,
                "isShowSpecialColumn": True,
                "bzywlflSonNames": None,
                "excludeCondiment": True,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_boh_new_report_request_contract(kind, expected_extra):
    def handle(request):
        assert str(request.url) == boh.REPORT_SPECS[kind]["url"]
        body = json.loads(request.content)
        assert body == {
            "startDate": "2026-09-01",
            "endDate": "2026-09-23",
            "pageIndex": 2,
            "pageSize": 25,
            "storeId": ["4764"],
            "isMatchRaw": 0,
            **expected_extra,
        }
        data = {
            "total": 1,
            "records": [{"usageDiffCost": "12.37", "extra": "keep"}],
            "pageIndex": 2,
            "pageSize": 25,
        }
        if kind.endswith("_week"):
            data["headers"] = [{"key": "week_1", "label": "第1周"}]
        return httpx.Response(200, json={"code": 200, "data": data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        data = await boh.fetch_boh_page(
            client,
            kind=kind,
            token="secret",
            store_id="4764",
            start_date="2026-09-01",
            end_date="2026-09-23",
            finance_category_names=["食材成本"] if kind in {"cos", "generic"} else None,
            page_index=2,
            page_size=25,
            raw_item_ids=[14766] if kind == "cos" else None,
        )
    assert data["records"][0]["extra"] == "keep"
    if kind.endswith("_week"):
        assert data["headers"][0]["key"] == "week_1"


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


@pytest.mark.asyncio
async def test_boh_skill_mounts_only_store_assistant_template(env_with_main_agent, monkeypatch):
    client, server, _admin_auth, _agent_id = env_with_main_agent
    alice, _ = await _login(client, monkeypatch, "alice")
    package = server.services.skill_package_repo.get_by_name(PACKAGE_NAME)
    assert package is not None
    skill_path = (
        server.paths.skill_packages_dir / package.id / "skills" / "boh-store-variance" / "SKILL.md"
    )
    assert "query_store_generic_week_page" in skill_path.read_text(encoding="utf-8")
    registry = server.app_runtime.agent_registry

    async def no_start(_row, **_kwargs):
        return None

    async def no_seed(_row, _template_name):
        return None

    monkeypatch.setattr(registry, "_start_agent", no_start)
    monkeypatch.setattr(registry, "_seed_expert_template", no_seed)
    templated = await registry.create(
        AgentCreateSpec(name="分析助手", user_id=alice, template_name="xm-store-assistant")
    )
    ordinary = await registry.create(AgentCreateSpec(name="其他 Agent", user_id=alice))
    assert package.id in json.loads(templated.skill_package_ids or "[]")
    assert package.id not in json.loads(ordinary.skill_package_ids or "[]")
    legacy = server.services.agent_repo.create(
        agent_id="legacy-store", user_id=alice, name="门店助手"
    )
    mount_existing_store_assistants(server.services, package.id)
    row = server.services.agent_repo.get(legacy)
    assert row is not None and package.id in json.loads(row.skill_package_ids or "[]")


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
    assert any(name.endswith("_query_store_generic_page") for name in names)
    assert any(name.endswith("_query_store_assessment_week_page") for name in names)
    assert any(name.endswith("_query_store_generic_week_page") for name in names)
    assert any(name.endswith("_search_raw_items") for name in names)
    assert any(name.endswith("_analyze_boh_variance") for name in names)
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
    assert set(schema["required"]) == {"startDate", "endDate"}
    assert set(schema["properties"]) == {
        "startDate",
        "endDate",
        "financeCategoryNames",
        "pageIndex",
        "pageSize",
        "datasetId",
        "rawItemIds",
    }
    assert "500" in definition["description"]
    assert "500" in schema["properties"]["pageSize"]["description"]
    assert schema["properties"]["financeCategoryNames"]["default"] == ["食材成本"]
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
async def test_boh_tool_set_dispatches_default_category_and_weekly_filters(
    env_with_main_agent, monkeypatch
):
    _client, server, _admin_auth, agent_id = env_with_main_agent
    calls = []

    async def fake_query(_self, **kwargs):
        calls.append(kwargs)
        return {"query": {}, "data": {"total": 0, "records": []}, "datasetId": "test"}

    monkeypatch.setattr(boh.BohReportService, "query", fake_query)
    monkeypatch.setattr(
        boh, "get_config", lambda: {"configurable": {"user": "7", "thread_id": "trusted"}}
    )
    tools = boh.build_boh_report_tools(
        mcp_server_name="boh__test",
        repos=server.services.repos,
        config=server.services.config,
        connector_service=_service(server)._connectors,
        agent_id=agent_id,
    )
    assert len(tools) == 8
    generic = next(tool for tool in tools if tool.name.endswith("_query_store_generic_page"))
    weekly = next(tool for tool in tools if tool.name.endswith("_query_store_generic_week_page"))
    assert (
        json.loads(await generic.ainvoke({"startDate": "2026-09-01", "endDate": "2026-09-23"}))[
            "status"
        ]
        == "ok"
    )
    assert calls[-1]["report_kind"] == "generic"
    assert calls[-1]["finance_category_names"] == ["食材成本"]
    assert (
        json.loads(
            await weekly.ainvoke(
                {"startDate": "2026-09-01", "endDate": "2026-09-23", "pageIndex": 2}
            )
        )["status"]
        == "ok"
    )
    assert calls[-1]["report_kind"] == "generic_week"
    assert calls[-1]["finance_category_names"] == []
    assert calls[-1]["page_index"] == 2


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
    await _login(client, monkeypatch, "alice")
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


@pytest.mark.asyncio
async def test_boh_snapshot_requires_complete_pages_and_same_conversation(
    env_with_main_agent, monkeypatch
):
    client, server, _admin_auth, agent_id = env_with_main_agent
    alice, _ = await _login(client, monkeypatch, "alice")
    bob, _ = await _login(client, monkeypatch, "bob")
    _thread(server, user_id=alice, agent_id=agent_id, name="alice-boh", store_id="alice-store-1")
    _thread(server, user_id=alice, agent_id=agent_id, name="alice-other", store_id="alice-store-1")
    _thread(server, user_id=bob, agent_id=agent_id, name="bob-boh", store_id="bob-store-1")
    monkeypatch.setattr(boh, "list_authorized_stores_with_oa_id", _stores)
    _stub_boh_login(monkeypatch)

    async def report(_client, **kwargs):
        index = kwargs["page_index"]
        return {
            "total": 2,
            "pageIndex": index,
            "pageSize": 1,
            "records": [
                {"rawCode": str(index), "rawName": f"物料{index}", "usageDiffCost": str(index)}
            ],
        }

    monkeypatch.setattr(boh, "fetch_report_page", report)
    service = _service(server)
    params = {
        "user_id": alice,
        "thread_id": "alice-boh",
        "agent_id": agent_id,
        "start_date": "2026-09-01",
        "end_date": "2026-09-23",
        "finance_category_names": ["食材成本"],
        "page_size": 1,
    }
    first = await service.query(**params, page_index=1)
    dataset = first["datasetId"]
    analysis = {
        "user_id": alice,
        "thread_id": "alice-boh",
        "agent_id": agent_id,
        "dataset_ids": [dataset],
        "mode": "overview",
        "start_date": "2026-09-01",
        "end_date": "2026-09-23",
    }
    with pytest.raises(boh.BohQueryError, match="BOH_INCOMPLETE_RESULT"):
        await service.analyze(**analysis)
    with pytest.raises(boh.BohQueryError, match="SNAPSHOT_MISMATCH"):
        await service.analyze(**{**analysis, "thread_id": "alice-other"})
    with pytest.raises(boh.BohQueryError, match="THREAD_FORBIDDEN"):
        await service.analyze(**{**analysis, "user_id": bob})
    await service.query(**params, page_index=2, dataset_id=dataset)
    result = await service.analyze(**analysis)
    assert result["reports"]["cos"]["usageDiffCost"] == "3.00"
    assert result["unavailableReports"] == ["assessment_week", "generic", "generic_week"]
    assert result["notQueriedReports"] == []
    server.services.thread_repo.set_xm_store_id("alice-boh", "alice-store-2")
    with pytest.raises(boh.BohQueryError, match="SNAPSHOT_MISMATCH"):
        await service.analyze(**analysis)


@pytest.mark.asyncio
async def test_boh_material_search_and_daily_analysis(env_with_main_agent, monkeypatch):
    client, server, _admin_auth, agent_id = env_with_main_agent
    alice, _ = await _login(client, monkeypatch, "alice")
    _thread(server, user_id=alice, agent_id=agent_id, name="alice-boh", store_id="alice-store-1")
    monkeypatch.setattr(boh, "list_authorized_stores_with_oa_id", _stores)
    _stub_boh_login(monkeypatch)

    def handle(request):
        assert str(request.url) == boh.BOH_RAW_ITEM_URL
        assert request.headers["token"] == "boh-oa-alice-1"
        assert json.loads(request.content) == {"pageIndex": 1, "pageSize": 10000, "type": 1}
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "total": 2,
                    "records": [
                        {"id": 14766, "code": "A", "name": "卤香烤鸡", "griName": "卤香烤鸡"},
                        {"id": 14767, "code": "B", "name": "其他", "griName": "其他"},
                    ],
                },
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        boh.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handle), trust_env=False, **kwargs
        ),
    )
    service = _service(server)
    found = await service.search_materials(
        user_id=alice, thread_id="alice-boh", agent_id=agent_id, keyword="卤香烤鸡"
    )
    assert found["totalMatches"] == 1 and found["records"][0]["id"] == 14766

    async def report(_client, **kwargs):
        assert kwargs["raw_item_ids"] == [14766]
        day = kwargs["start_date"]
        return {
            "total": 1 if day == "2026-09-22" else 0,
            "pageIndex": 1,
            "pageSize": 50,
            "records": [{"storeId": 100, "rawCode": "A", "usageDiffCost": "60"}]
            if day == "2026-09-22"
            else [],
        }

    monkeypatch.setattr(boh, "fetch_report_page", report)
    raw = await service.query_daily(
        user_id=alice,
        thread_id="alice-boh",
        agent_id=agent_id,
        start_date="2026-09-22",
        end_date="2026-09-23",
        finance_category_names=["食材成本"],
        raw_item_ids=[14766],
    )
    assert raw["days"]["2026-09-23"]["records"] == []
    result = await service.analyze(
        user_id=alice,
        thread_id="alice-boh",
        agent_id=agent_id,
        dataset_ids=[raw["days"][day]["datasetId"] for day in sorted(raw["days"])],
        mode="daily",
        start_date="2026-09-22",
        end_date="2026-09-23",
    )
    assert result["days"][0]["usageDiffCost"] == "60.00"
    assert result["days"][1]["status"] == "no_data"


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
