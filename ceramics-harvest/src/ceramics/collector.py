"""采集服务：按会话协议抓取两个入口，校验后按发布版原子激活。

关键保证：
- 503 暂停：按 Retry-After 等待后用*同一令牌*重试（暂停不消耗游标）。
- 401 过期 / 令牌跨会话：重新登录，按已确认保存的页数续采；已确认页面
  正文摘要一致即跳过，实现进程重启后的断点续采。
- 登记与拼接必须同属一个发布版、页数齐全、每页校验通过、拼接引用完整，
  才在一个事务里切换 active；采集与校验期间查询继续命中旧 active 版本。
- 切版前先取站点清单核对两份资料：清单摘要与本地已核验内容一致才发布，
  因此"已缓存的坏页"不会被当作新内容发出去。
- 资料未变时清单摘要不变，每轮每个入口只需一次清单请求，核验流量与页数无关；
  摘要变化时才取逐页摘要，只补抓更正或缺失的页面。
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
from ceramics.parsing import parse_page
from ceramics.protocol import (body_digest, edge_key, manifest_digest,
                               page_checksum, sign)

ENTRIES = ("registry", "joins")
MAX_SESSION_RESTARTS = 100

# 这些失败意味着站点资料本身不可用，需要落失败记录供操作员查看；
# entry_switch / content_changed 只是采集中途的竞态，下一轮重试即可，不算失败。
FAILURE_KINDS = {"corrupt_page", "parse_error", "checksum_mismatch",
                 "verify_incomplete", "protocol", "validation_failed"}


class SiteError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class SessionReset(Exception):
    """会话失效，需要重新登录并从已确认保存的游标续采。"""


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

    def fetch_manifest(self, entry: str, detail: bool = False) -> dict:
        """取入口当前发布版的整份清单摘要（detail 时含逐页摘要）。

        每次都用全新会话，清单请求不占用翻页游标，页序不受影响。
        资料未变时整份摘要一次返回，核验流量与页数无关。
        """
        path = f"/{entry}/manifest"
        suffix = "?detail=1" if detail else ""
        while True:
            self.login(resume=0)
            signature = sign(self.secret, "GET", path, self.sid, self.token)
            try:
                return self._request("GET", path + suffix, headers={
                    "X-Ceramic-Session": self.sid,
                    "X-Ceramic-Token": self.token,
                    "X-Ceramic-Signature": signature,
                })
            except SiteError as exc:
                if exc.kind == "paused":
                    time.sleep(exc.retry_after or 2)
                    continue
                raise


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


def _record_page_issue(conn: sqlite3.Connection, version: str, entry: str,
                       page_index: int, kind: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO page_issues(version, entry, page_index, kind, detail, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (version, entry, page_index, kind, detail, time.time()))
    conn.commit()


def _clear_page_issues(conn: sqlite3.Connection, version: str, entry: str) -> None:
    conn.execute("DELETE FROM page_issues WHERE version=? AND entry=?", (version, entry))


def _ensure_version_row(conn: sqlite3.Connection, version: str, published_at: str,
                        note: str) -> None:
    conn.execute(
        "INSERT INTO versions(version, status, published_at, note, updated_at) "
        "VALUES (?, 'collecting', ?, ?, ?) "
        "ON CONFLICT(version) DO UPDATE SET updated_at=excluded.updated_at, "
        "status=CASE WHEN versions.status='failed' THEN 'collecting' ELSE versions.status END",
        (version, published_at, note, time.time()))
    conn.commit()


def _get_verification(conn: sqlite3.Connection, version: str,
                      entry: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT digest, total_pages FROM entry_verifications WHERE version=? AND entry=?",
        (version, entry)).fetchone()


def _store_verification(conn: sqlite3.Connection, version: str, entry: str,
                        digest: str, total: int) -> None:
    conn.execute(
        "INSERT INTO entry_verifications(version, entry, digest, total_pages, verified_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(version, entry) DO UPDATE SET "
        "digest=excluded.digest, total_pages=excluded.total_pages, "
        "verified_at=excluded.verified_at",
        (version, entry, digest, total, time.time()))
    conn.commit()


def _local_body_digests(conn: sqlite3.Connection, version: str,
                        entry: str) -> dict[int, str]:
    return {r["page_index"]: body_digest(r["body"]) for r in conn.execute(
        "SELECT page_index, body FROM pages WHERE version=? AND entry=?",
        (version, entry))}


def _validate_and_store(conn: sqlite3.Connection, entry: str, version: str,
                        page: dict, expected_digest: str | None) -> bool:
    """校验一页并落盘（缺失则新增、更正则覆盖），返回内容是否有变化。"""
    page_index = page["page_index"]
    if page["version"] != version:
        raise SiteError("entry_switch",
                        f"入口 {entry} 采集中从 {version} 切到 {page['version']}，本轮作废")
    if page.get("corrupted"):
        detail = f"{entry} 第 {page_index} 页被站点标记为损坏（发布版 {version}）"
        _record_page_issue(conn, version, entry, page_index, "corrupt_page", detail)
        raise SiteError("corrupt_page", detail)
    try:
        records = parse_page(entry, page["layout"], page["body"])
    except ValueError as exc:
        detail = f"{entry} 第 {page_index} 页解析失败: {exc}"
        _record_page_issue(conn, version, entry, page_index, "parse_error", detail)
        raise SiteError("parse_error", detail)
    if page_checksum(records) != page["checksum"]:
        detail = f"{entry} 第 {page_index} 页校验和不符，内容不可用"
        _record_page_issue(conn, version, entry, page_index, "checksum_mismatch", detail)
        raise SiteError("checksum_mismatch", detail)
    actual = body_digest(page["body"])
    if expected_digest is not None and actual != expected_digest:
        raise SiteError("content_changed",
                        f"{entry} 第 {page_index} 页在清单核验后发生变化，等待下一轮")
    saved = conn.execute(
        "SELECT body FROM pages WHERE version=? AND entry=? AND page_index=?",
        (version, entry, page_index)).fetchone()
    if saved is not None and body_digest(saved["body"]) == actual:
        return False
    conn.execute(
        "INSERT INTO pages(version, entry, page_index, total_pages, layout,"
        " checksum, body, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(version, entry, page_index) DO UPDATE SET "
        "total_pages=excluded.total_pages, layout=excluded.layout, "
        "checksum=excluded.checksum, body=excluded.body, saved_at=excluded.saved_at",
        (version, entry, page_index, page["total_pages"], page["layout"],
         page["checksum"], page["body"], time.time()))
    conn.commit()
    return True


def _fetch_page_at(client: SiteClient, entry: str, index: int) -> dict:
    """用全新会话从指定游标取该页（清单已定位差异页，无需从第 0 页重走）。"""
    client.login(resume=index)
    return client.fetch_page(entry)


def sync_entry(client: SiteClient, conn: sqlite3.Connection, entry: str,
               summary: dict) -> tuple[str, bool]:
    """把某入口与站点当前发布版对齐；返回 (版本, 是否有内容变化)。

    先取整份清单摘要：与上次核验一致且本地页齐全时，不逐页核验（流量与页数无关）。
    摘要变化时才取逐页摘要，只补抓缺失页与正文被更正的页；发现损坏页即记录位置并
    放弃该版，查询继续使用旧版本。
    """
    restarts = 0
    recorded_version: dict[str, str] = {}

    def once() -> tuple[str, bool]:
        manifest = client.fetch_manifest(entry)
        summary["manifests_fetched"] += 1
        version = manifest["version"]
        recorded_version["version"] = version
        try:
            return _sync_entry_once(client, conn, entry, version, manifest, summary)
        except SiteError as exc:
            if exc.kind in FAILURE_KINDS:
                # 同一版每轮只记一次失败，避免重试把同一原因刷屏
                _record_failure(conn, version, f"verify:{entry}", exc.kind, str(exc))
            raise

    while True:
        if restarts > MAX_SESSION_RESTARTS:
            raise SiteError("restart_limit", "会话重建次数超限")
        try:
            return once()
        except SessionReset as exc:
            restarts += 1
            print(f"  ! 会话失效（{exc}），按已确认保存页数重新登录后续采，不重复抓取")


def _sync_entry_once(client: SiteClient, conn: sqlite3.Connection, entry: str,
                     version: str, manifest: dict,
                     summary: dict) -> tuple[str, bool]:
    total = manifest["total_pages"]
    _ensure_version_row(conn, version, manifest["published_at"], "")
    # 页数变少时清理越界旧页，避免旧页被算进完整性校验
    conn.execute(
        "DELETE FROM pages WHERE version=? AND entry=? AND page_index >= ?",
        (version, entry, total))
    conn.commit()

    cached = _get_verification(conn, version, entry)
    local = _local_body_digests(conn, version, entry)
    missing = [i for i in range(total) if i not in local]
    if (cached and cached["digest"] == manifest["digest"]
            and cached["total_pages"] == total and not missing):
        # 资料自上次完整核验后未变：一次清单请求即完成核验，无需逐页抓取
        _store_verification(conn, version, entry, manifest["digest"], total)
        _clear_page_issues(conn, version, entry)
        conn.commit()
        print(f"  · {entry} 发布版 {version} 资料未变（清单摘要一致），"
              f"{total} 页沿用已确认保存内容，跳过逐页核验（断点续采）")
        return version, False

    detail = client.fetch_manifest(entry, detail=True)
    summary["manifests_fetched"] += 1
    if detail["version"] != version or detail["digest"] != manifest["digest"]:
        raise SiteError("entry_switch", f"入口 {entry} 核验期间发布内容变化，本轮作废")
    pages = {p["page_index"]: p for p in detail["pages"]}
    if sorted(pages) != list(range(total)):
        raise SiteError("protocol", f"{entry} 清单页序号不完整")

    to_fetch: list[int] = []
    corrupt_details: list[str] = []
    for index in range(total):
        spec = pages[index]
        if spec["corrupted"]:
            detail_txt = f"{entry} 第 {index} 页被站点标记为损坏（发布版 {version}）"
            _record_page_issue(conn, version, entry, index, "corrupt_page", detail_txt)
            corrupt_details.append(detail_txt)
            continue
        if local.get(index) == spec["body_digest"]:
            print(f"  · 跳过已确认保存的 {entry} 第 {index} 页（断点续采）")
            continue
        to_fetch.append(index)
    if corrupt_details:
        # 坏页位置已逐个写入 page_issues / status；本版整份不可发布，查询继续用旧版
        raise SiteError("corrupt_page", "；".join(corrupt_details))

    changed = False
    for index in to_fetch:
        page = _fetch_page_at(client, entry, index)
        summary["pages_fetched"] += 1
        if page["page_index"] != index:
            raise SiteError("protocol",
                            f"{entry} 页序号不连续：期望 {index}，实际 {page['page_index']}")
        if _validate_and_store(conn, entry, version, page, pages[index]["body_digest"]):
            changed = True
            print(f"  · 已保存 {entry} 第 {index} 页（补抓更正/缺失页）")

    local = _local_body_digests(conn, version, entry)
    for index in range(total):
        if local.get(index) != pages[index]["body_digest"]:
            raise SiteError("verify_incomplete", f"{entry} 第 {index} 页未完成核验")

    _store_verification(conn, version, entry, manifest["digest"], total)
    _clear_page_issues(conn, version, entry)
    conn.commit()
    return version, changed


def confirm_snapshot(client: SiteClient, conn: sqlite3.Connection, version: str,
                     summary: dict) -> tuple[bool, str]:
    """切版前核对两份资料：站点当前清单必须与本地已核验内容逐字节一致。"""
    for entry in ENTRIES:
        manifest = client.fetch_manifest(entry)
        summary["manifests_fetched"] += 1
        if manifest["version"] != version:
            return False, (f"{entry} 在切版前切换到 {manifest['version']}，"
                           f"等待两入口同版后再激活")
        local = _local_body_digests(conn, version, entry)
        local_pages = [{"page_index": i, "body_digest": d, "corrupted": False}
                       for i, d in sorted(local.items())]
        if (len(local_pages) != manifest["total_pages"]
                or manifest_digest(local_pages) != manifest["digest"]):
            return False, f"{entry} 站点资料在核验后变化，等待下一轮重新核验后再切换"
    return True, ""


def validate_version(conn: sqlite3.Connection, version: str) -> tuple[bool, str]:
    """页数齐全 + 可解析 + 登记唯一 + 拼接引用完整性。"""
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


def run_round(base_url: str, db_path: str) -> dict:
    init_db(db_path)
    conn = connect(db_path)
    client = SiteClient(base_url)
    summary = {"versions_seen": [], "activated": None, "error": None,
               "pages_fetched": 0, "manifests_fetched": 0, "changed": False}
    try:
        seen, changed = {}, {}
        for entry in ENTRIES:
            seen[entry], changed[entry] = sync_entry(client, conn, entry, summary)
        summary["changed"] = any(changed.values())
        if seen["registry"] != seen["joins"]:
            # 两入口切版时间不同：不标记失败、不激活，下一轮再看；查询保持旧版
            summary["error"] = (f"两个入口分属不同发布版：registry={seen['registry']}, "
                                f"joins={seen['joins']}，等待同版后再激活")
            return summary
        version = seen["registry"]
        summary["versions_seen"] = [version]
        active = get_active_version(conn)
        if version == active and not summary["changed"]:
            # 资料与当前发布版一致，无需重复发布
            return summary
        ok, why = confirm_snapshot(client, conn, version, summary)
        if not ok:
            summary["error"] = why
            return summary
        activate_version(conn, version)
        summary["activated"] = version
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
        for v in conn.execute("SELECT version, status, published_at, note FROM versions "
                              "ORDER BY version"):
            print(f"  {v['version']:<8} {v['status']:<10} {v['published_at'] or ''}  {v['note'] or ''}")
        print("\n已保存页面:")
        for p in conn.execute("SELECT version, entry, COUNT(*) n, MIN(total_pages) total "
                              "FROM pages GROUP BY version, entry ORDER BY version, entry"):
            print(f"  {p['version']:<8} {p['entry']:<9} {p['n']}/{p['total']} 页")
        print("\n异常页（站点核验发现的坏页位置）:")
        issues = conn.execute(
            "SELECT version, entry, page_index, kind, detail FROM page_issues "
            "ORDER BY id DESC LIMIT 10").fetchall()
        if not issues:
            print("  （无）")
        for i in issues:
            print(f"  {i['version']:<8} {i['entry']} 第 {i['page_index']} 页 "
                  f"[{i['kind']}] {i['detail'] or ''}")
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
