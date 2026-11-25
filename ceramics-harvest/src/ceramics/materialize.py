"""更正单物化：从本地整版 + 已确认更正页沿谱系还原目标版视图。

设计要点：
- 整版直接读 sherds/joins（激活时已落库）；更正单不落全量数据，只保留原始更正页，
  查询时沿 edition_lineage 回溯到本地整版，再逐版套用更正操作实时物化，
  因此传输页数与落库记录数只随更正规模增长。
- 漏过中间版无需重新下载：只要谱系上的每个更正版页面已确认、基版整版本地存在，
  整条链都可在本地补齐。
- 每个目标版物化后都做拼接引用完整性校验，问题更正单在采集端激活前即被拦截。
"""
from __future__ import annotations

import sqlite3

from ceramics.parsing import parse_page
from ceramics.protocol import edge_key


class MaterializeError(Exception):
    """本地无法物化目标版（基版缺失、页面不全、更正非法等）。"""


def _load_full(conn: sqlite3.Connection, version: str) -> tuple[dict, dict]:
    sherds = {r["catalog_no"]: dict(r) for r in conn.execute(
        "SELECT catalog_no, museum, material, weight_g FROM sherds WHERE version=?",
        (version,))}
    joins = {r["edge_key"]: dict(r) for r in conn.execute(
        "SELECT edge_key, a, b, bond, confidence FROM joins WHERE version=?",
        (version,))}
    if not sherds and not joins:
        # 整版尚未激活或本地没有该版数据
        raise MaterializeError(f"基版 {version} 的整版数据本地不存在，无法从本地补齐")
    return sherds, joins


def _load_corrections(conn: sqlite3.Connection, target: str) -> tuple[list[dict], list[dict]]:
    reg_pages = conn.execute(
        "SELECT * FROM pages WHERE version=? AND entry='registry' ORDER BY page_index",
        (target,)).fetchall()
    join_pages = conn.execute(
        "SELECT * FROM pages WHERE version=? AND entry='joins' ORDER BY page_index",
        (target,)).fetchall()
    if not reg_pages or not join_pages:
        raise MaterializeError(f"更正版 {target} 的更正页未确认保存完整")
    reg_ops, join_ops = [], []
    for pages, bucket, entry in ((reg_pages, reg_ops, "registry"),
                                 (join_pages, join_ops, "joins")):
        expected = pages[0]["total_pages"]
        if expected == 0:
            continue  # 该入口本次没有任何更正操作（0 页合法）
        if len(pages) != expected or [p["page_index"] for p in pages] != list(range(expected)):
            raise MaterializeError(f"更正版 {target} 的 {entry} 更正页不全或不连续")
        for p in pages:
            bucket.extend(parse_page(entry, p["layout"], p["body"], kind="correction"))
    return reg_ops, join_ops


def _base_of(conn: sqlite3.Connection, current: str) -> str | None:
    """查某版的基版：先看已登记谱系，再看激活前试算用的临时谱系。"""
    row = conn.execute(
        "SELECT base_version FROM edition_lineage WHERE target_version=?",
        (current,)).fetchone()
    if row is not None:
        return row["base_version"]
    try:
        row = conn.execute(
            "SELECT base_version FROM temp._pending_lineage WHERE target_version=?",
            (current,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["base_version"] if row is not None else None


def _lineage_chain(conn: sqlite3.Connection, target: str) -> tuple[list[str], str]:
    """返回 (更正版序列, 谱系终点整版号)；序列含目标版，不含整版基版，按目标->基版排列。"""
    chain, current, seen = [], target, set()
    while True:
        if current in seen:
            raise MaterializeError(f"更正谱系存在环: {target}")
        seen.add(current)
        base = _base_of(conn, current)
        if base is None:
            # 谱系终点：必须是本地存在的整版
            return chain, current
        chain.append(current)
        current = base


def _apply_registry_ops(sherds: dict, ops: list[dict], version: str) -> None:
    for op in ops:
        no = op["catalog_no"]
        if op["op"] == "delete":
            if no not in sherds:
                raise MaterializeError(f"{version} 登记更正删除了不存在的陶片 {no}")
            del sherds[no]
        elif op["op"] == "add":
            if no in sherds:
                raise MaterializeError(f"{version} 登记更正新增的陶片已存在: {no}")
            sherds[no] = {"catalog_no": no, "museum": op["museum"],
                          "material": op["material"], "weight_g": op["weight_g"]}
        else:  # upsert
            if no not in sherds:
                raise MaterializeError(f"{version} 登记更正改写了不存在的陶片 {no}")
            sherds[no] = {"catalog_no": no, "museum": op["museum"],
                          "material": op["material"], "weight_g": op["weight_g"]}


def _apply_join_ops(joins: dict, ops: list[dict], version: str) -> None:
    for op in ops:
        key = edge_key(op["a"], op["b"])
        if op["op"] == "delete":
            if key not in joins:
                raise MaterializeError(f"{version} 拼接更正删除了不存在的边 {key}")
            del joins[key]
        elif op["op"] == "add":
            if key in joins:
                raise MaterializeError(f"{version} 拼接更正新增的边已存在: {key}")
            joins[key] = {"edge_key": key, "a": op["a"], "b": op["b"],
                          "bond": op["bond"], "confidence": op["confidence"]}
        else:  # upsert
            if key not in joins:
                raise MaterializeError(f"{version} 拼接更正改写了不存在的边 {key}")
            joins[key] = {"edge_key": key, "a": op["a"], "b": op["b"],
                          "bond": op["bond"], "confidence": op["confidence"]}


def materialize(conn: sqlite3.Connection, target: str) -> tuple[list[dict], list[dict]]:
    """把目标版物化为 (登记记录列表, 拼接记录列表)，并做引用完整性校验。"""
    chain, base = _lineage_chain(conn, target)
    sherds, joins = _load_full(conn, base)
    # 从最靠近基版的更正版开始依次套用
    for version in reversed(chain):
        reg_ops, join_ops = _load_corrections(conn, version)
        _apply_registry_ops(sherds, reg_ops, version)
        _apply_join_ops(joins, join_ops, version)
    catalogs = set(sherds)
    for key, j in joins.items():
        if j["a"] not in catalogs or j["b"] not in catalogs:
            raise MaterializeError(
                f"目标版 {target} 拼接引用了登记中不存在的陶片: {key}")
    registry_rows = [sherds[k] for k in sorted(sherds)]
    join_rows = [joins[k] for k in sorted(joins)]
    return registry_rows, join_rows


def dry_run(conn: sqlite3.Connection, target: str, base: str) -> tuple[list[dict], list[dict]]:
    """激活前试算：把 target->base 作为临时谱系纳入完整物化链，不落库任何数据。

    基版本身也可以是更正版（链式更正）：临时谱系对 _lineage_chain 可见，
    因此会沿 v5 整版 -> ... -> 基版 -> 目标版整条链本地补齐。
    """
    if _base_of(conn, target) is not None:
        raise MaterializeError(f"目标版 {target} 已存在谱系记录")
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _pending_lineage"
        "(target_version TEXT PRIMARY KEY, base_version TEXT NOT NULL)")
    conn.execute("DELETE FROM temp._pending_lineage")
    try:
        conn.execute("INSERT INTO temp._pending_lineage VALUES (?, ?)", (target, base))
        return materialize(conn, target)
    finally:
        conn.execute("DELETE FROM temp._pending_lineage")
