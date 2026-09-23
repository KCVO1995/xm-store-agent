"""Company API response normalization and expiry handling."""

from __future__ import annotations

import httpx
import pytest

from octop.infra.connectors.gateway.adapters import xm_store as store_adapter
from octop.infra.connectors.gateway.protocol import handle_mcp_request
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.xm_store import list_authorized_stores, login_account


def test_company_login_uses_required_system_type(monkeypatch):
    original_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.params["systemType"] == "XM_STORE_SUITE"
        assert request.url.params["loginName"] == "alice"
        assert request.url.params["password"] == "pw"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"platformUserId": "u1", "token": "secret"}},
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert login_account("alice", "pw")["token"] == "secret"


def test_store_list_uses_token_and_normalizes_data(monkeypatch):
    original_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["token"] == "secret"
        assert request.headers["lang"] == "zh"
        assert request.content == b"{}"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [{"storeId": "S1", "storeNameCN": "广州店", "storeNo": "001"}],
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert list_authorized_stores("secret") == [
        {"store_id": "S1", "store_name": "广州店", "store_no": "001"}
    ]


@pytest.mark.parametrize("code", [401, 405, 406])
def test_store_expiry_codes_trigger_relogin(monkeypatch, code: int):
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": code})),
            **kwargs,
        ),
    )
    with pytest.raises(OctopError) as error:
        list_authorized_stores("expired")
    assert error.value.code == ErrorCode.TOKEN_EXPIRED


def test_mcp_store_tool_returns_expiry_feedback(monkeypatch):
    def expired(_token: str):
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "expired")

    monkeypatch.setattr(store_adapter, "list_authorized_stores", expired)
    output = store_adapter.call_tool({"token": "expired"}, "list_my_stores", {})
    assert '"code": "TOKEN_EXPIRED"' in output
    response = handle_mcp_request(
        kind="xm-store",
        creds={"token": "expired"},
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_my_stores", "arguments": {}},
        },
    )
    assert response["result"]["isError"] is True
