"""Yuxue MCP discovery; data access requires trusted in-process chat context."""

from __future__ import annotations

from typing import Any

from octop.i18n import tr


def list_tools() -> list[dict[str, Any]]:
    from octop.infra.yuxue import TOOL_NAME, CourseDetailQuery

    return [
        {
            "name": TOOL_NAME,
            "description": tr("yuxue.tool_description", "zh"),
            "inputSchema": CourseDetailQuery.model_json_schema(),
        }
    ]


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    del creds, name, args
    raise PermissionError(tr("yuxue.thread_forbidden", "zh"))


def probe_credentials(creds: dict[str, Any]) -> None:
    if not creds.get("linked_company_connector"):
        raise ValueError(tr("yuxue.company_login_required", "zh"))
