"""SQLite 持久层：采集页、发布版状态、登记/拼接数据、撤销记录。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS versions(
    version      TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK(status IN ('collecting','complete','active','failed')),
    published_at TEXT,
    note         TEXT,
    updated_at   REAL NOT NULL
);

-- 已确认保存的原始页面；(version, entry, page_index) 唯一，
-- 进程重启后采集端据此跳过已确认页面继续采集。
CREATE TABLE IF NOT EXISTS pages(
    version    TEXT NOT NULL,
    entry      TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    page_index INTEGER NOT NULL,
    total_pages INTEGER NOT NULL,
    layout     TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    body       TEXT NOT NULL,
    saved_at   REAL NOT NULL,
    PRIMARY KEY (version, entry, page_index)
);

CREATE TABLE IF NOT EXISTS version_failures(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version   TEXT NOT NULL,
    stage     TEXT NOT NULL,
    reason    TEXT NOT NULL,
    detail    TEXT,
    created_at REAL NOT NULL
);

-- 站点核验发现的异常页（损坏/截断等），保留页位置供 status 展示。
CREATE TABLE IF NOT EXISTS page_issues(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL,
    entry       TEXT NOT NULL,
    page_index  INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT,
    detected_at REAL NOT NULL
);

-- 每个入口在某发布版上最近一次完整核验的清单摘要：
-- 摘要与页数都未变时，无需再逐页核对，核验流量不随页数增长。
CREATE TABLE IF NOT EXISTS entry_verifications(
    version     TEXT NOT NULL,
    entry       TEXT NOT NULL,
    digest      TEXT NOT NULL,
    total_pages INTEGER NOT NULL,
    verified_at REAL NOT NULL,
    PRIMARY KEY (version, entry)
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


def get_active_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT version FROM versions WHERE status='active' LIMIT 1").fetchone()
    return row["version"] if row else None
