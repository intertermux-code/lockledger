"""SQLite snapshot store."""

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .models import PackageRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY,
    source     TEXT    NOT NULL,
    sha256     TEXT    NOT NULL,
    label      TEXT    NOT NULL DEFAULT '',
    format     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS packages (
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    version      TEXT    NOT NULL,
    ecosystem    TEXT    NOT NULL,
    resolved_url TEXT    NOT NULL DEFAULT '',
    integrity    TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS packages_name_idx ON packages (name);
CREATE UNIQUE INDEX IF NOT EXISTS snapshots_sha_idx ON snapshots (sha256, source);
"""


def default_db_path() -> Path:
    return Path.cwd() / "lockledger.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def find_snapshot(self, sha256: str, source: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM snapshots WHERE sha256 = ? AND source = ?", (sha256, source)
        ).fetchone()

    def snapshot_by_id(self, snapshot_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()

    def add_snapshot(self, source: str, sha256: str, label: str, lock_format: str,
                     records: list[PackageRecord]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO snapshots (source, sha256, label, format, created_at) VALUES (?, ?, ?, ?, ?)",
            (source, sha256, label, lock_format, utc_now()),
        )
        snapshot_id = int(cursor.lastrowid)
        self.connection.executemany(
            "INSERT INTO packages (snapshot_id, name, version, ecosystem, resolved_url, integrity)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    snapshot_id,
                    record.name,
                    record.version,
                    record.ecosystem,
                    record.resolved_url,
                    record.integrity,
                )
                for record in records
            ],
        )
        self.connection.commit()
        return snapshot_id

    def package_count(self, snapshot_id: int) -> int:
        row = self.connection.execute(
            "SELECT count(name) FROM packages WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        return int(row[0])

    def snapshots(self) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM snapshots ORDER BY id").fetchall()

    def history(self, name: str) -> list[dict]:
        """Every snapshot in order, listing each version of `name` it held.

        A lockfile can pin the same package at several versions (npm nests
        duplicated subtrees), so a snapshot maps to a version list.
        """
        rows = self.connection.execute(
            """
            SELECT s.id, s.label, s.source, s.format, s.created_at,
                   p.version AS version, p.ecosystem AS ecosystem
            FROM snapshots s
            LEFT JOIN packages p ON p.snapshot_id = s.id AND LOWER(p.name) = LOWER(?)
            ORDER BY s.id, p.version
            """,
            (name,),
        ).fetchall()

        by_snapshot: dict[int, dict] = {}
        versions: dict[int, list[str]] = defaultdict(list)
        ecosystems: dict[int, set[str]] = defaultdict(set)
        for row in rows:
            entry = by_snapshot.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "label": row["label"],
                    "source": row["source"],
                    "format": row["format"],
                    "created_at": row["created_at"],
                    "versions": [],
                },
            )
            if row["version"]:
                versions[row["id"]].append(row["version"])
                ecosystems[row["id"]].add(row["ecosystem"])
        for snapshot_id, entry in by_snapshot.items():
            entry["versions"] = sorted(set(versions[snapshot_id]))
            entry["ecosystems"] = sorted(ecosystems[snapshot_id])
        return [by_snapshot[key] for key in sorted(by_snapshot)]

    def ecosystems(self, name: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT ecosystem FROM packages WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchall()
        return sorted(row[0] for row in rows)

    def record_for(self, name: str, version: str) -> PackageRecord | None:
        row = self.connection.execute(
            "SELECT * FROM packages WHERE LOWER(name) = LOWER(?) AND version = ?"
            " ORDER BY snapshot_id DESC LIMIT 1",
            (name, version),
        ).fetchone()
        if row is None:
            return None
        return PackageRecord(
            name=row["name"],
            version=row["version"],
            ecosystem=row["ecosystem"],
            resolved_url=row["resolved_url"],
            integrity=row["integrity"],
        )
