"""Authenticated store-selector endpoints. Agent tools use the connector gateway instead."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.infra.xm_store import select_store_for_user, stores_for_user

router = APIRouter()


class StoreOption(BaseModel):
    store_id: str
    store_name: str
    store_no: str


class StoreListResponse(BaseModel):
    enabled: bool
    stores: list[StoreOption]
    selected_store_id: str | None


class StoreSelectionBody(BaseModel):
    store_id: str | None = Field(description="Authorized store ID, or null to clear selection.")
    thread_id: str | None = Field(default=None, description="Current thread; omit for new chat.")


@router.get(
    "/xm-store/stores", summary="List my authorized stores", response_model=StoreListResponse
)
async def get_stores(
    thread_id: str | None = None,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> StoreListResponse:
    """Load selector options directly after login, without invoking MCP."""
    result = await stores_for_user(server.services, user.id, thread_id)
    return StoreListResponse.model_validate(result)


@router.put("/xm-store/selection", summary="Select a store for this conversation")
async def put_selection(
    body: StoreSelectionBody,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str | None]:
    """Reject forged store IDs and persist thread selection plus the user's next-chat default."""
    await select_store_for_user(server.services, user.id, body.store_id, body.thread_id)
    return {"selected_store_id": body.store_id}
