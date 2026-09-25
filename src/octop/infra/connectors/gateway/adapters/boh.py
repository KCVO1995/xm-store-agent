"""MCP metadata for the BOH inventory-summary data tool.

Calls require Octop's trusted chat runtime and cannot use credential-only gateway HTTP.
"""

from __future__ import annotations

from typing import Any

from octop.i18n import tr


def list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "query_store_cos_page",
            "description": tr("boh.tool_description", "zh"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "startDate": {
                        "type": "string",
                        "description": "统计开始日期，YYYY-MM-DD。",
                        "pattern": r"^\d{4}-\d{2}-\d{2}$",
                    },
                    "endDate": {
                        "type": "string",
                        "description": "统计结束日期，YYYY-MM-DD；跨度最多 62 天。",
                        "pattern": r"^\d{4}-\d{2}-\d{2}$",
                    },
                    "financeCategoryNames": {
                        "type": "array",
                        "description": "要查询的财务分类名称；空数组表示不筛选。",
                        "items": {"type": "string"},
                    },
                    "pageIndex": {
                        "type": "integer",
                        "description": "页码，从 1 开始；每页固定 9999 条。",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 1,
                    },
                },
                "required": ["startDate", "endDate", "financeCategoryNames"],
                "additionalProperties": False,
            },
        }
    ]


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    del creds, name, args
    raise PermissionError("BOH queries require a trusted Octop chat context")


def probe_credentials(creds: dict[str, Any]) -> None:
    if not creds.get("linked_company_connector"):
        raise ValueError("company login required")
