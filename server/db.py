import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    last_commit TEXT,
    last_checked REAL,
    imported INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    family TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_id TEXT,
    color TEXT NOT NULL DEFAULT '',
    icon TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (family, id)
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    appid TEXT NOT NULL,
    origin TEXT NOT NULL,
    alias TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    category_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    authors TEXT NOT NULL DEFAULT '[]',
    short_description TEXT NOT NULL DEFAULT '',
    install_path TEXT NOT NULL DEFAULT '',
    links TEXT NOT NULL DEFAULT '{}',
    current_version_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    UNIQUE (family, appid)
);
CREATE INDEX IF NOT EXISTS items_family_origin ON items (family, origin);
CREATE INDEX IF NOT EXISTS items_category ON items (family, category_id);

CREATE TABLE IF NOT EXISTS item_ids (
    alt_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items (id)
);

CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items (id),
    version TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    short_description TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    changelog TEXT NOT NULL DEFAULT '',
    icon_asset TEXT,
    screenshots TEXT NOT NULL DEFAULT '[]',
    install_path TEXT NOT NULL DEFAULT '',
    tag TEXT NOT NULL DEFAULT '',
    archive_path TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    bundle_sha256 TEXT NOT NULL DEFAULT '',
    has_sources INTEGER NOT NULL DEFAULT 0,
    source_origin TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (item_id, version)
);

CREATE TABLE IF NOT EXISTS builds (
    version_id TEXT NOT NULL REFERENCES versions (id),
    target TEXT NOT NULL,
    api TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    result TEXT NOT NULL,
    error TEXT,
    log_tail TEXT,
    artifact_path TEXT,
    artifact_name TEXT,
    sha256 TEXT,
    size INTEGER,
    built_at REAL NOT NULL,
    PRIMARY KEY (version_id, target, api, sdk_version)
);
CREATE INDEX IF NOT EXISTS builds_lookup ON builds (version_id, target, api, result);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    cache_key TEXT NOT NULL,
    family TEXT NOT NULL DEFAULT 'application',
    item_id TEXT,
    version_id TEXT,
    appid TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL,
    api TEXT NOT NULL DEFAULT '',
    sdk_version TEXT NOT NULL DEFAULT '',
    bundle_url TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    error TEXT,
    error_code TEXT,
    log_tail TEXT,
    artifact_path TEXT,
    artifact_name TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, priority, seq);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs (cache_key, status);

CREATE TABLE IF NOT EXISTS job_subscribers (
    job_id TEXT NOT NULL REFERENCES jobs (id),
    client_id TEXT NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, client_id)
);

CREATE TABLE IF NOT EXISTS downloads (
    ts REAL NOT NULL,
    item_id TEXT NOT NULL,
    version_id TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL,
    origin TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    api TEXT NOT NULL DEFAULT '',
    client_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS downloads_item_ts ON downloads (item_id, ts);
CREATE INDEX IF NOT EXISTS downloads_ts ON downloads (ts);

CREATE TABLE IF NOT EXISTS sdks (
    target TEXT NOT NULL,
    version TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'release',
    api TEXT NOT NULL,
    zip_path TEXT NOT NULL,
    added_at REAL NOT NULL,
    last_used REAL NOT NULL,
    PRIMARY KEY (target, version)
);
CREATE INDEX IF NOT EXISTS sdks_api ON sdks (target, api);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    content_type TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bans (
    ip TEXT PRIMARY KEY,
    until REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS push_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    event TEXT NOT NULL,
    family TEXT NOT NULL,
    item_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    origin TEXT NOT NULL,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '[]',
    sent_at REAL
);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class Database:
    """One connection, one lock: the server is a single process and SQLite is
    fast enough for its load; the lock keeps writes from interleaving."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(sdks)")}
        if "channel" not in columns:
            self._conn.execute("ALTER TABLE sdks ADD COLUMN channel TEXT NOT NULL DEFAULT 'release'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM meta WHERE key = ?", (key,))
        return default if value is None else value

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def next_seq(self) -> int:
        with self._lock:
            current = int(self.get_meta("job_seq", "0") or 0) + 1
            self.set_meta("job_seq", str(current))
            return current


def now() -> float:
    return time.time()
