"""Company API response normalization and expiry handling."""

from __future__ import annotations

import json

import httpx
import pytest

from octop.infra.connectors.gateway.adapters import xm_store as store_adapter
from octop.infra.connectors.gateway.protocol import handle_mcp_request
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.xm_store import (
    _RedactLoginUrl,
    list_authorized_stores,
    login_account,
    login_by_code,
    send_verification_code,
)


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


def test_company_sms_send_and_login_contract(monkeypatch):
    original_client = httpx.Client
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/send/code"):
            assert request.method == "POST"
            assert dict(request.url.params) == {
                "phoneNumber": "8613812345678",
                "systemType": "XM_STORE_SUITE",
            }
            return httpx.Response(200, json={"code": 0, "data": {"codeId": "internal"}})
        assert request.url.path.endswith("/loginByCode")
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "phoneNumber": "8613812345678",
            "code": "123456",
            "systemType": "XM_STORE_SUITE",
        }
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"platformUserId": "u1", "token": "secret", "qwId": "qw-1"},
            },
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    assert send_verification_code("8613812345678") is None
    assert login_by_code("8613812345678", "123456")["qwId"] == "qw-1"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"code": 405, "msg": "验证码错误"}, ErrorCode.XM_STORE_CODE_INVALID),
        (429, {}, ErrorCode.XM_STORE_SMS_THROTTLED),
        (503, {}, ErrorCode.INTERNAL_ERROR),
    ],
)
def test_company_sms_login_errors(monkeypatch, status, body, expected):
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
            **kwargs,
        ),
    )
    with pytest.raises(OctopError) as error:
        login_by_code("8613812345678", "wrong")
    assert error.value.code == expected


def test_company_sms_send_rejection_and_log_redaction(monkeypatch):
    import logging

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"code": 500, "msg": "发送失败"})
            ),
            **kwargs,
        ),
    )
    with pytest.raises(OctopError) as error:
        send_verification_code("8613812345678")
    assert error.value.code == ErrorCode.XM_STORE_SMS_SEND_FAILED

    record = logging.LogRecord(
        "httpx._client",
        logging.INFO,
        "test",
        1,
        "HTTP Request: POST https://digital.yujianxiaomian.com/meet-digital-manager/sso/send/code?phoneNumber=8613812345678",
        (),
        None,
    )
    assert _RedactLoginUrl().filter(record)
    assert "8613812345678" not in record.getMessage()


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
