"""Authorized QiXueBao courses and thread-scoped course selections."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from octop.infra.chat_selectors import QIXUEBAO_COURSE, selector_enabled
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.xm_access import company_credentials

COURSES_URL = (
    "https://meetfun-talents.yujianxiaomian.com/meetfun-talents/talents/qiXueBaoCourse/queryPage"
)
PAGE_SIZE = 1000


def selected_course_ids(raw: str | None) -> list[str]:
    try:
        context = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return []
    ids = context.get(QIXUEBAO_COURSE) if isinstance(context, dict) else None
    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []


def _course_token(services: Any, user_id: int) -> str:
    stored = company_credentials(services, user_id)
    if stored is None:
        raise OctopError(ErrorCode.FORBIDDEN, "company login required")
    return str(stored[1]["token"])


def _owned_course_thread(services: Any, user_id: int, agent_id: str, thread_id: str) -> Any:
    thread = services.thread_repo.get(thread_id)
    if (
        thread is None
        or thread.user_id != user_id
        or thread.agent_id != agent_id
        or thread.channel_type != "dashboard"
    ):
        raise OctopError(ErrorCode.FORBIDDEN, "thread not owned by user and expert")
    return thread


def _assert_selector_enabled(services: Any, agent_id: str) -> None:
    agent = services.agent_repo.get(agent_id)
    if agent is None or not selector_enabled(agent.chat_selectors, QIXUEBAO_COURSE):
        raise OctopError(ErrorCode.FORBIDDEN, "course selector is not enabled")


def query_authorized_courses(token: str, *, keyword: str = "", page_num: int = 1) -> dict[str, Any]:
    """Fetch one page; the upstream uses the same company token as stores."""
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                COURSES_URL,
                headers={"token": token, "lang": "zh", "systemtype": "TALENTS"},
                json={
                    "isEnable": True,
                    "keyword": keyword,
                    "pageNum": page_num,
                    "pageSize": PAGE_SIZE,
                },
            )
    except httpx.HTTPError as exc:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR, "course service unavailable", status=502
        ) from exc
    if response.status_code == 401:
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
    if response.status_code == 403:
        raise OctopError(ErrorCode.FORBIDDEN, "course access forbidden")
    if response.is_error:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "course service unavailable", status=502)
    try:
        body = response.json()
    except ValueError as exc:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "invalid course response", status=502) from exc
    if not isinstance(body, dict):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "invalid course response", status=502)
    if body.get("code") not in (None, 0, "0", 200, "200"):
        if str(body.get("code")) == "401":
            raise OctopError(ErrorCode.TOKEN_EXPIRED, "company session expired")
        raise OctopError(ErrorCode.INTERNAL_ERROR, "course service failed", status=502)
    page = body.get("data", body)
    if not isinstance(page, dict) or not isinstance(page.get("records"), list):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "invalid course response", status=502)
    courses = [
        {"course_id": str(item["courseId"]), "course_name": str(item.get("courseName") or "")}
        for item in page["records"]
        if isinstance(item, dict) and item.get("courseId") and item.get("isEnable") is not False
    ]
    return {
        "courses": courses,
        "page_num": page_num,
        "page_size": PAGE_SIZE,
        "pages": max(1, int(page.get("pages") or 1)),
        "total": int(page.get("total") or len(courses)),
    }


async def _authorized_courses(token: str) -> dict[str, str]:
    first = await asyncio.to_thread(query_authorized_courses, token)
    allowed = {item["course_id"]: item["course_name"] for item in first["courses"]}
    for page_num in range(2, first["pages"] + 1):
        page = await asyncio.to_thread(query_authorized_courses, token, page_num=page_num)
        allowed.update({item["course_id"]: item["course_name"] for item in page["courses"]})
    return allowed


async def courses_for_user(
    services: Any,
    user_id: int,
    agent_id: str,
    *,
    thread_id: str | None = None,
    keyword: str = "",
    page_num: int = 1,
) -> dict[str, Any]:
    _assert_selector_enabled(services, agent_id)
    thread = _owned_course_thread(services, user_id, agent_id, thread_id) if thread_id else None
    token = _course_token(services, user_id)
    page = await asyncio.to_thread(
        query_authorized_courses, token, keyword=keyword, page_num=page_num
    )
    selected = selected_course_ids(thread.chat_context_json) if thread else []
    selected_courses: list[dict[str, str]] = []
    if selected:
        allowed = await _authorized_courses(token)
        current = [course_id for course_id in selected if course_id in allowed]
        if current != selected and thread:
            services.thread_repo.set_chat_context_ids(thread.thread_id, QIXUEBAO_COURSE, current)
        selected = current
        selected_courses = [
            {"course_id": course_id, "course_name": allowed[course_id]} for course_id in current
        ]
    return {**page, "selected_course_ids": selected, "selected_courses": selected_courses}


async def select_courses_for_user(
    services: Any, user_id: int, agent_id: str, thread_id: str, course_ids: list[str]
) -> list[str]:
    _assert_selector_enabled(services, agent_id)
    _owned_course_thread(services, user_id, agent_id, thread_id)
    token = _course_token(services, user_id)
    normalized = list(
        dict.fromkeys(course_id.strip() for course_id in course_ids if course_id.strip())
    )
    if normalized:
        allowed = await _authorized_courses(token)
        if any(course_id not in allowed for course_id in normalized):
            raise OctopError(ErrorCode.FORBIDDEN, "course not authorized")
    services.thread_repo.set_chat_context_ids(thread_id, QIXUEBAO_COURSE, normalized)
    return normalized


async def authorized_courses_for_thread(
    services: Any, *, user_id: int, agent_id: str, thread_id: str
) -> list[str]:
    """Trusted course scope for future course tools. Empty means all authorized courses."""
    _assert_selector_enabled(services, agent_id)
    thread = _owned_course_thread(services, user_id, agent_id, thread_id)
    token = _course_token(services, user_id)
    selected = selected_course_ids(thread.chat_context_json)
    if selected:
        allowed = await _authorized_courses(token)
        if any(course_id not in allowed for course_id in selected):
            raise OctopError(ErrorCode.FORBIDDEN, "course not authorized")
    return selected
