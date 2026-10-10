"""Company-scoped Yuxue course detail queries using trusted chat selections."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import httpx
from langgraph.config import get_config
from pydantic import BaseModel, ConfigDict, Field

from octop.i18n import tr
from octop.infra.connectors.builder import mcp_server_name
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.qixuebao_courses import authorized_courses_for_thread
from octop.infra.utils.locale import resolve_user_locale
from octop.infra.xm_access import XmAccessError, authorized_store_for_thread, company_credentials

if TYPE_CHECKING:
    from octop.infra.db.services import RepoBundle, SharedServices

CONNECTOR_KIND = "yuxue"
TOOL_NAME = "query_course_detail_page"
COURSE_DETAILS_URL = (
    "https://meetfun-talents.yujianxiaomian.com"
    "/meetfun-talents/talents/report/qiXueBao/getCourseListByPage"
)


class CourseDetailQuery(BaseModel):
    """Model arguments can narrow results, but cannot supply authorization scope."""

    model_config = ConfigDict(extra="forbid")

    pageNum: int = Field(default=1, ge=1)
    pageSize: int = Field(default=100, ge=1, le=1000)
    courseStatus: Literal["UNPUBLISHED", "PUBLISHED", "CLOSED"] = "PUBLISHED"
    completeStatus: (
        list[Literal["LS_NOTSTART", "LS_PROGRESS", "LS_COMPLETED", "LS_INCOMPLETE"]] | None
    ) = None
    courseName: str | None = None
    staffName: str | None = None
    baseIdList: list[str] | None = None
    completeTimeStart: str | None = None
    completeTimeEnd: str | None = None
    joinTimeStart: str | None = None
    joinTimeEnd: str | None = None


def ensure_yuxue_connector_for_user(services: SharedServices, user_id: int) -> None:
    """Attach one managed connector to an existing active company login."""
    repo = services.connector_repo
    company = repo.get_by_user_kind(user_id, "xm-store")
    if company is None or company.status != "active" or not company.has_credentials:
        return
    instance = repo.get_by_user_kind(user_id, CONNECTOR_KIND)
    if instance is None:
        instance_id = f"yuxue_{company.instance_id}"
        display_name = "遇学"
        if repo.name_exists(user_id, display_name):
            display_name = f"遇学 ({company.instance_id[-6:]})"
        try:
            repo.create(
                instance_id=instance_id,
                user_id=user_id,
                kind=CONNECTOR_KIND,
                display_name=display_name,
                mcp_server_name=mcp_server_name(CONNECTOR_KIND, instance_id),
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


class YuxueQueryError(Exception):
    def __init__(self, code: str, message_key: str) -> None:
        super().__init__(code)
        self.code = code
        self.message_key = message_key


async def fetch_course_detail_page(
    client: httpx.AsyncClient, *, token: str, query: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(
            COURSE_DETAILS_URL,
            headers={"token": token, "systemtype": "TALENTS", "lang": "zh"},
            json=query,
        )
    except httpx.TimeoutException as exc:
        raise YuxueQueryError("YUXUE_TIMEOUT", "yuxue.timeout") from exc
    except httpx.HTTPError as exc:
        raise YuxueQueryError("YUXUE_UNAVAILABLE", "yuxue.unavailable") from exc
    if response.status_code == 401:
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
    if response.status_code == 403:
        raise YuxueQueryError("COURSE_FORBIDDEN", "yuxue.course_forbidden")
    if response.is_error:
        raise YuxueQueryError("YUXUE_UNAVAILABLE", "yuxue.unavailable")
    try:
        body = response.json()
    except ValueError as exc:
        raise YuxueQueryError("YUXUE_BAD_RESPONSE", "yuxue.unavailable") from exc
    if not isinstance(body, dict):
        raise YuxueQueryError("YUXUE_BAD_RESPONSE", "yuxue.unavailable")
    code = body.get("code")
    if str(code) == "401":
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
    if str(code) == "403":
        raise YuxueQueryError("COURSE_FORBIDDEN", "yuxue.course_forbidden")
    if code not in (None, 0, "0", 200, "200"):
        raise YuxueQueryError("YUXUE_UPSTREAM_ERROR", "yuxue.unavailable")
    page = body.get("data", body)
    if not isinstance(page, dict) or not isinstance(page.get("records"), list):
        raise YuxueQueryError("YUXUE_BAD_RESPONSE", "yuxue.unavailable")
    if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
        raise YuxueQueryError("YUXUE_RESULT_TOO_LARGE", "yuxue.result_too_large")
    return page


async def query_course_details(
    repos: RepoBundle,
    *,
    user_id: int,
    agent_id: str,
    thread_id: str,
    filters: CourseDetailQuery,
) -> dict[str, Any]:
    """Recheck the acting user's grants and inject the current conversation scope."""
    _thread, store = await authorized_store_for_thread(
        repos, user_id=user_id, agent_id=agent_id, thread_id=thread_id
    )
    course_ids = await authorized_courses_for_thread(
        repos, user_id=user_id, agent_id=agent_id, thread_id=thread_id
    )
    stored = company_credentials(repos, user_id)
    if stored is None:
        raise XmAccessError("COMPANY_LOGIN_REQUIRED")
    query = {
        **filters.model_dump(exclude_none=True),
        "chooseType": "STORE",
        "storeIdList": [store["store_id"]],
        "courseIdList": course_ids,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        page = await fetch_course_detail_page(client, token=str(stored[1]["token"]), query=query)
    return {"query": query, "data": page}


def build_yuxue_tools(*, mcp_server_name: str, repos: RepoBundle, agent_id: str) -> list[Any]:
    from harness_agent.mcp import sanitize_llm_tool_name
    from langchain_core.tools import StructuredTool

    async def invoke(**kwargs: Any) -> str:
        try:
            configurable = dict(get_config().get("configurable") or {})
        except RuntimeError:
            configurable = {}
        raw_user = configurable.get("user")
        user_id = (
            int(raw_user) if isinstance(raw_user, str | int) and str(raw_user).isdigit() else 0
        )
        thread_id = configurable.get("thread_id")
        locale = resolve_user_locale(
            user_repo=repos.user_repo, user_id=user_id, metadata=configurable
        )
        try:
            result = await query_course_details(
                repos,
                user_id=user_id,
                agent_id=agent_id,
                thread_id=thread_id if isinstance(thread_id, str) else "",
                filters=CourseDetailQuery.model_validate(kwargs),
            )
        except XmAccessError as exc:
            result = {
                "status": "failed",
                "error": exc.code,
                "message": tr(f"yuxue.{exc.code.lower()}", locale),
            }
        except YuxueQueryError as exc:
            result = {"status": "failed", "error": exc.code, "message": tr(exc.message_key, locale)}
        except OctopError as exc:
            message_key = {
                ErrorCode.TOKEN_EXPIRED: "yuxue.company_relogin_required",
                ErrorCode.FORBIDDEN: "yuxue.course_selection_forbidden",
            }.get(exc.code, "yuxue.unavailable")
            result = {
                "status": "failed",
                "error": exc.code.value,
                "message": tr(message_key, locale),
            }
        else:
            result = {"status": "ok", **result}
        return json.dumps(result, ensure_ascii=False)

    return [
        StructuredTool.from_function(
            coroutine=invoke,
            name=sanitize_llm_tool_name(f"{mcp_server_name}_{TOOL_NAME}"),
            description=tr("yuxue.tool_description", "zh"),
            args_schema=CourseDetailQuery,
        )
    ]
