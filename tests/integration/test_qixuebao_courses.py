"""QiXueBao course search and selection follow company-user and thread scope."""

from __future__ import annotations

import json

import httpx
import pytest

from octop.api.app import build_app
from octop.api.routers.chat.models import ChatTurnBody
from octop.infra import qixuebao_courses as courses_service
from octop.infra import xm_store as store_service


def test_course_request_uses_company_token_and_documented_payload(monkeypatch):
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "records": [
                    {"courseId": "course-1", "courseName": "课程一", "isEnable": True},
                    {"courseId": "course-2", "courseName": "停用课程", "isEnable": False},
                ],
                "pages": 1,
                "total": 1,
            },
        )

    transport = httpx.MockTransport(respond)
    original_client = httpx.Client
    monkeypatch.setattr(
        courses_service.httpx,
        "Client",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    page = courses_service.query_authorized_courses("company-token", keyword="课程", page_num=2)
    request = captured[0]
    assert str(request.url) == courses_service.COURSES_URL
    assert request.headers["token"] == "company-token"
    assert request.headers["systemtype"] == "TALENTS"
    assert json.loads(request.content) == {
        "isEnable": True,
        "keyword": "课程",
        "pageNum": 2,
        "pageSize": 1000,
    }
    assert page["courses"] == [{"course_id": "course-1", "course_name": "课程一"}]


@pytest.mark.asyncio
async def test_course_selection_is_scoped_to_expert_user_and_thread(
    env_with_main_agent, monkeypatch
):
    client, server, admin_auth, agent_id = env_with_main_agent
    route = f"/api/agents/{agent_id}/chat-context/courses"
    assert "/api/agents/{agent_id}/chat-context/courses" in build_app(server).openapi()["paths"]

    def fake_login(login_name: str, _password: str):
        return {
            "platformUserId": f"id-{login_name}",
            "platformUserName": login_name,
            "token": f"token-{login_name}",
        }

    def fake_courses(token: str, *, keyword: str = "", page_num: int = 1):
        assert page_num == 1
        owner = token.removeprefix("token-")
        items = [
            {"course_id": f"course-{owner}-1", "course_name": f"{owner} 第一课"},
            {"course_id": f"course-{owner}-2", "course_name": f"{owner} 第二课"},
        ]
        return {
            "courses": [item for item in items if keyword in item["course_name"]],
            "page_num": page_num,
            "page_size": 1000,
            "pages": 1,
            "total": len(items),
        }

    monkeypatch.setattr(store_service, "login_account", fake_login)
    monkeypatch.setattr(courses_service, "query_authorized_courses", fake_courses)

    async def sign_in(name: str) -> dict[str, str]:
        response = await client.post(
            "/api/auth/xm-store/login", json={"login_name": name, "password": "secret"}
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    alice = await sign_in("alice")
    bob = await sign_in("bob")
    disabled = await client.get(route, headers=alice)
    assert disabled.status_code == 403

    configured = await client.patch(
        f"/api/agents/{agent_id}",
        headers=admin_auth,
        json={"is_shared": True, "chat_selectors": ["qixuebao_course"]},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["chat_selectors"] == ["qixuebao_course"]

    searched = await client.get(f"{route}?keyword=第二", headers=alice)
    assert searched.status_code == 200, searched.text
    assert searched.json()["courses"] == [
        {"course_id": "course-alice-2", "course_name": "alice 第二课"}
    ]
    assert searched.json()["selected_course_ids"] == []

    alice_thread = await client.post(f"/api/agents/{agent_id}/threads", headers=alice)
    bob_thread = await client.post(f"/api/agents/{agent_id}/threads", headers=bob)
    assert alice_thread.status_code == bob_thread.status_code == 201
    alice_thread_id = alice_thread.json()["thread_id"]
    bob_thread_id = bob_thread.json()["thread_id"]
    assert (
        courses_service.selected_course_ids(
            server.services.thread_repo.get(alice_thread_id).chat_context_json
        )
        == []
    )

    forbidden_course = await client.put(
        f"{route}/selection",
        headers=alice,
        json={"thread_id": alice_thread_id, "course_ids": ["course-bob-1"]},
    )
    assert forbidden_course.status_code == 403
    forbidden_thread = await client.put(
        f"{route}/selection",
        headers=alice,
        json={"thread_id": bob_thread_id, "course_ids": ["course-alice-1"]},
    )
    assert forbidden_thread.status_code == 403

    selected = await client.put(
        f"{route}/selection",
        headers=alice,
        json={
            "thread_id": alice_thread_id,
            "course_ids": ["course-alice-1", "course-alice-2"],
        },
    )
    assert selected.status_code == 200, selected.text
    restored = await client.get(f"{route}?thread_id={alice_thread_id}&keyword=第二", headers=alice)
    assert restored.json()["selected_course_ids"] == ["course-alice-1", "course-alice-2"]
    assert len(restored.json()["selected_courses"]) == 2
    assert (
        await client.get(f"{route}?thread_id={alice_thread_id}", headers=bob)
    ).status_code == 403
    assert await courses_service.authorized_courses_for_thread(
        server.services,
        user_id=server.services.thread_repo.get(alice_thread_id).user_id,
        agent_id=agent_id,
        thread_id=alice_thread_id,
    ) == ["course-alice-1", "course-alice-2"]

    next_thread = await client.post(f"/api/agents/{agent_id}/threads", headers=alice)
    assert next_thread.status_code == 201
    fresh = await client.get(f"{route}?thread_id={next_thread.json()['thread_id']}", headers=alice)
    assert fresh.json()["selected_course_ids"] == []
    assert ChatTurnBody.from_ws_payload(
        {"text": "总结课程", "qixuebao_course_ids": ["course-alice-1"]}
    ).qixuebao_course_ids == ["course-alice-1"]
