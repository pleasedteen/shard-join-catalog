"""采集服务：按会话协议抓取两个入口，校验后按 (发布版, 修订) 原子激活。

关键保证：
- 503 暂停：按 Retry-After 等待后用*同一令牌*重试（暂停不消耗游标）。
- 401 过期 / 令牌跨会话：重新登录拿全新令牌链，按缺口续采，不重复抓取已确认页。
- 馆方可沿用原发布编号补发修订：两个入口可能错时上线同一修订，查询要等
  同一 (发布版, 修订) 收齐才切换；切换前按站点当时的资料状态逐页复核。
- 重登或重启后，位置与正文未变且仍完好的页直接沿用缓存、无需重下——
  即使它排在改动页后面；只补抓缺失或已改动的页。
- 已缓存的页若后来被站点标坏，失败记录报出入口和页号，查询继续用上次的
  完整结果（旧 active 不受影响）。
- 页数齐全、每页校验通过、拼接引用完整，才在一个事务里切换 active；
  采集与校验期间查询继续命中旧 active 版本。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from ceramics.db import connect, get_active_release, init_db
from ceramics.parsing import parse_page
from ceramics.protocol import edge_key, page_checksum, sign

ENTRIES = ("registry", "joins")
MAX_SESSION_RESTARTS = 100

Manifest = list[tuple[int, str, bool]]  # (页序号, 校验和, 是否损坏)，按页序号升序


class SiteError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class SessionReset(Exception):
    """会话失效，需要重新登录（站点为 新会话 从指定游标签发全新令牌链）。"""


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
        # resume 报告从第几页开始取，站点为*新会话*从该游标签发首令牌，
        # 因此无需复用任何旧令牌，也不必从第 0 页重走。
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


def _record_failure(conn: sqlite3.Connection, version: str, revision: int,
                    stage: str, reason: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO version_failures(version, revision, stage, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (version, revision, stage, reason, detail, time.time()))
    # 已激活的完整版本不因新一轮采集失败而降级，查询必须继续可用
    conn.execute("UPDATE versions SET status='failed', updated_at=? "
                 "WHERE version=? AND revision=? AND status!='active'",
                 (time.time(), version, revision))
    conn.commit()


def _ensure_version_row(conn: sqlite3.Connection, version: str, revision: int,
                        published_at: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO versions(version, revision, status, published_at, note, updated_at) "
        "VALUES (?, ?, 'collecting', ?, ?, ?) "
        "ON CONFLICT(version, revision) DO UPDATE SET updated_at=excluded.updated_at",
        (version, revision, published_at, note, time.time()))
    conn.commit()


def _manifest_of(page: dict) -> Manifest:
    """从页面响应取出整份页面清单并规范化：[(页序号, 校验和, 是否损坏)...]。"""
    total = page["total_pages"]
    raw = page.get("manifest")
    if not isinstance(raw, list) or len(raw) != total:
        raise SiteError("protocol", "站点页面清单缺失或与总页数不符")
    entries = sorted((int(m["page_index"]), str(m["checksum"]), bool(m.get("corrupted")))
                     for m in raw)
    if [i for i, _, _ in entries] != list(range(total)):
        raise SiteError("protocol", "站点页面清单页序号不连续")
    return entries


def _has_saved_page(conn: sqlite3.Connection, entry: str, page_index: int) -> bool:
    return conn.execute("SELECT 1 FROM pages WHERE entry=? AND page_index=? LIMIT 1",
                        (entry, page_index)).fetchone() is not None


def _save_page(conn: sqlite3.Connection, entry: str, page: dict,
               expected_checksum: str | None = None, source: str = "fetched") -> None:
    """校验单页并落盘；本修订已存在且校验和一致则跳过，不一致则报错。"""
    page_index = page["page_index"]
    version = page["version"]
    revision = page["revision"]
    if page.get("corrupted"):
        raise SiteError("corrupt_page",
                        f"{entry} 第 {page_index} 页被站点标记为损坏")
    try:
        records = parse_page(entry, page["layout"], page["body"])
    except ValueError as exc:
        raise SiteError("parse_error", f"{entry} 第 {page_index} 页解析失败: {exc}")
    if page_checksum(records) != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry} 第 {page_index} 页校验和不符，内容不可用")
    if expected_checksum is not None and page["checksum"] != expected_checksum:
        raise SiteError("checksum_mismatch",
                        f"{entry} 第 {page_index} 页与站点页面清单不符")
    saved = conn.execute(
        "SELECT checksum FROM pages WHERE version=? AND revision=? AND entry=? "
        "AND page_index=?", (version, revision, entry, page_index)).fetchone()
    if saved is None:
        conn.execute(
            "INSERT INTO pages(version, revision, entry, page_index, total_pages, layout,"
            " checksum, body, source, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version, revision, entry, page_index, page["total_pages"], page["layout"],
             page["checksum"], page["body"], source, time.time()))
        conn.commit()
    elif saved["checksum"] != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry} 第 {page_index} 页与已保存内容不一致"
                        f"（同一 (发布版, 修订) 被站点更换内容）")
    else:
        print(f"  · 跳过已确认保存的 {entry} 第 {page_index} 页（断点续采）")


def _probe(client: SiteClient, entry: str, restarts: list[int]) -> dict:
    """登录并取第 0 页（探针）；会话失效时重登重试，站点暂停由客户端自行等待。"""
    while True:
        if restarts[0] > MAX_SESSION_RESTARTS:
            raise SiteError("restart_limit", "会话重建次数超限")
        client.login(resume=0)
        try:
            return client.fetch_page(entry)
        except SessionReset as exc:
            restarts[0] += 1
            print(f"  ! 探针会话失效（{exc}），重新登录")


def walk_entry(client: SiteClient, conn: sqlite3.Connection, entry: str) -> tuple[str, int]:
    """完整抓取一个入口的当前 (发布版, 修订)，返回 (version, revision)。

    流程：
    1. 探针取第 0 页，确认入口当前 (发布版, 修订) 与整份页面清单；
    2. 清单中任何损坏页直接报错（含入口和页号），本轮放弃，查询继续用旧版；
    3. 位置与正文未变且仍完好的页沿用缓存（无需重下，即使排在改动页后面），
       只补抓缺失或已改动的页；连续区段共用一次登录，会话失效按缺口重登续采。
    """
    restarts = [0]
    version: str | None = None
    revision: int | None = None
    try:
        probe = _probe(client, entry, restarts)
        if probe["page_index"] != 0:
            raise SiteError("protocol", "探针页序号应为 0")
        version, revision = probe["version"], probe["revision"]
        total_pages = probe["total_pages"]
        manifest = _manifest_of(probe)
        _ensure_version_row(conn, version, revision, probe["published_at"])

        # 损坏页（含已缓存页后来被标坏）：报出入口和页号，本轮放弃
        for idx, _, broken in manifest:
            if broken:
                where = "已缓存页被标坏" if _has_saved_page(conn, entry, idx) \
                    else "站点当前资料状态"
                raise SiteError("corrupt_page",
                                f"{entry} 第 {idx} 页被站点标记为损坏（{where}）")
        _save_page(conn, entry, probe, expected_checksum=manifest[0][1], source="fetched")

        # 逐页比对：本修订已确认 / 可从既有缓存沿用 / 需要补抓
        needed, reused = [], 0
        for idx, checksum, _ in manifest[1:]:
            saved = conn.execute(
                "SELECT checksum FROM pages WHERE version=? AND revision=? AND entry=? "
                "AND page_index=?", (version, revision, entry, idx)).fetchone()
            if saved is not None:
                if saved["checksum"] != checksum:
                    raise SiteError(
                        "checksum_mismatch",
                        f"{entry} 第 {idx} 页：站点在同一 (发布版, 修订) 内更换了内容")
                reused += 1
                continue
            donor = conn.execute(
                "SELECT layout, body FROM pages WHERE entry=? AND page_index=? "
                "AND checksum=? ORDER BY revision DESC LIMIT 1",
                (entry, idx, checksum)).fetchone()
            if donor is not None:
                # 位置与正文未变且站点仍标完好：沿用缓存，无需重下
                conn.execute(
                    "INSERT INTO pages(version, revision, entry, page_index, total_pages,"
                    " layout, checksum, body, source, saved_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'carried', ?)",
                    (version, revision, entry, idx, total_pages, donor["layout"],
                     checksum, donor["body"], time.time()))
                conn.commit()
                reused += 1
            else:
                needed.append(idx)
        if reused:
            print(f"  · {entry} {version} 修订{revision}：{reused} 页位置与正文未变且仍完好，"
                  f"沿用已确认缓存（断点续采，无需重下）")
        if not needed:
            print(f"  · {entry} {version} 修订{revision} 的 {total_pages} 页均已确认，无需补抓")
            return version, revision

        # 只补抓缺失/改动页：连续区段共用一次登录，跨段按游标重新登录
        pos = 0
        while pos < len(needed):
            start = end = needed[pos]
            while pos + 1 < len(needed) and needed[pos + 1] == end + 1:
                pos += 1
                end = needed[pos]
            pos += 1
            client.login(resume=start)
            idx = start
            while idx <= end:
                try:
                    page = client.fetch_page(entry)
                except SessionReset as exc:
                    restarts[0] += 1
                    if restarts[0] > MAX_SESSION_RESTARTS:
                        raise SiteError("restart_limit", "会话重建次数超限")
                    print(f"  ! 会话失效（{exc}），按缺口重登续采，已确认页不重复抓取")
                    client.login(resume=idx)
                    continue
                if (page["version"], page["revision"]) != (version, revision):
                    raise SiteError(
                        "entry_switch",
                        f"入口 {entry} 采集中从 {version} 修订{revision} 切到 "
                        f"{page['version']} 修订{page['revision']}，本轮作废")
                if page["page_index"] != idx:
                    raise SiteError("protocol",
                                    f"页序号不连续：期望 {idx}，实际 {page['page_index']}")
                if _manifest_of(page) != manifest:
                    raise SiteError("site_changed",
                                    f"入口 {entry} 采集期间站点页面清单变化，本轮作废")
                _save_page(conn, entry, page,
                           expected_checksum=manifest[idx][1], source="fetched")
                idx += 1
        return version, revision
    except SiteError as exc:
        if version is not None:
            _record_failure(conn, version, revision, f"walk:{entry}", exc.kind, str(exc))
        raise
    except SessionReset as exc:  # 兜底：理论上是登录环节之外的会话失效
        err = SiteError("session_reset", f"会话失效：{exc}")
        if version is not None:
            _record_failure(conn, version, revision, f"walk:{entry}", err.kind, str(err))
        raise err


def recheck_release(client: SiteClient, conn: sqlite3.Connection, entry: str,
                    version: str, revision: int) -> tuple[bool, str, str]:
    """切换前按站点当时的资料状态复核：重新探针，逐页比对已确认内容与站点清单。

    返回 (是否通过, 失败类别, 详情)；详情含入口和页号，供操作员可见的失败记录。
    """
    restarts = [0]
    probe = _probe(client, entry, restarts)
    if (probe["version"], probe["revision"]) != (version, revision):
        return False, "site_changed", (
            f"入口 {entry} 站点当前为 {probe['version']} 修订{probe['revision']}，"
            f"本次收齐的是 {version} 修订{revision}")
    manifest = _manifest_of(probe)
    for idx, checksum, broken in manifest:
        if broken:
            return False, "corrupt_page", (
                f"{entry} 第 {idx} 页被站点标记为损坏（已缓存页被标坏）")
        saved = conn.execute(
            "SELECT checksum FROM pages WHERE version=? AND revision=? AND entry=? "
            "AND page_index=?", (version, revision, entry, idx)).fetchone()
        if saved is None:
            return False, "site_changed", f"入口 {entry} 第 {idx} 页缺失，无法复核"
        if saved["checksum"] != checksum:
            return False, "site_changed", f"入口 {entry} 第 {idx} 页与站点当前内容不符"
    return True, "", ""


def validate_version(conn: sqlite3.Connection, version: str, revision: int) -> tuple[bool, str]:
    """页数齐全 + 可解析 + 登记唯一 + 拼接引用完整性。"""
    for entry in ENTRIES:
        pages = conn.execute(
            "SELECT * FROM pages WHERE version=? AND revision=? AND entry=? "
            "ORDER BY page_index", (version, revision, entry)).fetchall()
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
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND revision=? "
                          "AND entry='registry' ORDER BY page_index",
                          (version, revision)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    catalogs = [r["catalog_no"] for r in registry_rows]
    if len(set(catalogs)) != len(catalogs):
        return False, "登记清单存在重复馆藏编号"

    join_rows = []
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND revision=? "
                          "AND entry='joins' ORDER BY page_index",
                          (version, revision)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))
    catalog_set = set(catalogs)
    for j in join_rows:
        for side in (j["a"], j["b"]):
            if side not in catalog_set:
                return False, f"拼接清单引用了登记中不存在的陶片: {side}"
    return True, ""


def activate_version(conn: sqlite3.Connection, version: str, revision: int) -> None:
    ok, reason = validate_version(conn, version, revision)
    if not ok:
        _record_failure(conn, version, revision, "activate", "validation_failed", reason)
        raise SiteError("validation_failed", reason)

    registry_rows, join_rows = [], []
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND revision=? "
                          "AND entry='registry' ORDER BY page_index",
                          (version, revision)):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    for p in conn.execute("SELECT * FROM pages WHERE version=? AND revision=? "
                          "AND entry='joins' ORDER BY page_index",
                          (version, revision)):
        join_rows.extend(parse_page("joins", p["layout"], p["body"]))

    published = conn.execute("SELECT published_at FROM versions WHERE version=? "
                             "AND revision=?", (version, revision)).fetchone()
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
                     "WHERE version=? AND revision=?",
                     (published["published_at"] if published else None, time.time(),
                      version, revision))
        conn.execute("INSERT INTO activation_log(version, revision, activated_at) "
                     "VALUES (?, ?, ?)", (version, revision, time.time()))


def run_round(base_url: str, db_path: str) -> dict:
    init_db(db_path)
    conn = connect(db_path)
    client = SiteClient(base_url)
    summary = {"versions_seen": [], "activated": None, "error": None}
    try:
        seen = {}
        for entry in ENTRIES:
            seen[entry] = walk_entry(client, conn, entry)
        if seen["registry"] != seen["joins"]:
            # 两入口错时上线：同一修订未收齐，不标记失败、不激活，等下一轮；
            # 查询保持上一个完整结果
            fmt = lambda rel: f"{rel[0]} 修订{rel[1]}"
            summary["error"] = (f"两个入口尚未收齐同一修订：registry={fmt(seen['registry'])}, "
                                f"joins={fmt(seen['joins'])}，等待收齐后再切换")
            return summary
        version, revision = seen["registry"]
        summary["versions_seen"] = [f"{version} 修订{revision}"]
        # 切换前按站点当时的资料状态复核两个入口
        for entry in ENTRIES:
            ok, kind, detail = recheck_release(client, conn, entry, version, revision)
            if not ok:
                _record_failure(conn, version, revision, f"recheck:{entry}", kind, detail)
                summary["error"] = (f"切换前复核未通过（{detail}），"
                                    f"查询继续用上次的完整结果")
                return summary
        activate_version(conn, version, revision)
        summary["activated"] = (version, revision)
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
        active = get_active_release(conn)
        shown = f"{active[0]} 修订{active[1]}" if active else "（尚无可用版本）"
        print(f"当前查询版本(active): {shown}")
        print("\n发布版状态:")
        for v in conn.execute("SELECT version, revision, status, published_at, note "
                              "FROM versions ORDER BY version, revision"):
            print(f"  {v['version']:<8} 修订{v['revision']:<3} {v['status']:<10} "
                  f"{v['published_at'] or ''}  {v['note'] or ''}")
        print("\n已保存页面:")
        for p in conn.execute("SELECT version, revision, entry, COUNT(*) n, "
                              "MIN(total_pages) total, SUM(source='carried') carried "
                              "FROM pages GROUP BY version, revision, entry "
                              "ORDER BY version, revision, entry"):
            print(f"  {p['version']:<8} 修订{p['revision']:<3} {p['entry']:<9} "
                  f"{p['n']}/{p['total']} 页（沿用缓存 {p['carried'] or 0} 页）")
        print("\n失败记录（操作员可见的失败原因）:")
        fails = conn.execute("SELECT version, revision, stage, reason, detail, created_at "
                             "FROM version_failures ORDER BY id DESC LIMIT 10").fetchall()
        if not fails:
            print("  （无）")
        for f in fails:
            print(f"  {f['version']:<8} 修订{f['revision']:<3} [{f['stage']}] "
                  f"{f['reason']} — {f['detail']}")
        if active:
            rev = conn.execute("SELECT edge_key FROM revocations WHERE version=?",
                               (active[0],)).fetchall()
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
            version, revision = result["activated"]
            print(f"[采集] 发布版 {version} 修订{revision} 已激活，查询已切换")
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
