"""Dashboard QiXueBao course selector for one expert and conversation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from octop.api.common.agent import require_agent_row
from octop.api.deps import current_user, get_server
from octop.infra.qixuebao_courses import courses_for_user, select_courses_for_user

router = APIRouter()


class CourseOption(BaseModel):
    course_id: str
    course_name: str


class CourseListResponse(BaseModel):
    courses: list[CourseOption]
    selected_courses: list[CourseOption]
    selected_course_ids: list[str]
    page_num: int
    page_size: int
    pages: int
    total: int


class CourseSelectionBody(BaseModel):
    thread_id: str = Field(description="Dashboard conversation to update.")
    course_ids: list[str] = Field(
        default_factory=list,
        description="Selected authorized course IDs; [] means all courses.",
    )


@router.get(
    "/agents/{agent_id}/chat-context/courses",
    summary="List authorized QiXueBao courses for chat",
    response_model=CourseListResponse,
)
async def get_courses(
    agent_id: str,
    thread_id: str | None = None,
    keyword: str = Query(default="", max_length=200),
    page_num: int = Query(default=1, ge=1),
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> CourseListResponse:
    """Search enabled courses using the current user's company token."""
    require_agent_row(agent_id, user=user, as_user=None, server=server)
    result = await courses_for_user(
        server.services,
        user.id,
        agent_id,
        thread_id=thread_id,
        keyword=keyword,
        page_num=page_num,
    )
    return CourseListResponse.model_validate(result)


@router.put(
    "/agents/{agent_id}/chat-context/courses/selection",
    summary="Select courses for this conversation",
)
async def put_course_selection(
    agent_id: str,
    body: CourseSelectionBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, list[str]]:
    """Persist a course scope after checking expert, thread ownership, and current grants."""
    require_agent_row(agent_id, user=user, as_user=None, server=server)
    selected = await select_courses_for_user(
        server.services, user.id, agent_id, body.thread_id, body.course_ids
    )
    return {"selected_course_ids": selected}
