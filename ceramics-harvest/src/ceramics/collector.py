"""采集服务：一次启动并发采集多个合作馆，各馆独立抓取、校验并按发布版原子激活。

关键保证：
- 并发与隔离：每个馆在自己的线程里独立采集、独立激活；某馆暂停或失败时，
  其它馆照常发布，不受牵连。
- 一馆内的登记与拼接必须同属一个发布版、页数齐全、每页校验通过、馆内拼接
  引用完整，才在一个事务里切换该馆 active；采集与校验期间查询继续命中该馆旧版。
- 暂停续采：503 按 Retry-After 等待后用*同一令牌*重试；暂停只影响该馆。
  暂停期间已到达的采集进度不会丢失，恢复后从原断点继续（游标 = 已确认保存页数）。
- 401 过期 / 令牌跨会话 / 进程重启：重新登录，从该馆该版已确认保存页数继续，
  校验和一致即跳过，不重复抓取。
- 不同馆可以使用相同的馆藏编号与版号：所有数据和游标都按 (site, version) 隔离。

跨馆拼接不要求两馆同版，也不做跨馆事务；查询端按各馆当前 active 版本把两端
跨馆登记实时配对（见 cross.py），因此一端晚到或撤下都无需重采另一端。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

from ceramics.db import all_sites, connect, init_db
from ceramics.parsing import parse_page, split_endpoint
from ceramics.protocol import edge_key, page_checksum

ENTRIES = ("registry", "joins")
MAX_SESSION_RESTARTS = 100


class SiteError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class SessionReset(Exception):
    """会话失效，需要重新登录并从已确认保存页数继续。"""


class SiteClient:
    def __init__(self, base_url: str, site: str, key: str = "ceramics-local-control",
                 pause_cap: int = 30, pause_budget: float | None = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.site = site
        self.key = key
        self.pause_cap = pause_cap
        # 单轮内为该馆在暂停上最多等待的秒数；超过则把该馆挂起到下一轮，
        # 已确认页面全部保留，恢复后从原断点继续，不会拖住其它馆的后续轮次。
        self.pause_budget = pause_budget
        self._paused_for = 0.0
        self.sid: str | None = None
        self.secret: str | None = None
        self.token: str | None = None

    def _wait_paused(self, seconds: int) -> None:
        self._paused_for += seconds
        if self.pause_budget is not None and self._paused_for > self.pause_budget:
            raise SiteError("paused_deferred",
                            f"[{self.site}] 暂停已超过本轮预算 {self.pause_budget:g}s，"
                            f"挂起到下一轮，恢复后从已确认页断点续采")
        print(f"  · [{self.site}] 站点暂停受理，{seconds}s 后重试")
        time.sleep(seconds)

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
            if exc.code in (403, 409):
                # 签名/令牌不属于当前会话或顺序错乱：重新登录拿全新令牌链
                raise SessionReset(payload.get("message", "签名或令牌被拒"))
            raise SiteError("http", f"HTTP {exc.code}: {payload.get('message', exc)}")
        except urllib.error.URLError as exc:
            raise SiteError("network", f"无法连接模拟站: {exc}")

    def login(self, resume: int = 0) -> None:
        # 暂停期登录会收到 503：等待恢复后重试；恢复后拿到的是全新会话与令牌链。
        # resume 报告该入口已确认保存的页数，站点从该游标签发*新会话*的首令牌。
        while True:
            try:
                body = f"key={self.key}&resume={int(resume)}".encode()
                data = self._request("POST", f"/{self.site}/session", body,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
                self.sid = data["sid"]
                self.secret = data["secret"]
                self.token = data["first_token"]
                return
            except SiteError as exc:
                if exc.kind == "paused":
                    self._wait_paused(exc.retry_after or 2)
                    continue
                raise
            self.sid = data["sid"]

    def fetch_page(self, entry: str) -> dict:
        """取当前令牌对应的一页；暂停时原地等待重试，其它认证问题向上抛 SessionReset。"""
        path = f"/{self.site}/{entry}"
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
                    # 暂停不消耗游标：同一令牌稍后重试，恢复后从原断点继续
                    self._wait_paused(exc.retry_after or 2)
                    continue
                raise
            self.token = page["next_token"]
            return page


def _record_failure(conn: sqlite3.Connection, site: str, version: str, stage: str,
                    reason: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO version_failures(site, version, stage, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (site, version, stage, reason, detail, time.time()))
    # 已激活的完整版本不因新一轮采集失败而降级，查询必须继续可用
    conn.execute("UPDATE versions SET status='failed', updated_at=? "
                 "WHERE site=? AND version=? AND status!='active'",
                 (time.time(), site, version))
    conn.commit()


def _ensure_version_row(conn: sqlite3.Connection, site: str, version: str,
                        published_at: str, note: str) -> None:
    conn.execute(
        "INSERT INTO versions(site, version, status, published_at, note, updated_at) "
        "VALUES (?, ?, 'collecting', ?, ?, ?) "
        "ON CONFLICT(site, version) DO UPDATE SET updated_at=excluded.updated_at",
        (site, version, published_at, note, time.time()))
    conn.commit()


def _saved_page_count(conn: sqlite3.Connection, site: str, entry: str,
                      version: str | None) -> int:
    """该馆该入口在指定版本下已确认保存的连续页数（从 0 起）。"""
    if not version:
        return 0
    rows = conn.execute(
        "SELECT page_index FROM pages WHERE site=? AND version=? AND entry=? "
        "ORDER BY page_index", (site, version, entry)).fetchall()
    indexes = {r["page_index"] for r in rows}
    count = 0
    while count in indexes:
        count += 1
    return count


def walk_entry(client: SiteClient, conn: sqlite3.Connection, entry: str) -> str:
    """完整抓取一个馆的一个入口，返回该入口当前发布版；失败记录原因并抛异常。"""
    site = client.site
    restarts = 0
    version: str | None = None
    total_pages = 0
    while True:
        if restarts > MAX_SESSION_RESTARTS:
            raise SiteError("restart_limit", f"[{site}] 会话重建次数超限")
        try:
            if version is None:
                # 第一阶段：探针会话取第 0 页，确定该馆该入口当前发布版
                client.login(resume=0)
                probe = client.fetch_page(entry)
                if probe["page_index"] != 0:
                    raise SiteError("protocol", "探针页序号应为 0")
                version = probe["version"]
                total_pages = probe["total_pages"]
                _ensure_version_row(conn, site, version, probe["published_at"], "")
                _save_or_verify_page(conn, site, entry, probe)

            # 第二阶段：按已保存页数续采；失效重登后也从这里继续，无需重新探针
            resume = _saved_page_count(conn, site, entry, version)
            if total_pages == 0:
                row = conn.execute(
                    "SELECT MIN(total_pages) t FROM pages WHERE site=? AND version=? "
                    "AND entry=?", (site, version, entry)).fetchone()
                total_pages = row["t"] or 0
            if resume >= total_pages:
                print(f"  · [{site}] {entry} 发布版 {version} 的 {total_pages} 页均已确认，整入口跳过")
                return version
            client.login(resume=resume)
            page_index = resume
            while True:
                page = client.fetch_page(entry)
                if page["version"] != version:
                    # 采集中入口切版：本轮作废，下一轮从新版第 0 页重新探针
                    raise SiteError(
                        "entry_switch",
                        f"[{site}] 入口 {entry} 采集中从 {version} 切到 {page['version']}，本轮作废")
                if page["page_index"] != page_index:
                    raise SiteError("protocol",
                                    f"[{site}] 页序号不连续：期望 {page_index}，"
                                    f"实际 {page['page_index']}")
                _save_or_verify_page(conn, site, entry, page)
                page_index += 1
                if page_index >= total_pages:
                    return version
        except SessionReset as exc:
            restarts += 1
            print(f"  ! [{site}] 会话失效（{exc}），按已确认保存页数重新登录后续采")
            continue
        except SiteError as exc:
            if exc.kind == "paused_deferred":
                # 暂停超预算：挂起本馆本轮，不记为失败；已落盘页面保留到下一轮续采
                raise
            if version:
                _record_failure(conn, site, version, f"walk:{entry}", exc.kind, str(exc))
            raise


def _save_or_verify_page(conn: sqlite3.Connection, site: str, entry: str,
                         page: dict) -> None:
    """校验单页并落盘；已存在且校验和一致则跳过，不一致则报错。"""
    page_index = page["page_index"]
    version = page["version"]
    if page.get("corrupted"):
        raise SiteError("corrupt_page",
                        f"[{site}] {entry} 第 {page_index} 页被站点标记为损坏")
    try:
        records = parse_page(entry, page["layout"], page["body"])
    except ValueError as exc:
        raise SiteError("parse_error", f"[{site}] {entry} 第 {page_index} 页解析失败: {exc}")
    if page_checksum(records) != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"[{site}] {entry} 第 {page_index} 页校验和不符，内容不可用")
    saved = conn.execute(
        "SELECT checksum FROM pages WHERE site=? AND version=? AND entry=? AND page_index=?",
        (site, version, entry, page_index)).fetchone()
    if saved is None:
        conn.execute(
            "INSERT INTO pages(site, version, entry, page_index, total_pages, layout,"
            " checksum, body, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (site, version, entry, page_index, page["total_pages"], page["layout"],
             page["checksum"], page["body"], time.time()))
        conn.commit()
    elif saved["checksum"] != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"[{site}] {entry} 第 {page_index} 页与已保存内容不一致")
    else:
        print(f"  · [{site}] 跳过已确认保存的 {entry} 第 {page_index} 页（断点续采）")


def validate_version(conn: sqlite3.Connection, site: str, version: str) -> tuple[bool, str]:
    """页数齐全 + 可解析 + 馆内登记唯一 + 馆内/跨馆拼接引用完整性。"""
    for entry in ENTRIES:
        pages = conn.execute(
            "SELECT * FROM pages WHERE site=? AND version=? AND entry=? ORDER BY page_index",
            (site, version, entry)).fetchall()
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
    for p in conn.execute("SELECT * FROM pages WHERE site=? AND version=? AND entry='registry' "
                          "ORDER BY page_index", (site, version)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    catalogs = [r["catalog_no"] for r in registry_rows]
    if len(set(catalogs)) != len(catalogs):
        return False, "登记清单存在重复馆藏编号"

    join_rows = []
    for p in conn.execute("SELECT * FROM pages WHERE site=? AND version=? AND entry='joins' "
                          "ORDER BY page_index", (site, version)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))
    catalog_set = set(catalogs)
    for j in join_rows:
        try:
            sa, a = split_endpoint(j["a"], site)
            sb, b = split_endpoint(j["b"], site)
        except ValueError as exc:
            return False, f"拼接端点格式错误: {exc}"
        if sa == site and a not in catalog_set:
            return False, f"拼接清单引用了本馆登记中不存在的陶片: {a}"
        if sb == site and b not in catalog_set:
            return False, f"拼接清单引用了本馆登记中不存在的陶片: {b}"
    return True, ""


def activate_version(conn: sqlite3.Connection, site: str, version: str) -> None:
    ok, reason = validate_version(conn, site, version)
    if not ok:
        _record_failure(conn, site, version, "activate", "validation_failed", reason)
        raise SiteError("validation_failed", reason)

    registry_rows, join_rows = [], []
    for p in conn.execute("SELECT * FROM pages WHERE site=? AND version=? AND entry='registry' "
                          "ORDER BY page_index", (site, version)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    for p in conn.execute("SELECT * FROM pages WHERE site=? AND version=? AND entry='joins' "
                          "ORDER BY page_index", (site, version)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))

    published = conn.execute("SELECT published_at FROM versions WHERE site=? AND version=?",
                             (site, version)).fetchone()
    # 单事务切换（仅本馆）：任何一步失败，本馆旧 active 保持不变，其它馆完全无感
    with conn:
        conn.execute("DELETE FROM sherds WHERE site=? AND version=?", (site, version))
        conn.execute("DELETE FROM joins WHERE site=? AND version=?", (site, version))
        conn.executemany(
            "INSERT INTO sherds(site, version, catalog_no, museum, material, weight_g) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(site, version, r["catalog_no"], r["museum"], r["material"], r["weight_g"])
             for r in registry_rows])
        conn.executemany(
            "INSERT INTO joins(site, version, edge_key, a, b, bond, confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(site, version, edge_key(r["a"], r["b"]), r["a"], r["b"], r["bond"],
              r["confidence"]) for r in join_rows])
        conn.execute("UPDATE versions SET status='complete', updated_at=? "
                     "WHERE site=? AND status='active'", (time.time(), site))
        conn.execute("UPDATE versions SET status='active', published_at=?, updated_at=? "
                     "WHERE site=? AND version=?",
                     (published["published_at"] if published else None,
                      time.time(), site, version))
        conn.execute("INSERT INTO activation_log(site, version, activated_at) VALUES (?, ?, ?)",
                     (site, version, time.time()))


def collect_site(base_url: str, db_path: str, site: str,
                 pause_budget: float | None = 20.0) -> dict:
    """一个馆的一轮采集：独立连接、独立激活。"""
    conn = connect(db_path)
    client = SiteClient(base_url, site, pause_budget=pause_budget)
    summary = {"site": site, "versions_seen": [], "activated": None, "error": None,
               "deferred": False}
    try:
        seen = {}
        for entry in ENTRIES:
            seen[entry] = walk_entry(client, conn, entry)
        if seen["registry"] != seen["joins"]:
            # 馆内两入口错时切版：不标记失败、不激活，下一轮再看；查询保持该馆旧版
            summary["error"] = (f"两个入口分属不同发布版：registry={seen['registry']}, "
                                f"joins={seen['joins']}，等待同版后再激活")
            return summary
        version = seen["registry"]
        summary["versions_seen"] = [version]
        activate_version(conn, site, version)
        summary["activated"] = version
        return summary
    except SiteError as exc:
        if exc.kind == "paused_deferred":
            summary["deferred"] = True
            summary["error"] = str(exc)
            return summary
        summary["error"] = f"{exc.kind}: {exc}"
        return summary
    finally:
        conn.close()


def run_round(base_url: str, db_path: str, sites: list[str] | None = None,
              concurrency: int = 0, pause_budget: float | None = 20.0) -> dict:
    """并发采集多个馆；返回每馆摘要与整体统计。单馆失败/暂停不阻塞其它馆。"""
    init_db(db_path)
    if not sites:
        sites = discover_sites(base_url, db_path)
    concurrency = concurrency or max(1, len(sites))
    results: dict[str, dict] = {}
    lock = threading.Lock()
    sem = threading.Semaphore(concurrency)

    def _work(site: str) -> None:
        with sem:
            try:
                summary = collect_site(base_url, db_path, site, pause_budget)
            except Exception as exc:  # 单馆异常绝不影响其它馆
                summary = {"site": site, "versions_seen": [], "activated": None,
                           "error": f"unexpected: {exc}"}
            with lock:
                results[site] = summary

    threads = [threading.Thread(target=_work, args=(s,), name=f"collect-{s}")
               for s in sites]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with lock:
        snapshot = dict(results)
    activated = {s: r["activated"] for s, r in snapshot.items() if r["activated"]}
    errors = {s: r["error"] for s, r in snapshot.items() if r["error"] and not r.get("deferred")}
    deferred = [s for s, r in snapshot.items() if r.get("deferred")]
    return {
        "sites": sorted(snapshot),
        "results": {s: snapshot[s] for s in sorted(snapshot)},
        "activated": activated,
        "errors": errors,
        "deferred": sorted(deferred),
    }


def discover_sites(base_url: str, db_path: str) -> list[str]:
    """优先问站点有哪些合作馆；站点不可达时退回数据库中已知的馆。"""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/sites", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        found = [s["site"] for s in data.get("sites", [])]
        if found:
            return sorted(found)
    except (urllib.error.URLError, ValueError):
        pass
    init_db(db_path)
    conn = connect(db_path)
    try:
        return all_sites(conn)
    finally:
        conn.close()


def print_status(db_path: str, site: str | None = None) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        from ceramics.db import active_versions
        active = active_versions(conn)
        print("各馆当前查询版本(active): " + (", ".join(
            f"{s}={v}" for s, v in sorted(active.items())) or "（尚无可用版本）"))
        print("\n发布版状态（按馆隔离）:")
        query = ("SELECT site, version, status, published_at, note FROM versions "
                 + ("WHERE site=? " if site else "") + "ORDER BY site, version")
        for v in conn.execute(query, (site,) if site else ()):
            print(f"  [{v['site']}] {v['version']:<8} {v['status']:<10} "
                  f"{v['published_at'] or ''}  {v['note'] or ''}")
        print("\n已保存页面:")
        query = ("SELECT site, version, entry, COUNT(*) n, MIN(total_pages) total "
                 "FROM pages " + ("WHERE site=? " if site else "")
                 + "GROUP BY site, version, entry ORDER BY site, version, entry")
        for p in conn.execute(query, (site,) if site else ()):
            print(f"  [{p['site']}] {p['version']:<8} {p['entry']:<9} {p['n']}/{p['total']} 页")
        print("\n失败记录（操作员可见的失败原因）:")
        query = ("SELECT site, version, stage, reason, detail, created_at "
                 "FROM version_failures " + ("WHERE site=? " if site else "")
                 + "ORDER BY id DESC LIMIT 10")
        fails = conn.execute(query, (site,) if site else ()).fetchall()
        if not fails:
            print("  （无）")
        for f in fails:
            print(f"  [{f['site']}] {f['version']:<8} [{f['stage']}] {f['reason']} — {f['detail']}")
        revs = conn.execute("SELECT site, version, edge_key FROM revocations "
                            "ORDER BY site, version").fetchall()
        print(f"\n已撤销馆内拼接 {len(revs)} 条: "
              + (", ".join(f"[{r['site']}]{r['edge_key']}" for r in revs) or "（无）"))
        crev = conn.execute("SELECT edge_key, side FROM cross_revocations").fetchall()
        print(f"已撤下跨馆拼接 {len(crev)} 条: "
              + (", ".join(f"{r['edge_key']}({r['side']})" for r in crev) or "（无）"))
    finally:
        conn.close()


def main() -> None:
    # 管道/nohup 下默认块缓冲，持续采集时日志无法实时可见，改为行缓冲
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="陶片清单采集服务（多合作馆并发）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run", help="执行采集轮次（默认一轮）")
    runp.add_argument("--base-url", default="http://127.0.0.1:8080")
    runp.add_argument("--db", default="./ceramics.db")
    runp.add_argument("--sites", default="", help="逗号分隔的馆名；留空自动发现全部馆")
    runp.add_argument("--concurrency", type=int, default=0, help="并发线程数，0 表示全部并行")
    runp.add_argument("--rounds", type=int, default=1, help="采集轮数，0 表示持续运行")
    runp.add_argument("--interval", type=float, default=3.0, help="持续运行时轮间秒数")
    runp.add_argument("--pause-budget", type=float, default=20.0,
                      help="单馆单轮最多等待暂停的秒数，超过则挂起到下一轮（0 表示不限）")

    stat = sub.add_parser("status", help="查看采集与版本状态")
    stat.add_argument("--db", default="./ceramics.db")
    stat.add_argument("--site", default=None)
    args = ap.parse_args()

    if args.cmd == "status":
        print_status(args.db, args.site)
        return

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    pause_budget = None if args.pause_budget <= 0 else args.pause_budget
    rounds_done = 0
    while True:
        rounds_done += 1
        print(f"[采集] 第 {rounds_done} 轮开始 {time.strftime('%H:%M:%S')}（多馆并发）")
        result = run_round(args.base_url, args.db, sites, args.concurrency, pause_budget)
        print(f"[采集] 本轮涉及 {len(result['sites'])} 个馆: {', '.join(result['sites'])}")
        for s in result["sites"]:
            r = result["results"][s]
            if r["activated"]:
                print(f"[采集]   [{s}] 发布版 {r['activated']} 已激活，查询已切换")
            elif r.get("deferred"):
                print(f"[采集]   [{s}] 站点仍在暂停，本轮挂起；恢复后从断点续采，其它馆不受影响")
            elif r["error"]:
                print(f"[采集]   [{s}] 本轮未切换版本：{r['error']}")
            else:
                print(f"[采集]   [{s}] 本轮无新版本变化")
        if args.rounds and rounds_done >= args.rounds:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
