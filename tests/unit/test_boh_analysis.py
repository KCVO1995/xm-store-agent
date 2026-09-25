"""Deterministic BOH variance arithmetic and default date selection."""

from __future__ import annotations

from datetime import date

from octop.infra.boh import default_analysis_period
from octop.infra.boh_analysis import daily_report, flat_report, weekly_report


def test_flat_report_uses_cost_fields_and_keeps_material_codes():
    result = flat_report(
        "cos",
        [
            {
                "rawCode": "A",
                "rawName": "同名",
                "purchaseUnit": "件",
                "usageDiffCost": "12.37",
                "usageDiffQuantity": 1,
            },
            {
                "rawCode": "B",
                "rawName": "同名",
                "purchaseUnit": "公斤",
                "usageDiffCost": "-2.11",
                "usageDiffQuantity": -3,
            },
        ],
    )
    assert result["usageDiffCost"] == "10.26"
    assert result["top10"][0]["rawCode"] == "A"
    assert result["top10"][1]["rawCode"] == "B"
    assert result["bottom10"] == []  # 前十已覆盖全部两条，不重复展示
    assert "usageDiffQuantity" not in result
    generic = flat_report(
        "generic",
        [
            {"usageDiffCost": "5.20", "lossDiffCost": "1.25"},
            {"usageDiffCost": "-1.00", "lossDiffCost": "-0.25"},
        ],
    )
    assert generic["usageDiffCost"] == "4.20"
    assert generic["lossDiffCost"] == "1.00"


def test_weekly_uses_week_map_and_does_not_mix_last_month():
    result = weekly_report(
        [
            {
                "weekDataMap": {
                    "week_1": {"scrapDiffAmount": "10", "salesDeductionAmount": "100"},
                    "week_2": {"scrapDiffAmount": "-4", "salesDeductionAmount": "50"},
                    "month_current": {"scrapDiffAmount": "6", "salesDeductionAmount": "150"},
                    "month_last": {"scrapDiffAmount": "999", "salesDeductionAmount": "999"},
                }
            }
        ]
    )
    assert result["monthScrapDiffAmount"] == "6.00"
    assert result["monthRatioPercent"] == "4.00"
    assert result["weeks"]["week_2"]["ratioPercent"] == "-8.00"
    assert result["financeCategoryFiltered"] is False


def test_daily_no_data_is_not_zero_and_default_period_handles_month_start():
    result = daily_report(
        {
            "2026-09-01": [{"usageDiffCost": "60"}],
            "2026-09-02": [],
            "2026-09-03": [{"usageDiffCost": "-10"}],
        }
    )
    assert result["days"][0]["over50"] is True
    assert result["days"][1] == {"date": "2026-09-02", "status": "no_data"}
    assert result["noDataDays"] == 1
    assert default_analysis_period("Asia/Shanghai", today=date(2026, 10, 1)) == {
        "startDate": "2026-09-01",
        "endDate": "2026-09-30",
        "timezone": "Asia/Shanghai",
    }
    assert (
        default_analysis_period("Asia/Shanghai", today=date(2026, 9, 25))["endDate"] == "2026-09-24"
    )
