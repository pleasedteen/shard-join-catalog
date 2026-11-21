"""SQLite 持久层：采集页、发布版状态、登记/拼接数据、撤销记录。

多馆模型要点：
- 发布版按馆隔离：versions 的主键是 (site, version)，同一版号算法在不同馆各自独立生效，
  不要求跨馆同版，也不需要跨馆同版才能切换。
- pages 按 (site, version, entry, page_index) 唯一，断点续采按馆按版计算游标。
- 跨馆拼接不在采集时合并进某一张表，而是由查询端根据各馆当前 active 版本里的
  跨馆登记实时配对：两端登记都在各自已发布清单中才生效，任一端撤下立即失效。
  这样晚到的跨馆登记无需重采另一端，撤下也无需额外清理。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS versions(
    site         TEXT NOT NULL,
    version      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK(status IN ('collecting','complete','active','failed')),
    published_at TEXT,
    note         TEXT,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (site, version)
);

-- 已确认保存的原始页面；(site, version, entry, page_index) 唯一，
-- 进程重启后采集端据此跳过已确认页面继续采集。
CREATE TABLE IF NOT EXISTS pages(
    site       TEXT NOT NULL,
    version    TEXT NOT NULL,
    entry      TEXT NOT NULL CHECK(entry IN ('registry','joins')),
    page_index INTEGER NOT NULL,
    total_pages INTEGER NOT NULL,
    layout     TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    body       TEXT NOT NULL,
    saved_at   REAL NOT NULL,
    PRIMARY KEY (site, version, entry, page_index)
);

CREATE TABLE IF NOT EXISTS version_failures(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    site      TEXT NOT NULL,
    version   TEXT NOT NULL,
    stage     TEXT NOT NULL,
    reason    TEXT NOT NULL,
    detail    TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sherds(
    site       TEXT NOT NULL,
    version    TEXT NOT NULL,
    catalog_no TEXT NOT NULL,
    museum     TEXT,
    material   TEXT,
    weight_g   REAL,
    PRIMARY KEY (site, version, catalog_no)
);

-- 馆内拼接
CREATE TABLE IF NOT EXISTS joins(
    site     TEXT NOT NULL,
    version  TEXT NOT NULL,
    edge_key TEXT NOT NULL,
    a        TEXT NOT NULL,
    b        TEXT NOT NULL,
    bond     TEXT,
    confidence TEXT,
    PRIMARY KEY (site, version, edge_key)
);

-- 跨馆拼接不单独建表：它由各馆当前 active 版本清单里的跨馆登记实时配对得出，
-- 见 cross.py。这样晚到的一端无需重采另一端，任一端撤下也立即失效。
CREATE TABLE IF NOT EXISTS revocations(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    site      TEXT NOT NULL,
    version   TEXT NOT NULL,
    edge_key  TEXT NOT NULL,
    reason    TEXT,
    revoked_at REAL NOT NULL,
    UNIQUE (site, version, edge_key)
);

-- 跨馆拼接的撤下登记：任一端撤下，整条跨馆拼接立即退出查询结果。
CREATE TABLE IF NOT EXISTS cross_revocations(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_key  TEXT NOT NULL,
    side      TEXT NOT NULL,
    reason    TEXT,
    revoked_at REAL NOT NULL,
    UNIQUE (edge_key, side)
);

CREATE TABLE IF NOT EXISTS activation_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    version TEXT NOT NULL,
    activated_at REAL NOT NULL
);
"""

# 从旧单馆库升级：旧表缺少 site 列时整体迁移
LEGACY_TABLES = ("versions", "pages", "version_failures", "sherds", "joins",
                 "revocations", "activation_log")


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把旧的单馆表原地升级为多馆结构：补 site 列、主键改为 (site, ...)。"""
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = [t for t in LEGACY_TABLES
              if t in existing and "site" not in _columns(conn, t)]
    if not legacy:
        return
    site = "legacy"
    with conn:
        for table in legacy:
            conn.execute(f"ALTER TABLE {table} RENAME TO _{table}_old")
        conn.executescript(SCHEMA)
        for table in legacy:
            cols = _columns(conn, f"_{table}_old")
            shared = ", ".join(sorted(cols))
            conn.execute(
                f"INSERT INTO {table}(site, {shared}) SELECT ?, {shared} "
                f"FROM _{table}_old", (site,))
            conn.execute(f"DROP TABLE _{table}_old")


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        _migrate_legacy(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_active_version(conn: sqlite3.Connection, site: str | None = None) -> str | None:
    if site is None:
        row = conn.execute(
            "SELECT version FROM versions WHERE status='active' ORDER BY site LIMIT 1"
        ).fetchone()
        return row["version"] if row else None
    row = conn.execute("SELECT version FROM versions WHERE site=? AND status='active'",
                       (site,)).fetchone()
    return row["version"] if row else None


def active_versions(conn: sqlite3.Connection) -> dict[str, str]:
    """各馆当前生效发布版；未激活的馆不出现在结果里。"""
    return {r["site"]: r["version"] for r in conn.execute(
        "SELECT site, version FROM versions WHERE status='active' ORDER BY site")}


def all_sites(conn: sqlite3.Connection) -> list[str]:
    """出现过的馆：已激活的优先，其次是有页面或失败记录的馆。"""
    sites = {r["site"] for r in conn.execute("SELECT site FROM versions")}
    sites |= {r["site"] for r in conn.execute("SELECT site FROM pages")}
    return sorted(sites)
