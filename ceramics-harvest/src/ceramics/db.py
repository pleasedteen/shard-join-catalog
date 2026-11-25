"""SQLite 持久层：采集页、发布版状态、登记/拼接数据、撤销记录、更正谱系。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS versions(
    version      TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK(status IN ('collecting','complete','active','failed')),
    kind         TEXT NOT NULL DEFAULT 'full' CHECK(kind IN ('full','correction')),
    base_version TEXT,
    published_at TEXT,
    note         TEXT,
    updated_at   REAL NOT NULL
);

-- 已确认保存的原始页面；(version, entry, page_index) 唯一。
-- 整版 version 即发布版号；更正单按 target_version 落盘，kind='correction' 并记录基版，
-- 进程重启后采集端据此跳过已确认页面继续采集。
CREATE TABLE IF NOT EXISTS pages(
    version        TEXT NOT NULL,
    entry          TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    page_index     INTEGER NOT NULL,
    total_pages    INTEGER NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'full' CHECK(kind IN ('full','correction')),
    base_version   TEXT,
    target_version TEXT,
    layout         TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    body           TEXT NOT NULL,
    saved_at       REAL NOT NULL,
    PRIMARY KEY (version, entry, page_index)
);

-- 更正谱系：目标版 -> 基版。激活更正单时登记，查询时据此沿谱系从本地整版物化。
CREATE TABLE IF NOT EXISTS edition_lineage(
    target_version TEXT PRIMARY KEY,
    base_version   TEXT NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS version_failures(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version   TEXT NOT NULL,
    stage     TEXT NOT NULL,
    reason    TEXT NOT NULL,
    detail    TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sherds(
    version    TEXT NOT NULL,
    catalog_no TEXT NOT NULL,
    museum     TEXT,
    material   TEXT,
    weight_g   REAL,
    PRIMARY KEY (version, catalog_no)
);

CREATE TABLE IF NOT EXISTS joins(
    version  TEXT NOT NULL,
    edge_key TEXT NOT NULL,
    a        TEXT NOT NULL,
    b        TEXT NOT NULL,
    bond     TEXT,
    confidence TEXT,
    PRIMARY KEY (version, edge_key)
);

CREATE TABLE IF NOT EXISTS revocations(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version   TEXT NOT NULL,
    edge_key  TEXT NOT NULL,
    reason    TEXT,
    revoked_at REAL NOT NULL,
    UNIQUE (version, edge_key)
);

CREATE TABLE IF NOT EXISTS activation_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    activated_at REAL NOT NULL
);
"""

# 旧库平滑升级：逐列补齐更正单需要的字段（全新库由 SCHEMA 直接建出）。
_MIGRATIONS = (
    ("versions", "kind", "ALTER TABLE versions ADD COLUMN kind TEXT NOT NULL DEFAULT 'full'"),
    ("versions", "base_version",
     "ALTER TABLE versions ADD COLUMN base_version TEXT"),
    ("pages", "kind", "ALTER TABLE pages ADD COLUMN kind TEXT NOT NULL DEFAULT 'full'"),
    ("pages", "base_version", "ALTER TABLE pages ADD COLUMN base_version TEXT"),
    ("pages", "target_version", "ALTER TABLE pages ADD COLUMN target_version TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(ddl)


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def get_active_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT version FROM versions WHERE status='active' LIMIT 1").fetchone()
    return row["version"] if row else None
