"""SQLite storage: schema, volume swap, and snapshot-consistent queries.

The database holds the capture index and the per-response digest-check
result.  Revisit evidence is *derived at query time* (a revisit is backed
iff a digest-matching, digest-verified response exists in the volumes
currently stored), so replacing a volume never requires rescanning other
volumes: ingest work grows only with the size of the old and new volume.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
    volume      TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    records     INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
    url         TEXT NOT NULL,
    ts          TEXT NOT NULL,          -- canonical UTC ISO-8601 capture instant
    volume      TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('response', 'revisit')),
    digest      TEXT NOT NULL,          -- canonical sha1/base32 payload digest
    valid       INTEGER,                -- response: 1 = digest verified, 0 = failed; revisit: NULL
    http_status INTEGER,
    PRIMARY KEY (url, ts)               -- one capture per URL per UTC instant, across all volumes
);

CREATE INDEX IF NOT EXISTS idx_captures_volume ON captures(volume);

-- Evidence lookup for revisits: verified response bodies by digest.
CREATE INDEX IF NOT EXISTS idx_verified_response_digest
    ON captures(digest) WHERE type = 'response' AND valid = 1;
"""

# One round trip per URL; revisit evidence resolved against the *current*
# committed state of the verified-response set.
CAPTURES_FOR_URL_SQL = """
SELECT c.ts, c.type, c.digest, c.valid, c.volume, c.http_status,
       CASE WHEN c.type = 'response' THEN c.valid
            ELSE EXISTS(SELECT 1 FROM captures r
                        WHERE r.type = 'response' AND r.valid = 1
                              AND r.digest = c.digest)
       END AS evidenced
FROM captures c
WHERE c.url = ?
ORDER BY c.ts ASC
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        con = self.connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
        finally:
            con.close()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        con.isolation_level = None  # autocommit; transactions are explicit
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    # -- ingest ------------------------------------------------------------

    def swap_volume(self, volume: str, captures, volume_row: dict) -> list[dict]:
        """Replace *volume* wholesale in one transaction.

        Returns the list of (url, ts) conflicts with *other* volumes; when
        non-empty nothing has been written and the caller must abort.  Any
        exception also rolls back, so the old volume survives a failed swap.
        """
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM captures WHERE volume = ?", (volume,))
            conflicts = []
            for cap in captures:
                row = con.execute(
                    "SELECT volume FROM captures WHERE url = ? AND ts = ?",
                    (cap.url, cap.ts),
                ).fetchone()
                if row is not None:
                    conflicts.append({"url": cap.url, "ts": cap.ts, "held_by": row["volume"]})
            if conflicts:
                con.execute("ROLLBACK")
                return conflicts
            con.executemany(
                "INSERT INTO captures (url, ts, volume, type, digest, valid, http_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (c.url, c.ts, volume, c.type, c.digest, c.valid, c.http_status)
                    for c in captures
                ],
            )
            con.execute(
                "INSERT INTO volumes (volume, filename, sha256, size_bytes, records, ingested_at)"
                " VALUES (:volume, :filename, :sha256, :size_bytes, :records, :ingested_at)"
                " ON CONFLICT(volume) DO UPDATE SET"
                "   filename = excluded.filename, sha256 = excluded.sha256,"
                "   size_bytes = excluded.size_bytes, records = excluded.records,"
                "   ingested_at = excluded.ingested_at",
                {"volume": volume, **volume_row},
            )
            con.execute("COMMIT")
            return []
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def delete_volume(self, volume: str) -> bool:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM captures WHERE volume = ?", (volume,))
            cur = con.execute("DELETE FROM volumes WHERE volume = ?", (volume,))
            con.execute("COMMIT")
            return cur.rowcount > 0
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    # -- queries (each runs inside a single read transaction, so every -----
    # -- query sees exactly one committed state of the index) ---------------

    def list_volumes(self) -> list[sqlite3.Row]:
        con = self.connect()
        try:
            con.execute("BEGIN")
            rows = con.execute(
                "SELECT * FROM volumes ORDER BY volume"
            ).fetchall()
            con.execute("COMMIT")
            return rows
        finally:
            con.close()

    def captures_for_url(self, url: str) -> list[sqlite3.Row]:
        con = self.connect()
        try:
            con.execute("BEGIN")
            rows = con.execute(CAPTURES_FOR_URL_SQL, (url,)).fetchall()
            con.execute("COMMIT")
            return rows
        finally:
            con.close()
