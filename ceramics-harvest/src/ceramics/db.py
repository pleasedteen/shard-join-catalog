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

-- 采集端在同一轮确认两个入口同版、页数齐全后写入的"收齐清单"。
-- 发布端只接手 switch_status='pending' 的清单，与采集过程解耦：
-- 采集进程退出/重启不影响清单，发布进程重启后继续处理未切换的清单。
CREATE TABLE IF NOT EXISTS ready_rounds(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    version        TEXT NOT NULL,
    registry_pages INTEGER NOT NULL,
    joins_pages    INTEGER NOT NULL,
    switch_status  TEXT NOT NULL DEFAULT 'pending'
                   CHECK(switch_status IN ('pending','switched','failed')),
    created_at     REAL NOT NULL,
    switched_at    REAL
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
