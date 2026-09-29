"""Trusted company credentials and selected-store access for XM tools."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from octop.infra.connectors.crypto import decrypt_credentials

if TYPE_CHECKING:
    from octop.infra.db.repos.threads import ThreadRow
    from octop.infra.db.services import RepoBundle, SharedServices


type XmAccessCode = Literal[
    "COMPANY_LOGIN_REQUIRED", "THREAD_FORBIDDEN", "STORE_REQUIRED", "STORE_FORBIDDEN"
]


class XmAccessError(ValueError):
    def __init__(self, code: XmAccessCode) -> None:
        super().__init__(code)
        self.code = code


def company_credentials(
    repos: RepoBundle | SharedServices, user_id: int
) -> tuple[str, dict[str, Any]] | None:
    """Read only this user's active, encrypted company connector credentials."""
    from octop.infra.xm_store import CONNECTOR_KIND  # noqa: PLC0415

    inst = repos.connector_repo.get_by_user_kind(user_id, CONNECTOR_KIND)
    if inst is None or inst.status != "active" or not inst.credential_blob:
        return None
    creds = decrypt_credentials(repos.secret_repo, inst.credential_blob)
    if not creds.get("token"):
        return None
    return inst.instance_id, creds


def agent_thread(repos: RepoBundle, *, user_id: int, agent_id: str, thread_id: str) -> ThreadRow:
    """Reject another user's, another agent's, or non-dashboard session."""
    thread = repos.thread_repo.get(thread_id)
    if (
        thread is None
        or thread.user_id != user_id
        or thread.agent_id != agent_id
        or thread.channel_type != "dashboard"
    ):
        raise XmAccessError("THREAD_FORBIDDEN")
    return thread


async def authorized_store_for_thread(
    repos: RepoBundle,
    *,
    user_id: int,
    agent_id: str,
    thread_id: str,
) -> tuple[ThreadRow, dict[str, str]]:
    """Resolve the current selection against fresh grants, not model arguments."""
    from octop.infra.xm_store import list_authorized_stores_with_oa_id  # noqa: PLC0415

    thread = agent_thread(repos, user_id=user_id, agent_id=agent_id, thread_id=thread_id)
    if not thread.xm_store_id:
        raise XmAccessError("STORE_REQUIRED")
    stored = company_credentials(repos, user_id)
    if stored is None:
        raise XmAccessError("COMPANY_LOGIN_REQUIRED")
    stores = await asyncio.to_thread(list_authorized_stores_with_oa_id, str(stored[1]["token"]))
    store = next((item for item in stores if item["store_id"] == thread.xm_store_id), None)
    if store is None:
        raise XmAccessError("STORE_FORBIDDEN")
    return thread, store
