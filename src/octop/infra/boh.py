"""User-scoped BOH SSO and read-only inventory data access."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import InvalidToken
from langgraph.config import get_config

from octop.i18n import tr
from octop.infra.connectors.builder import mcp_server_name
from octop.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.ulid import new_ulid
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
REPORT_SPECS: dict[str, dict[str, Any]] = {
    "cos": {
        "url": BOH_REPORT_URL,
        "permission": REPORT_PERMISSION,
        "page_size": 500,
    },
    "generic": {
        "url": "https://boh-manage.yujianxiaomian.com/boh-back/cos-report/store/cos/genericRawItem/pageList",
        "permission": "report:standardRawMaterialVariance",
        "page_size": 100,
    },
    "assessment_week": {
        "url": "https://boh-manage.yujianxiaomian.com/boh-back/cos-report/store/cos/assessmentItem/week/pageList",
        "permission": "report:assessmentItemVarianceWeek",
        "page_size": 50,
    },
    "generic_week": {
        "url": "https://boh-manage.yujianxiaomian.com/boh-back/cos-report/store/cos/genericRawItem/week/pageList",
        "permission": "report:standardRawMaterialVarianceWeek",
        "page_size": 50,
    },
}
BOH_RAW_ITEM_URL = "https://boh-manage.yujianxiaomian.com/boh-back/facade/rawItem/getPageList"
_AUTH_CODES = {401, 405, 406}
_CACHE_SECONDS = 15 * 60
BOH_CONNECTOR_KIND = "boh"
TOOL_NAME = "query_store_cos_page"
_UPSTREAM_PAGE_SIZE = 500
_MAX_PAGE_INDEX = 50
_MAX_RESULT_BYTES = 256 * 1024
_MAX_RAW_ITEM_IDS = 50


def _boh_store_id(value: Any) -> str | None:
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return str(int(value))
    return None


def default_analysis_period(timezone: str, *, today: date_type | None = None) -> dict[str, str]:
    current = today or datetime.now(ZoneInfo(timezone)).date()
    end = current - timedelta(days=1)
    start = end.replace(day=1) if current.day == 1 else current.replace(day=1)
    return {"startDate": start.isoformat(), "endDate": end.isoformat(), "timezone": timezone}


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
    page_size: int = _UPSTREAM_PAGE_SIZE,
    raw_item_ids: list[int] | None = None,
) -> dict[str, Any]:
    return await fetch_boh_page(
        client,
        kind="cos",
        token=token,
        store_id=store_id,
        start_date=start_date,
        end_date=end_date,
        finance_category_names=finance_category_names,
        page_index=page_index,
        page_size=page_size,
        raw_item_ids=raw_item_ids,
    )


async def fetch_boh_page(
    client: httpx.AsyncClient,
    *,
    kind: str,
    token: str,
    store_id: str,
    start_date: str,
    end_date: str,
    finance_category_names: list[str] | None,
    page_index: int,
    page_size: int,
    raw_item_ids: list[int] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "startDate": start_date,
        "endDate": end_date,
        "pageIndex": page_index,
        "pageSize": page_size,
        "storeId": [store_id],
        "isMatchRaw": 0,
    }
    if kind in {"cos", "generic"}:
        body.update(
            dateType=1,
            financeCategoryNames=finance_category_names,
            bzywlflSonNames=None,
        )
        if kind == "cos":
            body["orderingCategoryList"] = []
            if raw_item_ids is not None:
                body["riIds"] = raw_item_ids
    else:
        body.update(count=True, login=True, isShowSpecialColumn=True)
        if kind == "generic_week":
            body.update(bzywlflSonNames=None, excludeCondiment=True)
    try:
        response = await client.post(
            str(REPORT_SPECS[kind]["url"]),
            headers={
                "token": token,
                "systemType": "XM_STORE_SUITE",
                "Terminal-Type": "BOH_APP",
                "lang": "zh",
            },
            json=body,
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
    if kind.endswith("_week") and not isinstance(data.get("headers"), list):
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
            if not isinstance(permissions, list):
                raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
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

    async def search_materials(
        self, *, user_id: int, thread_id: str, agent_id: str, keyword: str
    ) -> dict[str, Any]:
        if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 80:
            raise BohQueryError("INVALID_MATERIAL", "boh.invalid_material")
        thread = self._repos.thread_repo.get(thread_id)
        if (
            thread is None
            or thread.user_id != user_id
            or thread.agent_id != agent_id
            or thread.channel_type != "dashboard"
            or not thread.xm_store_id
        ):
            raise BohQueryError("THREAD_FORBIDDEN", "boh.thread_forbidden")
        _instance_id, creds = self._credentials(user_id)
        stores = await asyncio.to_thread(list_authorized_stores_with_oa_id, str(creds["token"]))
        company_store = next((x for x in stores if x["store_id"] == thread.xm_store_id), None)
        if company_store is None:
            raise BohQueryError("STORE_FORBIDDEN", "boh.store_forbidden")
        async with httpx.AsyncClient(timeout=15.0) as client:
            session = await self._session(
                user_id=user_id, company_store=company_store, client=client
            )
            if REPORT_PERMISSION not in session["permission_codes"]:
                raise BohQueryError("BOH_REPORT_FORBIDDEN", "boh.report_forbidden")

            async def fetch() -> Any:
                try:
                    response = await client.post(
                        BOH_RAW_ITEM_URL,
                        headers={
                            "token": str(session["token"]),
                            "systemType": "XM_STORE_SUITE",
                            "Terminal-Type": "BOH_APP",
                            "lang": "zh",
                        },
                        json={"pageIndex": 1, "pageSize": 10000, "type": 1},
                    )
                except httpx.HTTPError as exc:
                    raise BohQueryError("BOH_UNAVAILABLE", "boh.unavailable") from exc
                return _upstream_data(response, report=True)

            try:
                data = await fetch()
            except _BohTokenExpired as exc:
                await self._invalidate(user_id, company_store["store_id"], session["token"])
                session = await self._session(
                    user_id=user_id, company_store=company_store, client=client
                )
                if REPORT_PERMISSION not in session["permission_codes"]:
                    raise BohQueryError("BOH_REPORT_FORBIDDEN", "boh.report_forbidden") from exc
                try:
                    data = await fetch()
                except _BohTokenExpired as exc:
                    raise BohQueryError("BOH_LOGIN_FAILED", "boh.unavailable") from exc
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable")
        if type(data.get("total")) is not int or data["total"] > len(data["records"]):
            raise BohQueryError("BOH_INCOMPLETE_RESULT", "boh.incomplete_result")
        needle = keyword.strip().casefold()
        matches = [
            item
            for item in data["records"]
            if isinstance(item, dict)
            and any(
                needle in str(item.get(key) or "").casefold() for key in ("code", "name", "griName")
            )
        ]
        result = {
            "keyword": keyword.strip(),
            "totalMatches": len(matches),
            "records": matches[:50],
            "hasMore": len(matches) > 50,
        }
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise BohQueryError("BOH_RESULT_TOO_LARGE", "boh.result_too_large")
        return result

    async def query_daily(
        self,
        *,
        user_id: int,
        thread_id: str,
        agent_id: str,
        start_date: str,
        end_date: str,
        finance_category_names: list[str],
        raw_item_ids: list[int],
    ) -> dict[str, Any]:
        try:
            start = date_type.fromisoformat(start_date)
            end = date_type.fromisoformat(end_date)
        except (TypeError, ValueError) as exc:
            raise BohQueryError("INVALID_DATE", "boh.invalid_date") from exc
        if start > end or (end - start).days > 30:
            raise BohQueryError("INVALID_DATE_RANGE", "boh.invalid_date_range")
        dates = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
        semaphore = asyncio.Semaphore(4)

        async def one_day(day: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                first = await self.query(
                    user_id=user_id,
                    thread_id=thread_id,
                    agent_id=agent_id,
                    start_date=day,
                    end_date=day,
                    finance_category_names=finance_category_names,
                    page_index=1,
                    raw_item_ids=raw_item_ids,
                    page_size=50,
                )
                pages = [first["data"]]
                total = int(first["data"]["total"])
                for page_index in range(2, (total + 49) // 50 + 1):
                    following = await self.query(
                        user_id=user_id,
                        thread_id=thread_id,
                        agent_id=agent_id,
                        start_date=day,
                        end_date=day,
                        finance_category_names=finance_category_names,
                        page_index=page_index,
                        raw_item_ids=raw_item_ids,
                        page_size=50,
                        dataset_id=first["datasetId"],
                    )
                    pages.append(following["data"])
                records = [record for page in pages for record in page["records"]]
                if len(records) != total:
                    raise BohQueryError("BOH_INCOMPLETE_RESULT", "boh.incomplete_result")
                return day, {"total": total, "records": records, "datasetId": first["datasetId"]}

        days = dict(await asyncio.gather(*(one_day(day) for day in dates)))
        result = {"startDate": start_date, "endDate": end_date, "riIds": raw_item_ids, "days": days}
        if (
            len(json.dumps({"status": "ok", **result}, ensure_ascii=False).encode("utf-8"))
            > _MAX_RESULT_BYTES
        ):
            raise BohQueryError("BOH_RESULT_TOO_LARGE", "boh.result_too_large")
        return result

    async def analyze(
        self,
        *,
        user_id: int,
        thread_id: str,
        agent_id: str,
        dataset_ids: list[str],
        mode: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        from octop.infra.boh_analysis import daily_report, flat_report, weekly_report

        if (
            mode not in {"overview", "daily"}
            or not isinstance(dataset_ids, list)
            or not 1 <= len(dataset_ids) <= 35
            or any(not isinstance(item, str) or not 1 <= len(item) <= 64 for item in dataset_ids)
            or len(set(dataset_ids)) != len(dataset_ids)
        ):
            raise BohQueryError("INVALID_DATASET", "boh.unavailable")
        thread = self._repos.thread_repo.get(thread_id)
        if (
            thread is None
            or thread.user_id != user_id
            or thread.agent_id != agent_id
            or thread.channel_type != "dashboard"
            or not thread.xm_store_id
        ):
            raise BohQueryError("THREAD_FORBIDDEN", "boh.thread_forbidden")
        _instance_id, creds = self._credentials(user_id)
        stores = await asyncio.to_thread(list_authorized_stores_with_oa_id, str(creds["token"]))
        company_store = next((x for x in stores if x["store_id"] == thread.xm_store_id), None)
        if company_store is None:
            raise BohQueryError("STORE_FORBIDDEN", "boh.store_forbidden")
        async with httpx.AsyncClient(timeout=10.0) as client:
            session = await self._session(
                user_id=user_id, company_store=company_store, client=client
            )
        reports: dict[str, dict[str, Any]] = {}
        daily: dict[str, list[dict[str, Any]]] = {}
        daily_ids: list[int] | None = None
        for dataset_id in dataset_ids:
            try:
                kind, query, encrypted_pages = self._repos.boh_snapshot_repo.pages(
                    dataset_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    thread_id=thread_id,
                    store_id=thread.xm_store_id,
                )
            except ValueError as exc:
                raise BohQueryError("SNAPSHOT_MISMATCH", "boh.snapshot_mismatch") from exc
            pages = []
            for index, stored_total, payload in encrypted_pages:
                try:
                    data = decrypt_credentials(self._repos.secret_repo, payload).get("data")
                except (InvalidToken, ValueError, UnicodeError) as exc:
                    raise BohQueryError("SNAPSHOT_MISMATCH", "boh.snapshot_mismatch") from exc
                if not isinstance(data, dict) or data.get("total") != stored_total:
                    raise BohQueryError("SNAPSHOT_MISMATCH", "boh.snapshot_mismatch")
                pages.append((index, data))
            if (
                kind not in REPORT_SPECS
                or REPORT_SPECS[kind]["permission"] not in session["permission_codes"]
            ):
                raise BohQueryError("BOH_REPORT_FORBIDDEN", "boh.report_forbidden")
            total = pages[0][1]["total"]
            size = query["pageSize"]
            expected = max(1, (total + size - 1) // size)
            if [index for index, _ in pages] != list(range(1, expected + 1)) or any(
                page["total"] != total for _, page in pages
            ):
                raise BohQueryError("BOH_INCOMPLETE_RESULT", "boh.incomplete_result")
            records = [record for _, page in pages for record in page["records"]]
            if len(records) != total:
                raise BohQueryError("BOH_INCOMPLETE_RESULT", "boh.incomplete_result")
            if mode == "overview":
                if (
                    query["startDate"] != start_date
                    or query["endDate"] != end_date
                    or kind in reports
                ):
                    raise BohQueryError("SNAPSHOT_MISMATCH", "boh.snapshot_mismatch")
                try:
                    reports[kind] = (
                        flat_report(kind, records)
                        if kind in {"cos", "generic"}
                        else weekly_report(records)
                    )
                except ValueError as exc:
                    raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable") from exc
                reports[kind]["financeCategoryNames"] = query["financeCategoryNames"]
                reports[kind]["financeCategoryFiltered"] = bool(query["financeCategoryNames"])
            else:
                day = query["startDate"]
                if (
                    kind != "cos"
                    or day != query["endDate"]
                    or day in daily
                    or not start_date <= day <= end_date
                    or not query.get("riIds")
                    or (daily_ids is not None and daily_ids != query["riIds"])
                ):
                    raise BohQueryError("SNAPSHOT_MISMATCH", "boh.snapshot_mismatch")
                daily_ids = query["riIds"]
                daily[day] = records
        if mode == "daily":
            try:
                start = date_type.fromisoformat(start_date)
                end = date_type.fromisoformat(end_date)
            except (TypeError, ValueError) as exc:
                raise BohQueryError("INVALID_DATE", "boh.invalid_date") from exc
            if start > end or (end - start).days > 30:
                raise BohQueryError("INVALID_DATE_RANGE", "boh.invalid_date_range")
            expected_days = {
                (start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)
            }
            if set(daily) != expected_days:
                raise BohQueryError("BOH_INCOMPLETE_RESULT", "boh.incomplete_result")
            try:
                daily_metrics = daily_report(daily)
            except ValueError as exc:
                raise BohQueryError("BOH_BAD_RESPONSE", "boh.unavailable") from exc
            return {
                "mode": mode,
                "storeName": company_store["store_name"],
                "startDate": start_date,
                "endDate": end_date,
                "riIds": daily_ids,
                **daily_metrics,
            }
        return {
            "mode": mode,
            "storeName": company_store["store_name"],
            "startDate": start_date,
            "endDate": end_date,
            "reports": reports,
            "unavailableReports": sorted(
                kind
                for kind, spec in REPORT_SPECS.items()
                if spec["permission"] not in session["permission_codes"]
            ),
            "notQueriedReports": sorted(
                kind
                for kind, spec in REPORT_SPECS.items()
                if spec["permission"] in session["permission_codes"] and kind not in reports
            ),
        }

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
        report_kind: str = "cos",
        page_size: int | None = None,
        raw_item_ids: list[int] | None = None,
        dataset_id: str | None = None,
    ) -> dict[str, Any]:
        if report_kind not in REPORT_SPECS:
            raise BohQueryError("INVALID_REPORT", "boh.unavailable")
        if dataset_id is not None and (
            not isinstance(dataset_id, str) or not 1 <= len(dataset_id) <= 64
        ):
            raise BohQueryError("INVALID_DATASET", "boh.unavailable")
        size = page_size if page_size is not None else int(REPORT_SPECS[report_kind]["page_size"])
        if type(size) is not int or not 1 <= size <= int(REPORT_SPECS[report_kind]["page_size"]):
            raise BohQueryError("INVALID_PAGE", "boh.invalid_page")
        if raw_item_ids is not None and (
            report_kind != "cos"
            or not isinstance(raw_item_ids, list)
            or not 1 <= len(raw_item_ids) <= _MAX_RAW_ITEM_IDS
            or any(type(item) is not int or item <= 0 for item in raw_item_ids)
        ):
            raise BohQueryError("INVALID_MATERIAL", "boh.invalid_material")
        if report_kind.endswith("_week") and finance_category_names:
            raise BohQueryError("INVALID_CATEGORY", "boh.invalid_category")
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
                if REPORT_SPECS[report_kind]["permission"] not in session["permission_codes"]:
                    raise BohQueryError("BOH_REPORT_FORBIDDEN", "boh.report_forbidden")

                async def request_page() -> dict[str, Any]:
                    if report_kind == "cos":
                        return await fetch_report_page(
                            client,
                            token=str(session["token"]),
                            store_id=str(session["store_id"]),
                            start_date=start_date,
                            end_date=end_date,
                            finance_category_names=finance_category_names,
                            page_index=page_index,
                            page_size=size,
                            raw_item_ids=raw_item_ids,
                        )
                    return await fetch_boh_page(
                        client,
                        kind=report_kind,
                        token=str(session["token"]),
                        store_id=str(session["store_id"]),
                        start_date=start_date,
                        end_date=end_date,
                        finance_category_names=finance_category_names,
                        page_index=page_index,
                        page_size=size,
                    )

                try:
                    data = await request_page()
                except _BohTokenExpired as exc:
                    await self._invalidate(user_id, company_store["store_id"], session["token"])
                    session = await self._session(
                        user_id=user_id, company_store=company_store, client=client
                    )
                    if REPORT_SPECS[report_kind]["permission"] not in session["permission_codes"]:
                        raise BohQueryError("BOH_REPORT_FORBIDDEN", "boh.report_forbidden") from exc
                    try:
                        data = await request_page()
                    except _BohTokenExpired as exc:
                        raise BohQueryError("BOH_LOGIN_FAILED", "boh.unavailable") from exc
        except TimeoutError as exc:
            raise BohQueryError("BOH_TIMEOUT", "boh.timeout") from exc
        if any(
            row.get("storeId") is not None
            and _boh_store_id(row["storeId"]) != str(session["store_id"])
            for row in data["records"]
        ):
            raise BohQueryError("BOH_STORE_MISMATCH", "boh.store_forbidden")
        result: dict[str, Any] = {
            "query": {
                "startDate": start_date,
                "endDate": end_date,
                "pageIndex": page_index,
                "pageSize": size,
                "storeId": [str(session["store_id"])],
                "storeName": company_store["store_name"],
                "storeNo": company_store["store_no"],
                "reportKind": report_kind,
            },
            "data": data,
        }
        if report_kind in {"cos", "generic"}:
            result["query"]["dateType"] = 1
            result["query"]["financeCategoryNames"] = finance_category_names
        if raw_item_ids is not None:
            result["query"]["riIds"] = raw_item_ids
        snapshot_id = dataset_id or new_ulid()
        result["datasetId"] = snapshot_id
        if (
            len(json.dumps({"status": "ok", **result}, ensure_ascii=False).encode("utf-8"))
            > _MAX_RESULT_BYTES
        ):
            raise BohQueryError("BOH_RESULT_TOO_LARGE", "boh.result_too_large")
        snapshot_query = {
            "startDate": start_date,
            "endDate": end_date,
            "financeCategoryNames": finance_category_names
            if report_kind in {"cos", "generic"}
            else None,
            "pageSize": size,
            "riIds": raw_item_ids,
        }
        try:
            self._repos.boh_snapshot_repo.put(
                snapshot_id=snapshot_id,
                page_index=page_index,
                user_id=user_id,
                agent_id=agent_id,
                thread_id=thread_id,
                store_id=thread.xm_store_id,
                report_kind=report_kind,
                query=snapshot_query,
                total=int(data["total"]),
                payload=encrypt_credentials(self._repos.secret_repo, {"data": data}),
            )
        except ValueError as exc:
            raise BohQueryError("SNAPSHOT_MISMATCH", "boh.snapshot_mismatch") from exc
        return result


def build_boh_report_tool(
    *,
    mcp_server_name: str,
    repos: RepoBundle,
    config: OctopConfig,
    connector_service: ConnectorService,
    agent_id: str,
) -> Any:
    """Compatibility entry point for the inventory-summary tool."""
    return build_boh_report_tools(
        mcp_server_name=mcp_server_name,
        repos=repos,
        config=config,
        connector_service=connector_service,
        agent_id=agent_id,
    )[0]


def build_boh_report_tools(
    *,
    mcp_server_name: str,
    repos: RepoBundle,
    config: OctopConfig,
    connector_service: ConnectorService,
    agent_id: str,
) -> list[Any]:
    """Build BOH raw-data tools and a separate trusted analysis tool."""
    from harness_agent.mcp import sanitize_llm_tool_name
    from langchain_core.tools import StructuredTool
    from pydantic import Field, create_model

    from octop.infra.connectors.gateway.adapters.boh import list_tools

    service = BohReportService(repos=repos, config=config, connector_service=connector_service)

    def identity() -> tuple[int, str]:
        try:
            configurable = dict(get_config().get("configurable") or {})
        except RuntimeError:
            configurable = {}
        raw_user = configurable.get("user")
        user_id = (
            int(raw_user) if isinstance(raw_user, str | int) and str(raw_user).isdigit() else 0
        )
        thread_id = configurable.get("thread_id")
        return user_id, thread_id if isinstance(thread_id, str) else ""

    async def run(name: str, args: dict[str, Any]) -> str:
        user_id, thread_id = identity()
        try:
            if name == "search_raw_items":
                result = await service.search_materials(
                    user_id=user_id,
                    thread_id=thread_id,
                    agent_id=agent_id,
                    keyword=args["keyword"],
                )
            elif name == "query_store_cos_daily":
                result = await service.query_daily(
                    user_id=user_id,
                    thread_id=thread_id,
                    agent_id=agent_id,
                    start_date=args["startDate"],
                    end_date=args["endDate"],
                    finance_category_names=args["financeCategoryNames"],
                    raw_item_ids=args["rawItemIds"],
                )
            elif name == "analyze_boh_variance":
                result = await service.analyze(
                    user_id=user_id,
                    thread_id=thread_id,
                    agent_id=agent_id,
                    dataset_ids=args["datasetIds"],
                    mode=args["mode"],
                    start_date=args["startDate"],
                    end_date=args["endDate"],
                )
            else:
                kind = {
                    "query_store_cos_page": "cos",
                    "query_store_generic_page": "generic",
                    "query_store_assessment_week_page": "assessment_week",
                    "query_store_generic_week_page": "generic_week",
                }[name]
                result = await service.query(
                    user_id=user_id,
                    thread_id=thread_id,
                    agent_id=agent_id,
                    start_date=args["startDate"],
                    end_date=args["endDate"],
                    finance_category_names=(
                        args["financeCategoryNames"]
                        if args.get("financeCategoryNames") is not None
                        else (["食材成本"] if kind in {"cos", "generic"} else [])
                    ),
                    page_index=args.get("pageIndex", 1),
                    page_size=args.get("pageSize"),
                    report_kind=kind,
                    raw_item_ids=args.get("rawItemIds"),
                    dataset_id=args.get("datasetId"),
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

    tools: list[Any] = []
    for definition in list_tools():
        short_name = str(definition["name"])
        name = sanitize_llm_tool_name(f"{mcp_server_name}_{short_name}")
        fields: dict[str, Any] = {}
        schema = definition["inputSchema"]
        for field_name, prop in schema["properties"].items():
            annotation: Any = str
            if prop["type"] == "integer":
                annotation = int
            elif prop["type"] == "array":
                annotation = list[int] if prop["items"]["type"] == "integer" else list[str]
            required = field_name in schema["required"]
            field_options: dict[str, Any] = {"description": prop.get("description", "")}
            if "minimum" in prop:
                field_options["ge"] = prop["minimum"]
            if "maximum" in prop:
                field_options["le"] = prop["maximum"]
            fields[field_name] = (
                annotation if required else annotation | None,
                Field(... if required else prop.get("default"), **field_options),
            )

        async def invoke(_name: str = short_name, **kwargs: Any) -> str:
            return await run(_name, kwargs)

        tools.append(
            StructuredTool.from_function(
                coroutine=invoke,
                name=name,
                description=str(definition["description"]),
                args_schema=create_model(f"{name}_args", **fields),
            )
        )

    analysis_name = sanitize_llm_tool_name(f"{mcp_server_name}_analyze_boh_variance")

    async def analyze_boh_variance(
        datasetIds: list[str], mode: str, startDate: str, endDate: str
    ) -> str:
        return await run(
            "analyze_boh_variance",
            {
                "datasetIds": datasetIds,
                "mode": mode,
                "startDate": startDate,
                "endDate": endDate,
            },
        )

    tools.append(
        StructuredTool.from_function(
            coroutine=analyze_boh_variance,
            name=analysis_name,
            description=tr("boh.analysis_description", "zh"),
            args_schema=create_model(
                f"{analysis_name}_args",
                datasetIds=(
                    list[str],
                    Field(description="要分析的完整数据集句柄；仅传当前会话工具返回的 datasetId"),
                ),
                mode=(str, Field(description="overview：报表概览；daily：已取齐的单物料逐日异常")),
                startDate=(str, Field(description="分析开始日期 YYYY-MM-DD")),
                endDate=(str, Field(description="分析结束日期 YYYY-MM-DD")),
            ),
        )
    )

    async def get_boh_default_period() -> str:
        return json.dumps(default_analysis_period(config.default_timezone), ensure_ascii=False)

    tools.append(
        StructuredTool.from_function(
            coroutine=get_boh_default_period,
            name=sanitize_llm_tool_name(f"{mcp_server_name}_get_boh_default_period"),
            description=tr("boh.period_description", "zh"),
        )
    )
    return tools
