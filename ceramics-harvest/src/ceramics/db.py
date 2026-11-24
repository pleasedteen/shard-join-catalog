"""SQLite 持久层：采集页、分册清单、发布版状态、登记/拼接数据、撤销记录。"""
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

-- 已确认保存的原始页面；(version, entry, volume, page_index) 唯一。
-- volume 为空串表示旧站两入口协议下的整入口页面；分册采集则为分册名。
-- 进程重启后采集端据此按分册跳过已确认页面继续采集。
CREATE TABLE IF NOT EXISTS pages(
    version    TEXT NOT NULL,
    entry      TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    volume     TEXT NOT NULL DEFAULT '',
    page_index INTEGER NOT NULL,
    total_pages INTEGER NOT NULL,
    layout     TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    body       TEXT NOT NULL,
    saved_at   REAL NOT NULL,
    PRIMARY KEY (version, entry, volume, page_index)
);

-- 某发布版的分册清单：先按站点清单登记*全部应采分册*（total_pages 暂空），
-- 每采到该分册首页再补全页数。激活前据此核对"所有分册同版收齐"。
CREATE TABLE IF NOT EXISTS volume_manifest(
    version     TEXT NOT NULL,
    entry       TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    volume      TEXT NOT NULL,
    ordinal     INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER,
    PRIMARY KEY (version, entry, volume)
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


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _migrate_pages_volume(conn: sqlite3.Connection) -> None:
    """把早期没有 volume 列的 pages 表重建为分册感知的表，旧数据归入 '' 分册。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(pages)")]
    if "volume" in cols:
        return
    conn.execute("ALTER TABLE pages RENAME TO pages_legacy")
    conn.execute(
        "CREATE TABLE pages("
        " version TEXT NOT NULL,"
        " entry TEXT NOT NULL CHECK(entry IN ('registry','joins')),"
        " volume TEXT NOT NULL DEFAULT '',"
        " page_index INTEGER NOT NULL,"
        " total_pages INTEGER NOT NULL,"
        " layout TEXT NOT NULL,"
        " checksum TEXT NOT NULL,"
        " body TEXT NOT NULL,"
        " saved_at REAL NOT NULL,"
        " PRIMARY KEY (version, entry, volume, page_index))")
    conn.execute(
        "INSERT INTO pages(version, entry, volume, page_index, total_pages,"
        " layout, checksum, body, saved_at) "
        "SELECT version, entry, '', page_index, total_pages, layout, checksum, body, saved_at "
        "FROM pages_legacy")
    conn.execute("DROP TABLE pages_legacy")


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate_pages_volume(conn)
        conn.commit()
    finally:
        conn.close()


def get_active_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT version FROM versions WHERE status='active' LIMIT 1").fetchone()
    return row["version"] if row else None
