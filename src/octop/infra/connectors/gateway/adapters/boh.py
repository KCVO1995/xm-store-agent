"""Raw BOH MCP contracts; execution requires Octop's trusted chat context."""

from __future__ import annotations

from typing import Any

from octop.i18n import tr


def _date_properties() -> dict[str, Any]:
    return {
        "startDate": {"type": "string", "description": "开始日期 YYYY-MM-DD，最多 62 天。"},
        "endDate": {"type": "string", "description": "结束日期 YYYY-MM-DD。"},
    }


def _report_tool(
    name: str, description: str, *, default_size: int, category: bool
) -> dict[str, Any]:
    properties = _date_properties()
    if category:
        properties["financeCategoryNames"] = {
            "type": "array",
            "items": {"type": "string"},
            "default": ["食材成本"],
            "description": '财务类别；默认 ["食材成本"]，[] 表示不筛选。',
        }
    properties.update(
        pageIndex={"type": "integer", "minimum": 1, "maximum": 50, "default": 1},
        pageSize={
            "type": "integer",
            "minimum": 1,
            "maximum": default_size,
            "default": default_size,
            "description": f"每页条数，默认 {default_size}；结果过大时可减小。",
        },
        datasetId={
            "type": "string",
            "description": "同一次查询第 2 页起传第一页返回的 datasetId，供精确分析核对完整页。",
        },
    )
    if name == "query_store_cos_page":
        properties["rawItemIds"] = {
            "type": "array",
            "items": {"type": "integer"},
            "description": "可选原物料 ID，先用 search_raw_items 确认；映射 BOH riIds。",
        }
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": ["startDate", "endDate"],
            "additionalProperties": False,
        },
    }


def list_tools() -> list[dict[str, Any]]:
    return [
        _report_tool(
            "query_store_cos_page",
            tr("boh.tool_description", "zh"),
            default_size=500,
            category=True,
        ),
        _report_tool(
            "query_store_generic_page",
            tr("boh.generic_description", "zh"),
            default_size=100,
            category=True,
        ),
        _report_tool(
            "query_store_assessment_week_page",
            tr("boh.assessment_week_description", "zh"),
            default_size=50,
            category=False,
        ),
        _report_tool(
            "query_store_generic_week_page",
            tr("boh.generic_week_description", "zh"),
            default_size=50,
            category=False,
        ),
        {
            "name": "search_raw_items",
            "description": tr("boh.search_description", "zh"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "物料编码或名称关键词。"}
                },
                "required": ["keyword"],
                "additionalProperties": False,
            },
        },
        {
            "name": "query_store_cos_daily",
            "description": tr("boh.daily_description", "zh"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **_date_properties(),
                    "financeCategoryNames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": '财务类别，通常为 ["食材成本"]。',
                    },
                    "rawItemIds": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "从原物料搜索结果确认的 1~50 个 ID。",
                    },
                },
                "required": ["startDate", "endDate", "financeCategoryNames", "rawItemIds"],
                "additionalProperties": False,
            },
        },
    ]


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    del creds, name, args
    raise PermissionError("BOH queries require a trusted Octop chat context")


def probe_credentials(creds: dict[str, Any]) -> None:
    if not creds.get("linked_company_connector"):
        raise ValueError("company login required")
