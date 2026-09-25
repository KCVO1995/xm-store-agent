"""Deterministic BOH variance calculations used by the BOH analysis Skill."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from math import sqrt
from typing import Any


def amount(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid BOH amount") from exc


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def flat_report(kind: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum((amount(row.get("usageDiffCost")) for row in records), Decimal(0))
    positive = sum(1 for row in records if amount(row.get("usageDiffCost")) > 0)
    negative = sum(1 for row in records if amount(row.get("usageDiffCost")) < 0)
    result: dict[str, Any] = {
        "recordCount": len(records),
        "usageDiffCost": money(total),
        "positiveCount": positive,
        "negativeCount": negative,
        "financeCategoryFiltered": True,
    }
    if kind == "generic":
        result["lossDiffCost"] = money(
            sum((amount(row.get("lossDiffCost")) for row in records), Decimal(0))
        )
    if kind == "cos":
        by_desc = sorted(
            records,
            key=lambda row: (-amount(row.get("usageDiffCost")), str(row.get("rawName") or "")),
        )
        top = by_desc[:10]
        picked = {id(row) for row in top}
        bottom = [
            row
            for row in sorted(
                records,
                key=lambda row: (amount(row.get("usageDiffCost")), str(row.get("rawName") or "")),
            )
            if id(row) not in picked
        ][:10]

        def labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {
                    "rawCode": row.get("rawCode"),
                    "rawName": row.get("rawName"),
                    "purchaseUnit": row.get("purchaseUnit"),
                    "usageDiffQuantity": row.get("usageDiffQuantity"),
                    "usageDiffCost": money(amount(row.get("usageDiffCost"))),
                }
                for row in rows
            ]

        result["top10"] = labels(top)
        result["bottom10"] = labels(bottom)
    return result


def weekly_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    weeks: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"scrap": Decimal(0), "sales": Decimal(0)}
    )
    week_labels: dict[str, dict[str, Any]] = {}
    month_scrap = month_sales = Decimal(0)
    for row in records:
        week_map = row.get("weekDataMap") or {}
        if not isinstance(week_map, dict):
            raise ValueError("invalid BOH weekDataMap")
        for key, item in week_map.items():
            if not isinstance(item, dict):
                continue
            if key.startswith("week_"):
                weeks[key]["scrap"] += amount(item.get("scrapDiffAmount"))
                weeks[key]["sales"] += amount(item.get("salesDeductionAmount"))
                week_labels.setdefault(
                    key,
                    {"weekLabel": item.get("weekLabel"), "dateRange": item.get("dateRange")},
                )
        current = week_map.get("month_current") or {}
        if isinstance(current, dict):
            month_scrap += amount(current.get("scrapDiffAmount"))
            month_sales += amount(current.get("salesDeductionAmount"))
    return {
        "recordCount": len(records),
        "financeCategoryFiltered": False,
        "monthScrapDiffAmount": money(month_scrap),
        "monthSalesDeductionAmount": money(month_sales),
        "monthRatioPercent": money(month_scrap / month_sales * 100) if month_sales else None,
        "weeks": {
            key: {
                "scrapDiffAmount": money(value["scrap"]),
                "salesDeductionAmount": money(value["sales"]),
                "ratioPercent": money(value["scrap"] / value["sales"] * 100)
                if value["sales"]
                else None,
                **week_labels.get(key, {}),
            }
            for key, value in sorted(
                weeks.items(),
                key=lambda pair: (
                    int(pair[0].split("_", 1)[1]) if pair[0].split("_", 1)[1].isdecimal() else 9999
                ),
            )
        },
    }


def daily_report(days: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    values = [
        float(sum((amount(row.get("usageDiffCost")) for row in rows), Decimal(0)))
        for rows in days.values()
        if rows
    ]
    mean = sum(values) / len(values) if values else 0.0
    deviation = sqrt(sum((value - mean) ** 2 for value in values) / len(values)) if values else 0.0
    out: list[dict[str, Any]] = []
    for day, rows in sorted(days.items()):
        if not rows:
            out.append({"date": day, "status": "no_data"})
            continue
        total = sum((amount(row.get("usageDiffCost")) for row in rows), Decimal(0))
        z = (float(total) - mean) / deviation if deviation else 0.0
        out.append(
            {
                "date": day,
                "status": "ok",
                "recordCount": len(rows),
                "usageDiffCost": money(total),
                "over50": abs(total) > 50,
                "zScore": round(z, 2),
                "statisticalOutlier": abs(z) > 2,
            }
        )
    return {"days": out, "noDataDays": sum(row["status"] == "no_data" for row in out)}
