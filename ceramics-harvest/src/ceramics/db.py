"""SQLite 持久层：采集页、发布版状态、登记/拼接数据、撤销记录。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 发布版按 (version, revision) 区分：馆方可沿用原发布编号补发修订，
-- 同一 version 下 revision 递增；全局至多一行 status='active'。
CREATE TABLE IF NOT EXISTS versions(
    version      TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL CHECK(status IN ('collecting','complete','active','failed')),
    published_at TEXT,
    note         TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (version, revision)
);

-- 已确认保存的原始页面；(version, revision, entry, page_index) 唯一。
-- source='fetched' 表示本轮新抓，'carried' 表示位置与正文未变且仍完好、
-- 从既有缓存沿用（无需重下）。进程重启后采集端据此续采。
CREATE TABLE IF NOT EXISTS pages(
    version    TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,
    entry      TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    page_index INTEGER NOT NULL,
    total_pages INTEGER NOT NULL,
    layout     TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    body       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'fetched' CHECK(source IN ('fetched','carried')),
    saved_at   REAL NOT NULL,
    PRIMARY KEY (version, revision, entry, page_index)
);

CREATE TABLE IF NOT EXISTS version_failures(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version   TEXT NOT NULL,
    revision  INTEGER NOT NULL DEFAULT 1,
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
    revision INTEGER NOT NULL DEFAULT 1,
    activated_at REAL NOT NULL
);
"""


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
        conn.commit()
    finally:
        conn.close()


def get_active_release(conn: sqlite3.Connection) -> tuple[str, int] | None:
    """当前查询使用的 (发布编号, 修订)；没有完整可用版本时返回 None。"""
    row = conn.execute("SELECT version, revision FROM versions WHERE status='active' "
                       "LIMIT 1").fetchone()
    return (row["version"], row["revision"]) if row else None
