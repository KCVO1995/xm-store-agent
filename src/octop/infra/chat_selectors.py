"""Supported per-expert selectors shown above the dashboard composer."""

from __future__ import annotations

import json

from octop.infra.errors import ErrorCode, OctopError

XM_STORE = "xm_store"
QIXUEBAO_COURSE = "qixuebao_course"
SUPPORTED_CHAT_SELECTORS = frozenset({XM_STORE, QIXUEBAO_COURSE})


def validate_chat_selectors(keys: list[str]) -> list[str]:
    if len(keys) != len(set(keys)) or set(keys) - SUPPORTED_CHAT_SELECTORS:
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "invalid chat selectors")
    return keys


def parse_chat_selectors(raw: str | None) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [key for key in parsed if isinstance(key, str) and key in SUPPORTED_CHAT_SELECTORS]


def selector_enabled(raw: str | None, key: str) -> bool:
    return key in parse_chat_selectors(raw)
