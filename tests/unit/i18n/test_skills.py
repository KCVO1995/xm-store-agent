"""tests/unit/i18n/test_skills.py"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from octop.i18n import all_skill_labels, skill_display_name


def test_skill_display_name_known_zh():
    assert skill_display_name("pdf", "zh") == "PDF 处理"


def test_skill_display_name_octop_assistant_slug_zh():
    assert skill_display_name("octop-assistant", "zh") == "xiaomian 配置助手"


def test_skill_display_name_octop_assistant_name_zh():
    assert skill_display_name("octop_assistant", "zh") == "xiaomian 配置助手"


def test_skill_display_name_unknown_passthrough():
    assert skill_display_name("my-custom-skill", "en") == "my-custom-skill"


def test_skill_display_name_empty_passthrough():
    assert skill_display_name(None, "en") == ""


def test_all_skill_labels_includes_pdf():
    labels = all_skill_labels("en")
    assert labels["docx"] == "Word"
    assert labels["octop-assistant"] == "xiaomian Assistant"


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_dashboard_skill_labels_match_backend(locale: str):
    repo = Path(__file__).resolve().parents[3]
    dashboard = json.loads(
        (repo / f"dashboard/src/locales/{locale}.json").read_text(encoding="utf-8")
    )
    backend = json.loads((repo / f"src/octop/i18n/{locale}.json").read_text(encoding="utf-8"))
    for slug, label in backend["skills"].items():
        assert dashboard["skills"][slug] == label
