"""User-scoped BOH SSO and read-only inventory data access."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date as date_type
from typing import TYPE_CHECKING, Any

import httpx
from langgraph.config import get_config

from octop.i18n import tr
from octop.infra.connectors.builder import mcp_server_name
from octop.infra.connectors.crypto import decrypt_credentials
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.xm_store import CONNECTOR_KIND, list_authorized_stores_with_oa_id

if TYPE_CHECKING:
    from octop.config import OctopConfig
    from octop.infra.connectors.service import ConnectorService
    from octop.infra.db.services import RepoBundle

SSO_CODE_URL = (
    "https://digital-manager-prod.yujianxiaomian.com/meet-digital-manager/sso/platformLoginCodeSave"
)
BOH_LOGIN_URL = "https://boh-manage.yujianxiaomian.com/boh-back/facade/loginByXMStoreSuiteCode"
BOH_REPORT_URL = (
    "https://boh-manage.yujianxiaomian.com/boh-back/facade/cos-report/store/cos/pageList"
)
REPORT_PERMISSION = "report:inventorySummaryReport"
_AUTH_CODES = {401, 405, 406}
_CACHE_SECONDS = 15 * 60
BOH_CONNECTOR_KIND = "boh"
TOOL_NAME = "query_store_cos_page"
_UPSTREAM_PAGE_SIZE = 500
_MAX_PAGE_INDEX = 50
_MAX_RESULT_BYTES = 256 * 1024


def _boh_store_id(value: Any) -> str | None:
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return str(int(value))
    return None


def ensure_boh_connector_for_user(services: Any, user_id: int) -> None:
    """Create the managed BOH connector for an existing company login."""
    repo = services.connector_repo
    company = repo.get_by_user_kind(user_id, CONNECTOR_KIND)
    if company is None or company.status != "active" or not company.has_credentials:
        return
    if company.shared:
        repo.update_metadata(company.instance_id, shared=False)
    instance = repo.get_by_user_kind(user_id, BOH_CONNECTOR_KIND)
    if instance is None:
        instance_id = f"boh_{company.instance_id}"
        display_name = "BOH 数据"
        if repo.name_exists(user_id, display_name):
            display_name = f"BOH 数据 ({company.instance_id[-6:]})"
        try:
            repo.create(
                instance_id=instance_id,
                user_id=user_id,
                kind=BOH_CONNECTOR_KIND,
                display_name=display_name,
                mcp_server_name=mcp_server_name(BOH_CONNECTOR_KIND, instance_id),
                config_json=json.dumps({"default_open": True}),
            )
        except Exception:
            if repo.get(instance_id) is None:
                raise
        instance = repo.get(instance_id)
    assert instance is not None
    if instance.shared:
        repo.update_metadata(instance.instance_id, shared=False)
    if instance.has_credentials:
        return
    from octop.infra.connectors.service import ConnectorService

    svc = ConnectorService(
        repo=repo,
        secret_repo=services.secret_repo,
        settings_repo=services.settings_repo,
        config=services.config,
    )
    svc.encrypt_and_store(
        instance_id=instance.instance_id,
        payload={"linked_company_connector": company.instance_id},
    )


class _RedactCodeUrl(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if BOH_LOGIN_URL in record.getMessage():
            record.msg = "HTTP Request: BOH SSO exchange [URL redacted]"
            record.args = ()
        return True


logging.getLogger("httpx._client").addFilter(_RedactCodeUrl())


class BohQueryError(Exception):
    def __init__(self, code: str, message_key: str) -> None:
        super().__init__(code)
        self.code = code
        self.message_key = message_key


class _BohTokenExpired(Exception):
    pass


def _upstream_data(response: httpx.Response, *, company: bool = False, report: bool = False) -> Any:
    if response.status_code in _AUTH_CODES:
        if company:
            raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
        raise _BohTokenExpired
    if response.is_error:
        raise BohQueryError("BOH_UNAVAILABLE", "boh.unavailable")
    try:
        body = response.json()
    except ValueError as exc:
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable") from exc
    if not isinstance(body, dict):
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
    auth_codes = {str(code) for code in _AUTH_CODES}
    if str(body.get("httpStatus")) in auth_codes or str(body.get("code")) in auth_codes:
        if company:
            raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
        raise _BohTokenExpired
    expected: tuple[int | str, ...]
    if company:
        expected = (0, "0")
    elif report:
        expected = (0, "0", 200, "200")
    else:
        expected = (200, "200")
    if body.get("code") not in expected:
        raise BohQueryError("BOH_UPSTREAM_ERROR", "boh.unavailable")
    return body.get("data")


async def create_login_code(
    client: httpx.AsyncClient, *, company_token: str, qw_id: str, store_oa_id: str
) -> str:
    try:
        response = await client.post(
            SSO_CODE_URL,
            headers={"token": company_token, "systemType": "XM_STORE_SUITE", "lang": "zh"},
            json={
                "qwId": qw_id,
                "storeOaId": store_oa_id,
                "systemBaseName": "BOH",
                "systemType": "BOH",
            },
        )
    except httpx.HTTPError as exc:
        raise BohQueryError("BOH_UNAVAILABLE", "boh.unavailable") from exc
    data = _upstream_data(response, company=True)
    code = data.get("platformLoginCode") if isinstance(data, dict) else None
    if not isinstance(code, str) or not code:
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
    return code


async def exchange_login_code(client: httpx.AsyncClient, code: str) -> dict[str, Any]:
    try:
        response = await client.post(
            BOH_LOGIN_URL,
            params={"xmLoginCode": code},
            headers={"systemType": "XM_STORE_SUITE", "Terminal-Type": "BOH_APP", "lang": "zh"},
            json={"xmLoginCode": code},
        )
    except httpx.HTTPError as exc:
        raise BohQueryError("BOH_UNAVAILABLE", "boh.unavailable") from exc
    try:
        data = _upstream_data(response)
    except _BohTokenExpired as exc:
        raise BohQueryError("BOH_LOGIN_FAILED", "boh.unavailable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
    return data


async def fetch_report_page(
    client: httpx.AsyncClient,
    *,
    token: str,
    store_id: str,
    start_date: str,
    end_date: str,
    finance_category_names: list[str],
    page_index: int,
) -> dict[str, Any]:
    try:
        response = await client.post(
            BOH_REPORT_URL,
            headers={
                "token": token,
                "systemType": "XM_STORE_SUITE",
                "Terminal-Type": "BOH_APP",
                "lang": "zh",
            },
            json={
                "startDate": start_date,
                "endDate": end_date,
                "dateType": 1,
                "financeCategoryNames": finance_category_names,
                "storeId": [store_id],
                "pageIndex": page_index,
                "pageSize": _UPSTREAM_PAGE_SIZE,
                "bzywlflSonNames": None,
                "isMatchRaw": 0,
                "orderingCategoryList": [],
            },
        )
    except httpx.HTTPError as exc:
        raise BohQueryError("BOH_UNAVAILABLE", "boh.unavailable") from exc
    data = _upstream_data(response, report=True)
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
    if type(data.get("total")) is not int or data["total"] < 0:
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
    if not all(isinstance(item, dict) for item in data["records"]):
        raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
    return data


class BohReportService:
    def __init__(
        self, *, repos: RepoBundle, config: OctopConfig, connector_service: ConnectorService
    ) -> None:
        self._repos = repos
        self._config = config
        self._connectors = connector_service
        self._locks: dict[int, asyncio.Lock] = {}

    def _credentials(self, user_id: int) -> tuple[str, dict[str, Any]]:
        inst = self._repos.connector_repo.get_by_user_kind(user_id, CONNECTOR_KIND)
        if inst is None or inst.status != "active" or not inst.credential_blob:
            raise BohQueryError("COMPANY_LOGIN_REQUIRED", "boh.company_login_required")
        creds = decrypt_credentials(self._repos.secret_repo, inst.credential_blob)
        if not creds.get("token"):
            raise BohQueryError("COMPANY_LOGIN_REQUIRED", "boh.company_login_required")
        if not creds.get("qw_id"):
            raise BohQueryError("COMPANY_RELOGIN_REQUIRED", "boh.company_relogin_required")
        return inst.instance_id, creds

    async def _session(
        self, *, user_id: int, company_store: dict[str, str], client: httpx.AsyncClient
    ) -> dict[str, Any]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            instance_id, creds = self._credentials(user_id)
            cache = creds.get("boh_sessions")
            cache = cache if isinstance(cache, dict) else {}
            selected_id = company_store["store_id"]
            cached = cache.get(selected_id)
            cached_store_id = (
                _boh_store_id(cached.get("store_id")) if isinstance(cached, dict) else None
            )
            if (
                isinstance(cached, dict)
                and cached.get("expires_at", 0) > time.time()
                and cached.get("store_no") == company_store["store_no"]
                and cached.get("store_oa_id") == company_store["store_oa_id"]
                and cached.get("token")
                and cached_store_id
            ):
                return {**cached, "store_id": cached_store_id}
            code = await create_login_code(
                client,
                company_token=str(creds["token"]),
                qw_id=str(creds["qw_id"]),
                store_oa_id=company_store["store_oa_id"],
            )
            login = await exchange_login_code(client, code)
            profile = login.get("loginSysUserVo")
            if not isinstance(profile, dict):
                raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
            permissions = profile.get("permissionCodes")
            if not isinstance(permissions, list) or REPORT_PERMISSION not in permissions:
                raise BohQueryError("BOH_REPORT_FORBIDDEN", "boh.report_forbidden")
            stores = profile.get("storeList")
            if not isinstance(stores, list):
                raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
            boh_store = next(
                (
                    item
                    for item in stores
                    if isinstance(item, dict) and str(item.get("code")) == company_store["store_no"]
                ),
                None,
            )
            boh_store_id = (
                _boh_store_id(boh_store.get("id")) if isinstance(boh_store, dict) else None
            )
            if not boh_store_id:
                raise BohQueryError("BOH_STORE_FORBIDDEN", "boh.store_forbidden")
            session = {
                "token": login["token"],
                "store_id": boh_store_id,
                "store_no": company_store["store_no"],
                "store_oa_id": company_store["store_oa_id"],
                "permission_codes": permissions,
                "expires_at": time.time() + _CACHE_SECONDS,
            }
            latest_id, latest_creds = self._credentials(user_id)
            if latest_id != instance_id or latest_creds.get("token") != creds["token"]:
                raise BohQueryError("COMPANY_RELOGIN_REQUIRED", "boh.company_relogin_required")
            latest_cache = latest_creds.get("boh_sessions")
            latest_cache = dict(latest_cache) if isinstance(latest_cache, dict) else {}
            latest_cache[selected_id] = session
            latest_creds["boh_sessions"] = latest_cache
            self._connectors.encrypt_and_store(instance_id=instance_id, payload=latest_creds)
            return session

    async def _invalidate(self, user_id: int, store_id: str, token: str) -> None:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            instance_id, creds = self._credentials(user_id)
            cache = creds.get("boh_sessions")
            if not isinstance(cache, dict):
                return
            cached = cache.get(store_id)
            if not isinstance(cached, dict) or cached.get("token") != token:
                return
            cache = dict(cache)
            cache.pop(store_id, None)
            creds["boh_sessions"] = cache
            self._connectors.encrypt_and_store(instance_id=instance_id, payload=creds)

    async def query(
        self,
        *,
        user_id: int,
        thread_id: str,
        agent_id: str,
        start_date: str,
        end_date: str,
        finance_category_names: list[str],
        page_index: int,
    ) -> dict[str, Any]:
        thread = self._repos.thread_repo.get(thread_id)
        if (
            thread is None
            or thread.user_id != user_id
            or thread.agent_id != agent_id
            or thread.channel_type != "dashboard"
        ):
            raise BohQueryError("THREAD_FORBIDDEN", "boh.thread_forbidden")
        if not thread.xm_store_id:
            raise BohQueryError("STORE_REQUIRED", "boh.store_required")
        if type(page_index) is not int or not 1 <= page_index <= _MAX_PAGE_INDEX:
            raise BohQueryError("INVALID_PAGE", "boh.invalid_page")
        if (
            not isinstance(finance_category_names, list)
            or len(finance_category_names) > 20
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 80
                for item in finance_category_names
            )
        ):
            raise BohQueryError("INVALID_CATEGORY", "boh.invalid_category")
        try:
            start = date_type.fromisoformat(start_date)
            end = date_type.fromisoformat(end_date)
            if start.isoformat() != start_date or end.isoformat() != end_date:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise BohQueryError("INVALID_DATE", "boh.invalid_date") from exc
        if start > end or (end - start).days > 62:
            raise BohQueryError("INVALID_DATE_RANGE", "boh.invalid_date_range")
        _instance_id, creds = self._credentials(user_id)
        stores = await asyncio.to_thread(list_authorized_stores_with_oa_id, str(creds["token"]))
        company_store = next(
            (item for item in stores if item["store_id"] == thread.xm_store_id), None
        )
        if company_store is None:
            raise BohQueryError("STORE_FORBIDDEN", "boh.store_forbidden")
        if not company_store["store_oa_id"] or not company_store["store_no"]:
            raise BohQueryError("STORE_MAPPING_MISSING", "boh.store_mapping_missing")
        try:
            async with asyncio.timeout(30), httpx.AsyncClient(timeout=10.0) as client:
                session = await self._session(
                    user_id=user_id, company_store=company_store, client=client
                )
                try:
                    data = await fetch_report_page(
                        client,
                        token=str(session["token"]),
                        store_id=str(session["store_id"]),
                        start_date=start_date,
                        end_date=end_date,
                        finance_category_names=finance_category_names,
                        page_index=page_index,
                    )
                except _BohTokenExpired:
                    await self._invalidate(user_id, company_store["store_id"], session["token"])
                    session = await self._session(
                        user_id=user_id, company_store=company_store, client=client
                    )
                    try:
                        data = await fetch_report_page(
                            client,
                            token=str(session["token"]),
                            store_id=str(session["store_id"]),
                            start_date=start_date,
                            end_date=end_date,
                            finance_category_names=finance_category_names,
                            page_index=page_index,
                        )
                    except _BohTokenExpired as exc:
                        raise BohQueryError("BOH_LOGIN_FAILED", "boh.unavailable") from exc
        except TimeoutError as exc:
            raise BohQueryError("BOH_TIMEOUT", "boh.timeout") from exc
        result = {
            "query": {
                "startDate": start_date,
                "endDate": end_date,
                "dateType": 1,
                "financeCategoryNames": finance_category_names,
                "pageIndex": page_index,
                "pageSize": _UPSTREAM_PAGE_SIZE,
                "storeId": [str(session["store_id"])],
                "storeName": company_store["store_name"],
                "storeNo": company_store["store_no"],
            },
            "data": data,
        }
        if (
            len(json.dumps({"status": "ok", **result}, ensure_ascii=False).encode("utf-8"))
            > _MAX_RESULT_BYTES
        ):
            raise BohQueryError("BOH_RESULT_TOO_LARGE", "boh.result_too_large")
        return result


def build_boh_report_tool(
    *,
    mcp_server_name: str,
    repos: RepoBundle,
    config: OctopConfig,
    connector_service: ConnectorService,
    agent_id: str,
) -> Any:
    """Expose only report filters; derive acting identity and selected store at invocation."""
    from harness_agent.mcp import sanitize_llm_tool_name
    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    from octop.infra.connectors.gateway.adapters.boh import list_tools

    service = BohReportService(repos=repos, config=config, connector_service=connector_service)
    definition = list_tools()[0]
    name = sanitize_llm_tool_name(f"{mcp_server_name}_{TOOL_NAME}")
    args_schema = create_model(
        f"{name}_args",
        startDate=(str, Field(description="统计开始日期，YYYY-MM-DD")),
        endDate=(str, Field(description="统计结束日期，YYYY-MM-DD")),
        financeCategoryNames=(list[str], Field(description="财务分类；空数组表示全部")),
        pageIndex=(int, Field(default=1, ge=1, le=_MAX_PAGE_INDEX)),
    )

    async def _query(
        startDate: str,
        endDate: str,
        financeCategoryNames: list[str],
        pageIndex: int = 1,
    ) -> str:
        try:
            configurable = dict(get_config().get("configurable") or {})
        except RuntimeError:
            configurable = {}
        raw_user = configurable.get("user")
        user_id = (
            int(raw_user) if isinstance(raw_user, str | int) and str(raw_user).isdigit() else 0
        )
        thread_id = configurable.get("thread_id")
        try:
            result = await service.query(
                user_id=user_id,
                thread_id=thread_id if isinstance(thread_id, str) else "",
                agent_id=agent_id,
                start_date=startDate,
                end_date=endDate,
                finance_category_names=financeCategoryNames,
                page_index=pageIndex,
            )
        except BohQueryError as exc:
            return json.dumps(
                {"status": "failed", "error": exc.code, "message": tr(exc.message_key, "zh")},
                ensure_ascii=False,
            )
        except OctopError as exc:
            message_key = (
                "boh.company_relogin_required"
                if exc.code == ErrorCode.TOKEN_EXPIRED
                else "boh.unavailable"
            )
            return json.dumps(
                {"status": "failed", "error": exc.code.value, "message": tr(message_key, "zh")},
                ensure_ascii=False,
            )
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)

    return StructuredTool.from_function(
        coroutine=_query,
        name=name,
        description=str(definition["description"]),
        args_schema=args_schema,
    )
