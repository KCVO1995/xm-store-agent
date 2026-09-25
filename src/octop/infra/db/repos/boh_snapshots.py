"""Encrypted, expiring BOH report pages bound to one chat identity."""

from __future__ import annotations

import json
import time
from typing import Any

from octop.infra.db.pool import DatabasePool


class BohSnapshotRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def purge_expired(self) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM boh_report_snapshots WHERE expires_at <= ?", (int(time.time()),)
            )

    def put(
        self,
        *,
        snapshot_id: str,
        page_index: int,
        user_id: int,
        agent_id: str,
        thread_id: str,
        store_id: str,
        report_kind: str,
        query: dict[str, Any],
        total: int,
        payload: bytes,
    ) -> None:
        now = int(time.time())
        query_json = json.dumps(query, ensure_ascii=False, sort_keys=True)
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM boh_report_snapshots WHERE expires_at <= ?", (now,))
            prior = conn.execute(
                "SELECT user_id, agent_id, thread_id, store_id, report_kind, query_json "
                "FROM boh_report_snapshots WHERE snapshot_id = ? LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if prior is not None and (
                prior["user_id"] != user_id
                or prior["agent_id"] != agent_id
                or prior["thread_id"] != thread_id
                or prior["store_id"] != store_id
                or prior["report_kind"] != report_kind
                or prior["query_json"] != query_json
            ):
                raise ValueError("snapshot identity or query mismatch")
            conn.execute(
                "DELETE FROM boh_report_snapshots WHERE snapshot_id = ? AND page_index = ?",
                (snapshot_id, page_index),
            )
            conn.execute(
                "INSERT INTO boh_report_snapshots "
                "(snapshot_id, page_index, user_id, agent_id, thread_id, store_id, "
                "report_kind, query_json, total, payload, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    page_index,
                    user_id,
                    agent_id,
                    thread_id,
                    store_id,
                    report_kind,
                    query_json,
                    total,
                    payload,
                    now + 30 * 60,
                ),
            )

    def pages(
        self, snapshot_id: str, *, user_id: int, agent_id: str, thread_id: str, store_id: str
    ) -> tuple[str, dict[str, Any], list[tuple[int, int, bytes]]]:
        now = int(time.time())
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM boh_report_snapshots WHERE snapshot_id = ? "
                "AND expires_at > ? ORDER BY page_index",
                (snapshot_id, now),
            ).fetchall()
        if not rows or any(
            row["user_id"] != user_id
            or row["agent_id"] != agent_id
            or row["thread_id"] != thread_id
            or row["store_id"] != store_id
            for row in rows
        ):
            raise ValueError("snapshot not found for this conversation")
        query = json.loads(rows[0]["query_json"])
        if any(row["query_json"] != rows[0]["query_json"] for row in rows):
            raise ValueError("snapshot query mismatch")
        pages = [(int(row["page_index"]), int(row["total"]), bytes(row["payload"])) for row in rows]
        return str(rows[0]["report_kind"]), query, pages
