"""Company account and authorized-store API for the store assistant."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from octop.infra.connectors.builder import mcp_server_name, new_internal_token
from octop.infra.connectors.crypto import decrypt_credentials
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.preferences import parse_preferences_json
from octop.infra.utils.ulid import new_ulid

LOGIN_URL = "https://digital.yujianxiaomian.com/meet-digital-manager/sso/v1/login"
STORES_URL = (
    "https://app-container.yujianxiaomian.com/meet-digital-facade-app-container/"
    "appcontainer/store/queryListByStoreCodeList"
)
CONNECTOR_KIND = "xm-store"
EXPIRED_CODES = {401, 405, 406}
_PREFERENCE_KEY = "xm_store_last_store_id"


class _RedactLoginUrl(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if LOGIN_URL in record.getMessage():
            record.msg = "HTTP Request: company account login [URL redacted]"
            record.args = ()
        return True


logging.getLogger("httpx._client").addFilter(_RedactLoginUrl())


def _payload(response: httpx.Response, *, login: bool) -> Any:
    if not login and response.status_code in EXPIRED_CODES:
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
    if login and response.status_code in (401, 403):
        raise OctopError(ErrorCode.AUTH_FAILED, "company credentials rejected")
    if response.is_error:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "company service unavailable", status=502)
    try:
        body = response.json()
    except ValueError as exc:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "invalid company response", status=502) from exc
    if not isinstance(body, dict):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "invalid company response", status=502)
    code = body.get("code")
    if not login and (
        str(code) in {str(item) for item in EXPIRED_CODES}
        or str(body.get("httpStatus")) in {str(item) for item in EXPIRED_CODES}
    ):
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
    if login and code not in (None, 0, "0", 200, "200"):
        raise OctopError(ErrorCode.AUTH_FAILED, "company credentials rejected")
    if not login and code not in (None, 0, "0", 200, "200"):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "company store service failed", status=502)
    return body.get("data", body)


def login_account(login_name: str, password: str) -> dict[str, Any]:
    """Authenticate server-side. The upstream requires credentials as query parameters."""
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                LOGIN_URL,
                params={
                    "loginName": login_name,
                    "password": password,
                    "systemType": "XM_STORE_SUITE",
                },
            )
    except httpx.HTTPError:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR, "company login unavailable", status=502
        ) from None
    data = _payload(response, login=True)
    if not isinstance(data, dict) or not data.get("token") or not data.get("platformUserId"):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "incomplete company login response", status=502)
    return data


def list_authorized_stores_with_oa_id(token: str) -> list[dict[str, str]]:
    """Fetch current company store grants, including server-only BOH mapping fields."""
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(STORES_URL, headers={"token": token, "lang": "zh"}, json={})
    except httpx.HTTPError as exc:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR, "company store service unavailable", status=502
        ) from exc
    data = _payload(response, login=False)
    if not isinstance(data, list):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "invalid company store list", status=502)
    stores: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("storeId"):
            continue
        stores.append(
            {
                "store_id": str(item["storeId"]),
                "store_name": str(item.get("storeNameCN") or item.get("storeName") or ""),
                "store_no": str(item.get("storeNo") or ""),
                "store_oa_id": str(item.get("storeIdOa") or ""),
            }
        )
    return stores


def list_authorized_stores(token: str) -> list[dict[str, str]]:
    """Return only the fields used by the dashboard and existing MCP tool."""
    return [
        {key: item[key] for key in ("store_id", "store_name", "store_no")}
        for item in list_authorized_stores_with_oa_id(token)
    ]


async def authenticate_company_user(
    login_name: str, password: str, *, services: Any, user_manager: Any
) -> Any:
    """Map a company identity to an ordinary Octop user and rotate its encrypted token."""
    from octop.infra.connectors.service import ConnectorService  # noqa: PLC0415

    profile = await asyncio.to_thread(login_account, login_name, password)
    provider = services.sso_repo.upsert_by_kind(
        CONNECTOR_KIND,
        enabled=False,
        display_name="遇见小面门店账号",
        issuer="xm-store-password",
        client_id="XM_STORE_SUITE",
        client_secret_enc=None,
        scopes="",
        dashboard_origin=None,
    )
    user = await user_manager.resolve_or_create_sso_user(
        provider_id=provider.id,
        subject=str(profile["platformUserId"]),
        claims={
            "preferred_username": f"xm_{profile['platformUserId']}",
            "name": profile.get("platformUserName") or login_name,
        },
    )
    repo = services.connector_repo
    inst = repo.get_by_user_kind(user.id, CONNECTOR_KIND)
    if inst is None:
        instance_id = new_ulid()
        repo.create(
            instance_id=instance_id,
            user_id=user.id,
            kind=CONNECTOR_KIND,
            display_name="我的门店",
            mcp_server_name=mcp_server_name(CONNECTOR_KIND, instance_id),
            config_json=json.dumps({"default_open": True}),
        )
    else:
        instance_id = inst.instance_id
        if inst.status != "active":
            repo.update_status(instance_id, "active")
    connector_service = ConnectorService(
        repo=repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    existing_creds = connector_service.decrypt(instance_id)
    connector_service.encrypt_and_store(
        instance_id=instance_id,
        payload={
            "token": str(profile["token"]),
            "qw_id": str(profile.get("qwId") or ""),
            "internal_token": existing_creds.get("internal_token") or new_internal_token(),
        },
    )
    from octop.infra.boh import ensure_boh_connector_for_user  # noqa: PLC0415

    ensure_boh_connector_for_user(services, user.id)
    return user


def _token(services: Any, user_id: int) -> str | None:
    inst = services.connector_repo.get_by_user_kind(user_id, CONNECTOR_KIND)
    if inst is None or not inst.credential_blob or inst.status != "active":
        return None
    creds = decrypt_credentials(services.secret_repo, inst.credential_blob)
    return str(creds.get("token") or "") or None


def _owned_thread(services: Any, user_id: int, thread_id: str) -> Any:
    row = services.thread_repo.get(thread_id)
    if row is None or row.user_id != user_id or row.channel_type != "dashboard":
        raise OctopError(ErrorCode.FORBIDDEN, "thread not owned by user")
    return row


def _preference(services: Any, user_id: int) -> str | None:
    row = services.user_repo.get(user_id)
    raw = parse_preferences_json(row.preferences_json if row else None).get(_PREFERENCE_KEY)
    return raw if isinstance(raw, str) and raw else None


async def stores_for_user(
    services: Any, user_id: int, thread_id: str | None = None
) -> dict[str, Any]:
    thread = _owned_thread(services, user_id, thread_id) if thread_id else None
    token = _token(services, user_id)
    if token is None:
        return {"enabled": False, "stores": [], "selected_store_id": None}
    stores = await asyncio.to_thread(list_authorized_stores, token)
    allowed = {item["store_id"] for item in stores}
    selected = (
        thread.xm_store_id
        if thread and thread.xm_store_id is not None
        else _preference(services, user_id)
    )
    if selected not in allowed:
        selected = None
        if thread and thread.xm_store_id:
            services.thread_repo.set_xm_store_id(thread.thread_id, "")
    if thread and thread.xm_store_id is None and selected is not None:
        services.thread_repo.set_xm_store_id(thread.thread_id, selected)
    return {"enabled": True, "stores": stores, "selected_store_id": selected}


async def select_store_for_user(
    services: Any, user_id: int, store_id: str | None, thread_id: str | None = None
) -> None:
    thread = _owned_thread(services, user_id, thread_id) if thread_id else None
    token = _token(services, user_id)
    if token is None:
        raise OctopError(ErrorCode.FORBIDDEN, "company login required")
    if store_id is not None:
        stores = await asyncio.to_thread(list_authorized_stores, token)
        if store_id not in {item["store_id"] for item in stores}:
            raise OctopError(ErrorCode.FORBIDDEN, "store not authorized")
    if thread:
        services.thread_repo.set_xm_store_id(thread.thread_id, store_id or "")
    row = services.user_repo.get(user_id)
    prefs = parse_preferences_json(row.preferences_json if row else None)
    if store_id is None:
        prefs.pop(_PREFERENCE_KEY, None)
    else:
        prefs[_PREFERENCE_KEY] = store_id
    services.user_repo.set_preferences_json(user_id, json.dumps(prefs, ensure_ascii=False))
