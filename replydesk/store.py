"""SQLite store via aiosqlite.

Two tables:
- ``processed``  - audit log + dedupe. One row per processed message-id.
                  ``outcome`` goes through ``sending`` -> ``sent`` (or
                  ``draft``/``escalate``/``ignore``) so we never reply twice
                  even after a crash mid-send.
- ``replies_sent`` - lightweight per-sender daily counter for the cap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from .models import (
    Analysis,
    Decision,
    Reply,
)

log = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    message_id   TEXT PRIMARY KEY,
    outcome       TEXT NOT NULL,
    analysis_json TEXT,
    reply_json    TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_processed_outcome ON processed(outcome);

CREATE TABLE IF NOT EXISTS replies_sent (
    sender    TEXT NOT NULL,
    sent_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_replies_sent_sender ON replies_sent(sender);
CREATE INDEX IF NOT EXISTS idx_replies_sent_sent_at ON replies_sent(sent_at);
"""


class Store:
    """Async SQLite store. Use as an async context manager or call
    ``connect()`` / ``close()`` explicitly."""

    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Store:
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def connect(self) -> None:
        import os
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- processed --------------------------------------------------------
    async def already_processed(self, message_id: str) -> bool:
        if self._db is None:
            raise RuntimeError("Store is not connected")
        async with self._db.execute(
            "SELECT 1 FROM processed WHERE message_id = ? LIMIT 1",
            (message_id,),
        ) as cur:
            return await cur.fetchone() is not None

    async def claim(self, message_id: str) -> bool:
        """Atomically insert a placeholder row with outcome='processing'.

        Returns True if we won the race (we now own this message_id), False
        if another worker has already claimed it. This is the dedupe
        primitive used by :meth:`Pipeline.process_one` so concurrent
        workers can't double-process the same message-id.
        """
        if self._db is None:
            raise RuntimeError("Store is not connected")
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO processed "
            "(message_id, outcome, analysis_json, reply_json, created_at) "
            "VALUES (?, 'processing', NULL, NULL, ?)",
            (message_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def reclaim_sending(self) -> int:
        """On startup, move any rows left in 'processing' or 'sending' state
        to 'review' so the system never replies twice.

        Returns the number of reclaimed rows. Those messages are NOT
        re-processed: the system never replies twice.
        """
        if self._db is None:
            raise RuntimeError("Store is not connected")
        cur = await self._db.execute(
            "UPDATE processed SET outcome = 'review' "
            "WHERE outcome IN ('sending', 'processing')"
        )
        await self._db.commit()
        return cur.rowcount

    async def record(
        self,
        message_id: str,
        outcome: Decision | str,
        *,
        analysis: Analysis | None = None,
        reply: Reply | None = None,
    ) -> None:
        """Insert a row with the given outcome.

        For ``send`` decisions, ``main.py`` first writes outcome='sending'
        (so a crash is recoverable), then calls :meth:`mark_sent` after
        SMTP success.
        """
        if self._db is None:
            raise RuntimeError("Store is not connected")
        await self._db.execute(
            "INSERT OR REPLACE INTO processed "
            "(message_id, outcome, analysis_json, reply_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                message_id,
                outcome,
                analysis.model_dump_json() if analysis else None,
                reply.model_dump_json() if reply else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._db.commit()

    async def mark_sent(self, message_id: str, sender: str) -> None:
        """Finalise a 'sending' row to 'sent' and bump the per-sender cap."""
        if self._db is None:
            raise RuntimeError("Store is not connected")
        await self._db.execute(
            "UPDATE processed SET outcome = 'sent' WHERE message_id = ?",
            (message_id,),
        )
        await self._db.execute(
            "INSERT INTO replies_sent (sender, sent_at) VALUES (?, ?)",
            (sender, datetime.now(timezone.utc).isoformat()),
        )
        await self._db.commit()

    async def count_replies_today(self, sender: str) -> int:
        """Number of replies sent to ``sender`` in the last 24h."""
        if self._db is None:
            raise RuntimeError("Store is not connected")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        async with self._db.execute(
            "SELECT COUNT(*) FROM replies_sent WHERE sender = ? AND sent_at >= ?",
            (sender, cutoff),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    # -- helpers ----------------------------------------------------------
    async def iter_recent(self, limit: int = 50) -> list[dict]:
        if self._db is None:
            raise RuntimeError("Store is not connected")
        rows: list[dict] = []
        async with self._db.execute(
            "SELECT message_id, outcome, created_at FROM processed "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            for r in await cur.fetchall():
                rows.append(dict(r))
        return rows


__all__ = ["Store"]
