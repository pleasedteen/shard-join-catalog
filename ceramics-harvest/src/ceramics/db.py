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
-- record_count 为该页解析出的业务记录数：重启后只按页数+校验和+记录数复用，
-- 不必重新解析整版即可确认业务行（sherds/joins）已随该页同事务入库。
CREATE TABLE IF NOT EXISTS pages(
    version    TEXT NOT NULL,
    entry      TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    page_index INTEGER NOT NULL,
    total_pages INTEGER NOT NULL,
    layout     TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    body       TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
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

-- 校验期按版流式扫描的常用路径（pages/sherds/joins 主键已覆盖 version 前缀，
-- 这里补一个失败排查与状态查询用的页面计数索引）。
CREATE INDEX IF NOT EXISTS idx_pages_version_entry ON pages(version, entry);
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
        # 旧库迁移：pages 早期没有 record_count 列，补齐后由后续采页/校验自然回填
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
        if "record_count" not in cols:
            conn.execute("ALTER TABLE pages ADD COLUMN record_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def get_active_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT version FROM versions WHERE status='active' LIMIT 1").fetchone()
    return row["version"] if row else None
