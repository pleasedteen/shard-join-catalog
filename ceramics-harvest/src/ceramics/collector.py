"""采集服务：按会话协议抓取两个入口，校验后按发布版原子激活。

支持两种发布形态：
- 整版（kind=full）：登记/拼接全量页面，激活时解析写入 sherds/joins。
- 更正单（kind=correction）：只携带 add/upsert/delete 变化记录，页面按目标版编号
  落盘；激活时只登记更正谱系（edition_lineage），查询时从本地基版整版 + 已确认
  更正页实时物化，传输页数与落库记录数只随更正规模增长。

关键保证：
- 503 暂停：按 Retry-After 等待后用*同一令牌*重试（暂停不消耗游标）。
- 401 过期 / 令牌跨会话：重新登录，并按已确认保存页数续走；已确认页面
  校验和一致即跳过，实现进程重启后的断点续采（更正页同样持久化、同样可续采）。
- 两入口的清单必须到达*同一目标版*且引用完整（更正单还要基版本地可补齐），
  才在一个事务里切换 active；采集与补采期间查询继续命中旧 active 版本。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from ceramics.db import connect, get_active_version, init_db
from ceramics.materialize import MaterializeError, dry_run
from ceramics.parsing import parse_page
from ceramics.protocol import edge_key, page_checksum

ENTRIES = ("registry", "joins")
MAX_SESSION_RESTARTS = 100


class SiteError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class SessionReset(Exception):
    """会话失效，需要重新登录并从第一页重走。"""


class SiteClient:
    def __init__(self, base_url: str, key: str = "ceramics-local-control",
                 pause_cap: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.pause_cap = pause_cap
        self.sid: str | None = None
        self.secret: str | None = None
        self.token: str | None = None

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None) -> dict:
        url = self.base_url + path
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            payload = {}
            try:
                payload = json.loads(exc.read().decode())
            except Exception:
                pass
            if exc.code == 503:
                retry = int(exc.headers.get("Retry-After", 2))
                raise SiteError("paused", payload.get("message", "站点暂停"),
                                retry_after=min(retry, self.pause_cap))
            if exc.code == 401:
                raise SessionReset(payload.get("message", "会话过期"))
            if exc.code == 403:
                # 签名/令牌不属于当前会话：重新登录拿全新令牌链
                raise SessionReset(payload.get("message", "签名或令牌被拒"))
            if exc.code == 409:
                raise SessionReset(payload.get("message", "令牌顺序错乱"))
            raise SiteError("http", f"HTTP {exc.code}: {payload.get('message', exc)}")
        except urllib.error.URLError as exc:
            raise SiteError("network", f"无法连接模拟站: {exc}")

    def login(self, resume: int = 0) -> None:
        # 暂停期登录会收到 503：等待恢复后重试；恢复后拿到的是全新会话与令牌链。
        # resume 报告该入口已确认保存的页数，站点从该游标签发*新会话*的首令牌，
        # 因此无需复用任何旧令牌。
        while True:
            try:
                body = f"key={self.key}&resume={int(resume)}".encode()
                data = self._request("POST", "/session", body,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
                self.sid = data["sid"]
                self.secret = data["secret"]
                self.token = data["first_token"]
                return
            except SiteError as exc:
                if exc.kind == "paused":
                    print(f"  · 站点暂停受理，{exc.retry_after or 2}s 后重试登录")
                    time.sleep(exc.retry_after or 2)
                    continue
                raise

    def fetch_page(self, entry: str) -> dict:
        """取当前令牌对应的一页；暂停时原地等待重试，其它认证问题向上抛 SessionReset。"""
        path = f"/{entry}"
        while True:
            from ceramics.protocol import sign
            signature = sign(self.secret, "GET", path, self.sid, self.token)
            try:
                page = self._request("GET", path, headers={
                    "X-Ceramic-Session": self.sid,
                    "X-Ceramic-Token": self.token,
                    "X-Ceramic-Signature": signature,
                })
            except SiteError as exc:
                if exc.kind == "paused":
                    time.sleep(exc.retry_after or 2)
                    continue
                raise
            self.token = page["next_token"]
            return page


def _record_failure(conn: sqlite3.Connection, version: str, stage: str,
                    reason: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO version_failures(version, stage, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (version, stage, reason, detail, time.time()))
    # 已激活的完整版本不因新一轮采集失败而降级，查询必须继续可用
    conn.execute("UPDATE versions SET status='failed', updated_at=? "
                 "WHERE version=? AND status!='active'",
                 (time.time(), version))
    conn.commit()


def _ensure_version_row(conn: sqlite3.Connection, version: str, published_at: str,
                        note: str, kind: str = "full",
                        base_version: str | None = None) -> None:
    conn.execute(
        "INSERT INTO versions(version, status, kind, base_version, published_at, note, updated_at) "
        "VALUES (?, 'collecting', ?, ?, ?, ?, ?) "
        "ON CONFLICT(version) DO UPDATE SET updated_at=excluded.updated_at",
        (version, kind, base_version, published_at, note, time.time()))
    conn.commit()


def _saved_page_count(conn: sqlite3.Connection, entry: str, version: str | None) -> int:
    """该入口在指定版本下已确认保存的连续页数（从 0 起）。"""
    if not version:
        return 0
    rows = conn.execute(
        "SELECT page_index FROM pages WHERE version=? AND entry=? ORDER BY page_index",
        (version, entry)).fetchall()
    indexes = {r["page_index"] for r in rows}
    count = 0
    while count in indexes:
        count += 1
    return count


def walk_entry(client: SiteClient, conn: sqlite3.Connection, entry: str) -> dict:
    """完整抓取一个入口，返回该入口当前清单的描述符；失败时记录原因并抛异常。

    返回 {"kind", "target", "base"}：
    - 整版 kind=full，target 即发布版号，base=None；
    - 更正单 kind=correction，target 为目标版号，base 为基版号，页面按 target 落盘。

    续采策略（整版与更正单一致）：
    - 版本未知时用探针会话取第 0 页确认发布形态并落盘；
    - 之后按该目标版已确认保存的页数作为游标登录，站点为新会话从该游标签发新令牌；
    - 续采中会话失效只按当前已保存页数重登重试，不重新探针、不复用旧令牌、不重复抓页。
    """
    restarts = 0
    kind: str | None = None
    target: str | None = None
    base: str | None = None
    total_pages = 0
    while True:
        if restarts > MAX_SESSION_RESTARTS:
            raise SiteError("restart_limit", "会话重建次数超限")
        try:
            if target is None:
                # 第一阶段：探针会话取第 0 页，确定入口当前发布形态
                client.login(resume=0)
                probe = client.fetch_page(entry)
                if probe["page_index"] != 0:
                    raise SiteError("protocol", "探针页序号应为 0")
                kind = probe.get("kind", "full")
                if kind == "correction":
                    target = probe["target_version"]
                    base = probe["base_version"]
                else:
                    target = probe["version"]
                total_pages = probe["total_pages"]
                _ensure_version_row(conn, target, probe["published_at"], "",
                                    kind=kind, base_version=base)
                _save_or_verify_page(conn, entry, probe)

            # 第二阶段：按已保存页数续采；失效重登后也从这里继续，无需重新探针
            resume = _saved_page_count(conn, entry, target)
            if total_pages == 0:
                row = conn.execute(
                    "SELECT MIN(total_pages) t FROM pages WHERE version=? AND entry=?",
                    (target, entry)).fetchone()
                total_pages = row["t"] or 0
            if resume >= total_pages:
                print(f"  · {entry} 目标版 {target} 的 {total_pages} 页均已确认，整入口跳过")
                return {"kind": kind, "target": target, "base": base}
            client.login(resume=resume)
            page_index = resume
            while True:
                page = client.fetch_page(entry)
                page_kind = page.get("kind", "full")
                page_target = page["target_version"] if page_kind == "correction" \
                    else page["version"]
                if page_target != target or page_kind != kind:
                    # 采集中入口切版（整版/更正单互换也算）：本轮作废，下一轮重新探针
                    raise SiteError(
                        "entry_switch",
                        f"入口 {entry} 采集中从 {kind}:{target} 切到 "
                        f"{page_kind}:{page_target}，本轮作废")
                if page_kind == "correction" and page.get("base_version") != base:
                    raise SiteError(
                        "protocol",
                        f"更正单 {target} 续采中基版声明变化：{base} -> "
                        f"{page.get('base_version')}")
                if page["page_index"] != page_index:
                    raise SiteError("protocol",
                                    f"页序号不连续：期望 {page_index}，实际 {page['page_index']}")
                _save_or_verify_page(conn, entry, page)
                page_index += 1
                if page_index >= total_pages:
                    return {"kind": kind, "target": target, "base": base}
        except SessionReset as exc:
            restarts += 1
            print(f"  ! 会话失效（{exc}），按已确认保存页数重新登录后续采，不重复抓取")
            continue
        except SiteError as exc:
            if target:
                _record_failure(conn, target, f"walk:{entry}", exc.kind, str(exc))
            raise


def _save_or_verify_page(conn: sqlite3.Connection, entry: str, page: dict) -> None:
    """校验单页并落盘；已存在且校验和一致则跳过，不一致则报错。

    整版按发布版号落盘；更正单按 target_version 落盘并登记 kind/base_version，
    两种形态共用同一套断点续采逻辑。
    """
    page_index = page["page_index"]
    kind = page.get("kind", "full")
    if kind == "correction":
        version = page["target_version"]
        base = page.get("base_version")
        target = page["target_version"]
    else:
        version = page["version"]
        base = None
        target = None
    if page.get("corrupted"):
        raise SiteError("corrupt_page",
                        f"{entry} 第 {page_index} 页被站点标记为损坏")
    try:
        records = parse_page(entry, page["layout"], page["body"], kind=kind)
    except ValueError as exc:
        raise SiteError("parse_error", f"{entry} 第 {page_index} 页解析失败: {exc}")
    if page_checksum(records) != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry} 第 {page_index} 页校验和不符，内容不可用")
    saved = conn.execute(
        "SELECT checksum FROM pages WHERE version=? AND entry=? AND page_index=?",
        (version, entry, page_index)).fetchone()
    if saved is None:
        conn.execute(
            "INSERT INTO pages(version, entry, page_index, total_pages, kind, base_version,"
            " target_version, layout, checksum, body, saved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version, entry, page_index, page["total_pages"], kind, base, target,
             page["layout"], page["checksum"], page["body"], time.time()))
        conn.commit()
    elif saved["checksum"] != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry} 第 {page_index} 页与已保存内容不一致")
    else:
        print(f"  · 跳过已确认保存的 {entry} 第 {page_index} 页（断点续采）")


def validate_version(conn: sqlite3.Connection, version: str) -> tuple[bool, str]:
    """整版校验：页数齐全 + 可解析 + 登记唯一 + 拼接引用完整性。"""
    for entry in ENTRIES:
        pages = conn.execute(
            "SELECT * FROM pages WHERE version=? AND entry=? ORDER BY page_index",
            (version, entry)).fetchall()
        if not pages:
            return False, f"{entry} 没有任何已保存页面"
        expected = pages[0]["total_pages"]
        if len(pages) != expected:
            return False, f"{entry} 页数不全：{len(pages)}/{expected}"
        if [p["page_index"] for p in pages] != list(range(expected)):
            return False, f"{entry} 页序号不连续"
        for p in pages:
            try:
                parse_page(entry, p["layout"], p["body"])
            except ValueError as exc:
                return False, f"{entry} 第 {p['page_index']} 页不可解析: {exc}"

    registry_rows = []
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND entry='registry' "
                          "ORDER BY page_index", (version,)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    catalogs = [r["catalog_no"] for r in registry_rows]
    if len(set(catalogs)) != len(catalogs):
        return False, "登记清单存在重复馆藏编号"

    join_rows = []
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND entry='joins' "
                          "ORDER BY page_index", (version,)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))
    catalog_set = set(catalogs)
    for j in join_rows:
        for side in (j["a"], j["b"]):
            if side not in catalog_set:
                return False, f"拼接清单引用了登记中不存在的陶片: {side}"
    return True, ""


def activate_version(conn: sqlite3.Connection, version: str) -> None:
    ok, reason = validate_version(conn, version)
    if not ok:
        _record_failure(conn, version, "activate", "validation_failed", reason)
        raise SiteError("validation_failed", reason)

    registry_rows, join_rows = [], []
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND entry='registry' "
                          "ORDER BY page_index", (version,)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND entry='joins' "
                          "ORDER BY page_index", (version,)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))

    published = conn.execute("SELECT published_at FROM versions WHERE version=?",
                             (version,)).fetchone()
    # 单事务切换：任何一步失败，旧 active 保持不变
    with conn:
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


def validate_correction(conn: sqlite3.Connection, target: str, base: str) -> tuple[bool, str]:
    """更正单激活前校验：更正页齐全可解析，且能从本地基版完整物化目标版。"""
    for entry in ENTRIES:
        pages = conn.execute(
            "SELECT * FROM pages WHERE version=? AND entry=? ORDER BY page_index",
            (target, entry)).fetchall()
        if not pages:
            return False, f"{entry} 没有任何已确认的更正页"
        expected = pages[0]["total_pages"]
        if expected == 0:
            continue  # 该入口本次无更正操作，0 页合法
        if len(pages) != expected:
            return False, f"{entry} 更正页不全：{len(pages)}/{expected}"
        if [p["page_index"] for p in pages] != list(range(expected)):
            return False, f"{entry} 更正页序号不连续"
        for p in pages:
            try:
                parse_page(entry, p["layout"], p["body"], kind="correction")
            except ValueError as exc:
                return False, f"{entry} 第 {p['page_index']} 更正页不可解析: {exc}"
    try:
        dry_run(conn, target, base)
    except MaterializeError as exc:
        return False, str(exc)
    return True, ""


def activate_correction(conn: sqlite3.Connection, target: str, base: str) -> None:
    """激活更正单：只登记更正谱系并切换 active，不落全量登记/拼接数据。

    目标版的查询视图由 materialize 在读取时沿谱系从本地整版实时物化，
    因此漏过中间版也能从本地已确认的整版与更正页补齐。
    """
    ok, reason = validate_correction(conn, target, base)
    if not ok:
        _record_failure(conn, target, "activate", "validation_failed", reason)
        raise SiteError("validation_failed", reason)

    published = conn.execute("SELECT published_at FROM versions WHERE version=?",
                             (target,)).fetchone()
    # 单事务切换：任何一步失败，旧 active 保持不变
    with conn:
        conn.execute(
            "INSERT INTO edition_lineage(target_version, base_version, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(target_version) DO UPDATE SET "
            "base_version=excluded.base_version, created_at=excluded.created_at",
            (target, base, time.time()))
        conn.execute("UPDATE versions SET status='complete', updated_at=? WHERE status='active'",
                     (time.time(),))
        conn.execute("UPDATE versions SET status='active', kind='correction', base_version=?,"
                     " published_at=?, updated_at=? WHERE version=?",
                     (base, published["published_at"] if published else None,
                      time.time(), target))
        conn.execute("INSERT INTO activation_log(version, activated_at) VALUES (?, ?)",
                     (target, time.time()))


def run_round(base_url: str, db_path: str) -> dict:
    init_db(db_path)
    conn = connect(db_path)
    client = SiteClient(base_url)
    summary = {"versions_seen": [], "activated": None, "error": None}
    try:
        seen = {}
        for entry in ENTRIES:
            seen[entry] = walk_entry(client, conn, entry)
        reg, jn = seen["registry"], seen["joins"]
        # 两份清单必须到达同一目标版、同为整版或同为更正单（更正单基版也要一致）
        if reg != jn:
            # 两入口错时：不标记失败、不激活，下一轮再看；查询保持旧版
            summary["error"] = (f"两个入口清单未到达同一目标版：registry={reg}, joins={jn}，"
                                "等待两份清单同版且引用完整后再激活")
            return summary
        kind, target, base = reg["kind"], reg["target"], reg["base"]
        summary["versions_seen"] = [target]
        if get_active_version(conn) == target:
            print(f"  · 目标版 {target} 已是当前查询版本，重复轮次直接跳过")
            summary["activated"] = target
            return summary
        if kind == "correction":
            activate_correction(conn, target, base)
        else:
            activate_version(conn, target)
        summary["activated"] = target
        return summary
    except SiteError as exc:
        summary["error"] = f"{exc.kind}: {exc}"
        return summary
    finally:
        conn.close()


def print_status(db_path: str) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        active = get_active_version(conn)
        print(f"当前查询版本(active): {active or '（尚无可用版本）'}")
        print("\n发布版状态:")
        for v in conn.execute(
                "SELECT version, status, kind, base_version, published_at, note "
                "FROM versions ORDER BY version"):
            origin = (f"更正单(基版 {v['base_version']})" if v["kind"] == "correction"
                      else "整版")
            print(f"  {v['version']:<10} {v['status']:<10} {origin:<16} "
                  f"{v['published_at'] or ''}  {v['note'] or ''}")
        print("\n已保存页面:")
        for p in conn.execute(
                "SELECT version, entry, kind, COUNT(*) n, MIN(total_pages) total "
                "FROM pages GROUP BY version, entry ORDER BY version, entry"):
            tag = "更正页" if p["kind"] == "correction" else "整版页"
            print(f"  {p['version']:<10} {p['entry']:<9} {tag} {p['n']}/{p['total']} 页")
        lineage = conn.execute(
            "SELECT target_version, base_version FROM edition_lineage ORDER BY target_version"
        ).fetchall()
        print("\n更正谱系（目标版 <- 基版）:")
        if not lineage:
            print("  （无）")
        for r in lineage:
            print(f"  {r['target_version']} <- {r['base_version']}")
        print("\n失败记录（操作员可见的失败原因）:")
        fails = conn.execute("SELECT version, stage, reason, detail, created_at "
                             "FROM version_failures ORDER BY id DESC LIMIT 10").fetchall()
        if not fails:
            print("  （无）")
        for f in fails:
            print(f"  {f['version']:<8} [{f['stage']}] {f['reason']} — {f['detail']}")
        if active:
            rev = conn.execute("SELECT edge_key FROM revocations WHERE version=?",
                               (active,)).fetchall()
            print(f"\n当前版本已撤销拼接 {len(rev)} 条: "
                  f"{', '.join(r['edge_key'] for r in rev) or '（无）'}")
    finally:
        conn.close()


def main() -> None:
    # 管道/nohup 下默认块缓冲，持续采集时日志无法实时可见，改为行缓冲
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="陶片清单采集服务")
    sub = ap.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run", help="执行采集轮次（默认一轮）")
    runp.add_argument("--base-url", default="http://127.0.0.1:8080")
    runp.add_argument("--db", default="./ceramics.db")
    runp.add_argument("--rounds", type=int, default=1, help="采集轮数，0 表示持续运行")
    runp.add_argument("--interval", type=float, default=3.0, help="持续运行时轮间秒数")

    stat = sub.add_parser("status", help="查看采集与版本状态")
    stat.add_argument("--db", default="./ceramics.db")
    args = ap.parse_args()

    if args.cmd == "status":
        print_status(args.db)
        return

    rounds_done = 0
    while True:
        rounds_done += 1
        print(f"[采集] 第 {rounds_done} 轮开始 {time.strftime('%H:%M:%S')}")
        result = run_round(args.base_url, args.db)
        if result["activated"]:
            print(f"[采集] 发布版 {result['activated']} 已激活，查询已切换")
        elif result["error"]:
            print(f"[采集] 本轮未切换版本：{result['error']}")
            print("       查询继续使用上一个完整版本，详见 status 失败记录")
        else:
            print("[采集] 本轮无新版本变化")
        if args.rounds and rounds_done >= args.rounds:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
