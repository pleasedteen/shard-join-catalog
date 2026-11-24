"""发布服务：只接手采集端登记的收齐清单，整版校验后原子切换 active。

与采集端完全解耦的独立入口（可单独启动、单独重启，不访问站点）：
- 数据源只有采集端在同一轮确认收齐后写入 ready_rounds 的同版清单；
  清单之外的页面本进程一律不发布。
- 切换前对整版重新做完整校验（页数/解析/唯一性/引用完整性），任何一步
  失败只把该清单标记 failed 并记录原因，旧 active 原样保留。
- 切换在单个 BEGIN IMMEDIATE 事务内完成；同一版本只要已在 activation_log
  出现过就绝不再次切换——并发两个发布进程、或发布进程中断后重启，同一版
  都只会成功切换一次。
- 校验与切换期间采集端仍可写入后续版本的页面与清单，查询服务在事务提交
  前始终读到旧 active（SQLite WAL 快照），提交后才看到新版。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

from ceramics.collector import validate_version
from ceramics.db import connect, get_active_version, init_db
from ceramics.parsing import parse_page
from ceramics.protocol import edge_key


def _record_failure(conn: sqlite3.Connection, version: str, stage: str,
                    reason: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO version_failures(version, stage, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (version, stage, reason, detail, time.time()))
    conn.execute("UPDATE versions SET status='failed', updated_at=? "
                 "WHERE version=? AND status!='active'", (time.time(), version))
    conn.commit()


def already_activated(conn: sqlite3.Connection, version: str) -> bool:
    """同一版只成功切换一次：activation_log 里出现过即视为已发布。"""
    return conn.execute(
        "SELECT 1 FROM activation_log WHERE version=? LIMIT 1", (version,)).fetchone() is not None


def pending_rounds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ready_rounds WHERE switch_status='pending' ORDER BY id").fetchall()


def switch_version(conn: sqlite3.Connection, round_row: sqlite3.Row) -> tuple[bool, str]:
    """对一条收齐清单做整版校验并原子切换。返回 (是否完成切换, 说明)。

    已切换过的版本直接跳过，保证同一版只成功切换一次。
    """
    version = round_row["version"]
    rid = round_row["id"]
    if already_activated(conn, version):
        conn.execute(
            "UPDATE ready_rounds SET switch_status='switched', switched_at=? "
            "WHERE id=? AND switch_status='pending'", (time.time(), rid))
        conn.commit()
        return False, f"发布版 {version} 已切换过，跳过（同一版只切换一次）"

    ok, reason = validate_version(conn, version)
    if not ok:
        _record_failure(conn, version, "publish", "validation_failed", reason)
        conn.execute("UPDATE ready_rounds SET switch_status='failed' WHERE id=?", (rid,))
        conn.commit()
        return False, f"发布版 {version} 整版校验未通过：{reason}"

    registry_rows, join_rows = [], []
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND entry='registry' "
                          "ORDER BY page_index", (version,)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND entry='joins' "
                          "ORDER BY page_index", (version,)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))
    published = conn.execute("SELECT published_at FROM versions WHERE version=?",
                             (version,)).fetchone()

    # 单事务原子切换：BEGIN IMMEDIATE 立即拿写锁，并发/重启的第二个发布进程
    # 会在锁上等待，提交后重查 activation_log 即跳过；任何一步失败旧版不变。
    conn.execute("BEGIN IMMEDIATE")
    try:
        if already_activated(conn, version):
            conn.rollback()
            conn.execute(
                "UPDATE ready_rounds SET switch_status='switched', switched_at=? "
                "WHERE id=? AND switch_status='pending'", (time.time(), rid))
            conn.commit()
            return False, f"发布版 {version} 已被其它发布进程切换，跳过"
        conn.execute("DELETE FROM sherds WHERE version=?", (version,))
        conn.execute("DELETE FROM joins WHERE version=?", (version,))
        conn.executemany(
            "INSERT INTO sherds(version, catalog_no, museum, material, weight_g) "
            "VALUES (?, ?, ?, ?, ?)",
            [(version, r["catalog_no"], r["museum"], r["material"], r["weight_g"])
             for r in registry_rows])
        conn.executemany(
            "INSERT INTO joins(version, edge_key, a, b, bond, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(version, edge_key(r["a"], r["b"]), r["a"], r["b"], r["bond"], r["confidence"])
             for r in join_rows])
        conn.execute("UPDATE versions SET status='complete', updated_at=? WHERE status='active'",
                     (time.time(),))
        conn.execute("UPDATE versions SET status='active', published_at=?, updated_at=? "
                     "WHERE version=?",
                     (published["published_at"] if published else None, time.time(), version))
        conn.execute("INSERT INTO activation_log(version, activated_at) VALUES (?, ?)",
                     (version, time.time()))
        conn.execute("UPDATE ready_rounds SET switch_status='switched', switched_at=? "
                     "WHERE id=?", (time.time(), rid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True, f"发布版 {version} 已原子切换，查询版本已更新"


def publish_once(db_path: str, version: str | None = None) -> dict:
    """处理一轮待发布清单；指定 version 时只处理该版本的最新 pending 清单。"""
    init_db(db_path)
    conn = connect(db_path)
    summary = {"switched": [], "skipped": [], "error": None}
    try:
        rounds = pending_rounds(conn)
        if version is not None:
            rounds = [r for r in rounds if r["version"] == version]
        if not rounds:
            summary["error"] = "没有待发布的收齐清单（等待采集端确认同版收齐）"
            return summary
        for row in rounds:
            try:
                switched, msg = switch_version(conn, row)
            except sqlite3.OperationalError as exc:
                # 写锁竞争：交给下一轮/重启后的发布进程重试，清单仍是 pending
                summary["error"] = f"发布锁竞争，留待重试: {exc}"
                return summary
            (summary["switched"] if switched else summary["skipped"]).append(
                {"version": row["version"], "message": msg})
            # 仅本地验证用：CERAMICS_PUBLISH_PAUSE 在每处理完一条清单后暂停，
            # 用于确定性制造"发布进程中断、剩余清单仍 pending"的现场
            pause = os.environ.get("CERAMICS_PUBLISH_PAUSE")
            if pause:
                time.sleep(float(pause))
        return summary
    finally:
        conn.close()


def run_publisher(db_path: str, interval: float, once: bool = False,
                  version: str | None = None) -> None:
    """持续发布循环：只盯 ready_rounds，不访问站点；中断重启后自动续处理。"""
    while True:
        result = publish_once(db_path, version)
        for item in result["switched"]:
            print(f"[发布] {item['message']}")
        for item in result["skipped"]:
            print(f"[发布] {item['message']}")
        if result["error"]:
            print(f"[发布] {result['error']}")
        conn = connect(db_path)
        try:
            active = get_active_version(conn)
        finally:
            conn.close()
        print(f"[发布] 当前查询版本(active): {active or '（尚无可用版本）'} "
              f"{time.strftime('%H:%M:%S')}")
        if once:
            return
        time.sleep(interval)


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="陶片清单发布服务（独立于采集，只发布收齐清单）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    once_p = sub.add_parser("once", help="处理当前全部待发布清单后退出")
    once_p.add_argument("--db", default="./ceramics.db")
    once_p.add_argument("--version", default=None, help="只发布指定版本的清单")
    run_p = sub.add_parser("run", help="持续轮询待发布清单并切换")
    run_p.add_argument("--db", default="./ceramics.db")
    run_p.add_argument("--interval", type=float, default=2.0)
    run_p.add_argument("--version", default=None, help="只发布指定版本（持续模式）")
    args = ap.parse_args()

    if args.cmd == "once":
        run_publisher(args.db, interval=0, once=True, version=args.version)
    else:
        run_publisher(args.db, interval=args.interval, once=False, version=args.version)


if __name__ == "__main__":
    main()
