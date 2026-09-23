"""Read-only MCP tool for the authenticated user's stores."""

from __future__ import annotations

import json
from typing import Any

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.xm_store import list_authorized_stores


def list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_my_stores",
            "description": "查询当前登录用户有权限的门店列表。无需输入用户 ID。",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    if name != "list_my_stores":
        raise ValueError(f"unknown tool: {name}")
    token = str(creds.get("token") or "")
    if not token:
        raise ValueError("company login required")
    try:
        stores = list_authorized_stores(token)
    except OctopError as exc:
        if exc.code != ErrorCode.TOKEN_EXPIRED:
            raise
        return json.dumps(
            {
                "status": "failed",
                "is_error": True,
                "message": "公司账号登录已过期，请重新登录。",
                "error": {"code": ErrorCode.TOKEN_EXPIRED.value},
            },
            ensure_ascii=False,
        )
    return json.dumps(stores, ensure_ascii=False)


def probe_credentials(creds: dict[str, Any]) -> None:
    token = str(creds.get("token") or "")
    if not token:
        raise ValueError("company login required")
    list_authorized_stores(token)
