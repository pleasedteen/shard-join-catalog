"""采集服务：按会话协议抓取两个入口，逐页解析入库，校验通过后按发布版原子激活。

关键保证：
- 503 暂停：按 Retry-After 等待后用*同一令牌*重试（暂停不消耗游标）。
- 401 过期 / 令牌跨会话：重新登录，并从入口第一页重走；已确认保存的页面
  校验和一致即跳过，实现进程重启后的断点续采。
- **逐页落库**：每页（固定 500 条）解析与校验通过后，在*同一个事务*里写入
  pages 原始页与 sherds/joins 业务行；单个写事务至多覆盖两页（1000 条），
  进程内存只保留当前页，整版从 2 万增到 8 万峰值内存不随之线性增长。
- 登记与拼接必须同属一个发布版、页数齐全、每页校验通过、拼接引用完整，
  才以仅改版本状态的轻量事务切换 active；采集与校验期间查询继续命中旧 active，
  且校验全程只读，不阻塞当前版的查询与复核撤销。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import sys
import time
import urllib.error
import urllib.request

from ceramics.db import connect, get_active_version, init_db
from ceramics.parsing import (
    MAX_BUSINESS_ROWS_PER_TXN,
    MAX_PAGES_PER_WRITE_TXN,
    PAGE_SIZE,
    parse_page,
)
from ceramics.protocol import edge_key, page_checksum

ENTRIES = ("registry", "joins")
MAX_SESSION_RESTARTS = 100


class _Metrics:
    """采集进程资源指标：用于 verify 的资源对比，不参与任何业务判断。"""

    def __init__(self) -> None:
        self.fetch_count = 0          # 实际向站点发起的翻页请求数
        self.write_txn_count = 0      # 含业务记录写入的事务数
        self.max_business_rows_per_txn = 0
        self.txn_histogram: dict[int, int] = {}  # 事务业务行数 -> 次数
        self.max_records_per_page = 0

    def record_write_txn(self, business_rows: int) -> None:
        self.write_txn_count += 1
        self.max_business_rows_per_txn = max(self.max_business_rows_per_txn,
                                             business_rows)
        self.txn_histogram[business_rows] = self.txn_histogram.get(business_rows, 0) + 1

    def snapshot(self) -> dict:
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss 在 Linux 上单位为 KB
        peak_rss_kb = int(rusage.ru_maxrss)
        return {
            "fetch_count": self.fetch_count,
            "write_txn_count": self.write_txn_count,
            "max_business_rows_per_txn": self.max_business_rows_per_txn,
            "max_pages_per_txn_limit": MAX_PAGES_PER_WRITE_TXN,
            "page_size_limit": PAGE_SIZE,
            "max_records_per_page": self.max_records_per_page,
            "txn_histogram": dict(sorted(self.txn_histogram.items())),
            "peak_rss_kb": peak_rss_kb,
        }


METRICS = _Metrics()


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
                        note: str) -> None:
    conn.execute(
        "INSERT INTO versions(version, status, published_at, note, updated_at) "
        "VALUES (?, 'collecting', ?, ?, ?) "
        "ON CONFLICT(version) DO UPDATE SET updated_at=excluded.updated_at",
        (version, published_at, note, time.time()))
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


def walk_entry(client: SiteClient, conn: sqlite3.Connection, entry: str) -> str:
    """完整抓取一个入口，返回该入口当前发布版；失败时记录原因并抛异常。

    续采策略：
    - 版本未知时用探针会话取第 0 页确认发布版并落盘；
    - 之后按该版本已确认保存的页数作为游标登录，站点为新会话从该游标签发新令牌；
    - 续采中会话失效只按当前已保存页数重登重试，不重新探针、不复用旧令牌、不重复抓页。
    """
    restarts = 0
    version: str | None = None
    total_pages = 0
    while True:
        if restarts > MAX_SESSION_RESTARTS:
            raise SiteError("restart_limit", "会话重建次数超限")
        try:
            if version is None:
                # 第一阶段：探针会话取第 0 页，确定入口当前发布版
                client.login(resume=0)
                probe = client.fetch_page(entry)
                METRICS.fetch_count += 1
                if probe["page_index"] != 0:
                    raise SiteError("protocol", "探针页序号应为 0")
                version = probe["version"]
                total_pages = probe["total_pages"]
                _ensure_version_row(conn, version, probe["published_at"], "")
                _save_or_verify_page(conn, entry, probe)

            # 第二阶段：按已保存页数续采；失效重登后也从这里继续，无需重新探针
            resume = _saved_page_count(conn, entry, version)
            if total_pages == 0:
                row = conn.execute(
                    "SELECT MIN(total_pages) t FROM pages WHERE version=? AND entry=?",
                    (version, entry)).fetchone()
                total_pages = row["t"] or 0
            if resume >= total_pages:
                print(f"  · {entry} 发布版 {version} 的 {total_pages} 页均已确认，整入口跳过")
                return version
            client.login(resume=resume)
            page_index = resume
            while True:
                page = client.fetch_page(entry)
                METRICS.fetch_count += 1
                if page["version"] != version:
                    # 采集中入口切版：本轮作废，下一轮从新版第 0 页重新探针
                    raise SiteError(
                        "entry_switch",
                        f"入口 {entry} 采集中从 {version} 切到 {page['version']}，本轮作废")
                if page["page_index"] != page_index:
                    raise SiteError("protocol",
                                    f"页序号不连续：期望 {page_index}，实际 {page['page_index']}")
                _save_or_verify_page(conn, entry, page)
                page_index += 1
                if page_index >= total_pages:
                    return version
        except SessionReset as exc:
            restarts += 1
            print(f"  ! 会话失效（{exc}），按已确认保存页数重新登录后续采，不重复抓取")
            continue
        except SiteError as exc:
            if version:
                _record_failure(conn, version, f"walk:{entry}", exc.kind, str(exc))
            raise


def _business_row_params(entry: str, version: str, records: list[dict]) -> list[tuple]:
    """把一页记录转成对应业务表的插入参数；调用方保证记录数 <= PAGE_SIZE。"""
    if entry == "registry":
        return [(version, r["catalog_no"], r["museum"], r["material"], r["weight_g"])
                for r in records]
    return [(version, edge_key(r["a"], r["b"]), r["a"], r["b"], r["bond"], r["confidence"])
            for r in records]


def _insert_business_rows(conn: sqlite3.Connection, entry: str, version: str,
                          records: list[dict]) -> None:
    """在*当前已开启的事务内*写入一页业务行。

    单页固定 <= 500 条，连同 pages 行一起提交，因此任何采集写事务的业务写入
    都不超过两页（1000 条）。唯一性冲突说明同版数据本身有问题，交给上层记录失败。
    """
    params = _business_row_params(entry, version, records)
    if len(params) > MAX_BUSINESS_ROWS_PER_TXN:
        # 护栏：排版页大小失控时直接拒绝，防止出现大事务
        raise SiteError("page_too_large",
                        f"单页 {len(params)} 条超过两页上限 {MAX_BUSINESS_ROWS_PER_TXN}")
    try:
        if entry == "registry":
            conn.executemany(
                "INSERT INTO sherds(version, catalog_no, museum, material, weight_g) "
                "VALUES (?, ?, ?, ?, ?)", params)
        else:
            conn.executemany(
                "INSERT INTO joins(version, edge_key, a, b, bond, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?)", params)
    except sqlite3.IntegrityError as exc:
        # 登记编号重复 / 拼接边重复：整版校验会给出明确原因，这里先让本轮失败
        raise SiteError("integrity_violation", f"{entry} 页内唯一约束冲突: {exc}")


def _save_or_verify_page(conn: sqlite3.Connection, entry: str, page: dict) -> None:
    """校验单页并在*一个事务*内同时落盘原始页与其业务记录。

    - 解析 + 校验和在事务外完成，事务内只有有界写入（一页 <= 500 行业务记录）；
    - 已存在且校验和/记录数一致则整页跳过：原始页与业务行当初同事务提交，
      重启后据此直接复用，不重新解析整版、不重复写库；
    - 不一致则报错，绝不静默覆盖。
    """
    page_index = page["page_index"]
    version = page["version"]
    if page.get("corrupted"):
        raise SiteError("corrupt_page",
                        f"{entry} 第 {page_index} 页被站点标记为损坏")
    try:
        records = parse_page(entry, page["layout"], page["body"])
    except ValueError as exc:
        raise SiteError("parse_error", f"{entry} 第 {page_index} 页解析失败: {exc}")
    if len(records) > PAGE_SIZE:
        raise SiteError("page_too_large",
                        f"{entry} 第 {page_index} 页 {len(records)} 条，超过固定页大小 {PAGE_SIZE}")
    METRICS.max_records_per_page = max(METRICS.max_records_per_page, len(records))
    if page_checksum(records) != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry} 第 {page_index} 页校验和不符，内容不可用")
    saved = conn.execute(
        "SELECT checksum, record_count FROM pages "
        "WHERE version=? AND entry=? AND page_index=?",
        (version, entry, page_index)).fetchone()
    if saved is not None:
        if saved["checksum"] != page["checksum"]:
            raise SiteError("checksum_mismatch",
                            f"{entry} 第 {page_index} 页与已保存内容不一致")
        if saved["record_count"] != len(records):
            raise SiteError("checksum_mismatch",
                            f"{entry} 第 {page_index} 页记录数与已保存页不一致")
        print(f"  · 跳过已确认保存的 {entry} 第 {page_index} 页（断点续采，业务行已入库）")
        return

    business_rows = len(records)
    # 单事务 = 1 行 pages + 同页业务行（<=500），远低于两页 1000 条的上限
    with conn:  # with 块正常退出即提交，任何异常整体回滚，不会出现半页数据
        conn.execute(
            "INSERT INTO pages(version, entry, page_index, total_pages, layout,"
            " checksum, body, record_count, saved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version, entry, page_index, page["total_pages"], page["layout"],
             page["checksum"], page["body"], business_rows, time.time()))
        _insert_business_rows(conn, entry, version, records)
    METRICS.record_write_txn(business_rows)


def validate_version(conn: sqlite3.Connection, version: str) -> tuple[bool, str]:
    """整版校验：页数齐全 + 逐页可解析 + 登记唯一 + 拼接引用完整。

    全程只读：一次只取一页正文到内存（流式），唯一性与引用完整性交给 SQL 集合
    运算，Python 侧不累积整版列表。因此大册校验期间 SQLite 不加写锁，查询服务
    仍可读取当前 active 版，复核撤销（写 revocations）也能正常提交。
    """
    for entry in ENTRIES:
        meta = conn.execute(
            "SELECT page_index, total_pages, layout, checksum, record_count "
            "FROM pages WHERE version=? AND entry=? ORDER BY page_index",
            (version, entry)).fetchall()
        if not meta:
            return False, f"{entry} 没有任何已保存页面"
        expected = meta[0]["total_pages"]
        if len(meta) != expected:
            return False, f"{entry} 页数不全：{len(meta)}/{expected}"
        if [p["page_index"] for p in meta] != list(range(expected)):
            return False, f"{entry} 页序号不连续"
        # 逐页回读正文并立即释放：同一时刻内存里至多一页
        for m in meta:
            row = conn.execute(
                "SELECT body FROM pages WHERE version=? AND entry=? AND page_index=?",
                (version, entry, m["page_index"])).fetchone()
            try:
                records = parse_page(entry, m["layout"], row["body"])
            except ValueError as exc:
                return False, f"{entry} 第 {m['page_index']} 页不可解析: {exc}"
            if page_checksum(records) != m["checksum"]:
                return False, f"{entry} 第 {m['page_index']} 页校验和不符"
            if len(records) != m["record_count"]:
                return False, (f"{entry} 第 {m['page_index']} 页记录数 "
                               f"{len(records)} 与入库登记 {m['record_count']} 不一致")

    # 登记唯一性：直接让 SQL 报告重复编号，不把整版编号拉进 Python
    dup = conn.execute(
        "SELECT catalog_no FROM sherds WHERE version=? "
        "GROUP BY catalog_no HAVING COUNT(*) > 1 LIMIT 1", (version,)).fetchone()
    if dup is not None:
        return False, f"登记清单存在重复馆藏编号: {dup['catalog_no']}"

    # 引用完整性：集合反查，缺失编号由数据库聚合，Python 只收一条结果
    missing = conn.execute(
        "SELECT j.side AS side FROM ("
        "  SELECT a AS side FROM joins WHERE version=?"
        "  UNION ALL SELECT b FROM joins WHERE version=? ) j "
        "LEFT JOIN sherds s ON s.version=? AND s.catalog_no=j.side "
        "WHERE s.catalog_no IS NULL LIMIT 1",
        (version, version, version)).fetchone()
    if missing is not None:
        return False, f"拼接清单引用了登记中不存在的陶片: {missing['side']}"
    return True, ""


def activate_version(conn: sqlite3.Connection, version: str) -> None:
    """收齐且校验成功后整体生效。

    业务记录已在采页时逐页入库，这里只剩一个轻量元数据事务：降旧版、升新版、
    写激活日志，任何一步失败旧 active 保持不变。
    """
    if get_active_version(conn) == version:
        # 同版重复进入（重启续采已激活等）：业务行与状态都在，直接幂等返回
        print(f"  · 发布版 {version} 已是当前 active，跳过重复激活")
        return
    ok, reason = validate_version(conn, version)
    if not ok:
        _record_failure(conn, version, "activate", "validation_failed", reason)
        raise SiteError("validation_failed", reason)

    published = conn.execute("SELECT published_at FROM versions WHERE version=?",
                             (version,)).fetchone()
    with conn:
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
    summary = {"versions_seen": [], "activated": None, "error": None}
    try:
        seen = {}
        for entry in ENTRIES:
            seen[entry] = walk_entry(client, conn, entry)
        if seen["registry"] != seen["joins"]:
            # 两入口切版时间不同：不标记失败、不激活，下一轮再看；查询保持旧版
            summary["error"] = (f"两个入口分属不同发布版：registry={seen['registry']}, "
                                f"joins={seen['joins']}，等待同版后再激活")
            return summary
        version = seen["registry"]
        summary["versions_seen"] = [version]
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
        for p in conn.execute("SELECT version, entry, COUNT(*) n, MIN(total_pages) total, "
                              "SUM(record_count) records "
                              "FROM pages GROUP BY version, entry ORDER BY version, entry"):
            print(f"  {p['version']:<10} {p['entry']:<9} {p['n']}/{p['total']} 页, "
                  f"已解析入库 {p['records']} 条")
        print("\n分版业务记录（随页逐页入库，激活仅切换状态）:")
        for v in conn.execute("SELECT version, "
                              "(SELECT COUNT(*) FROM sherds s WHERE s.version=v.version) sherd_n, "
                              "(SELECT COUNT(*) FROM joins j WHERE j.version=v.version) join_n "
                              "FROM versions v ORDER BY version"):
            print(f"  {v['version']:<10} 登记 {v['sherd_n']:>7} 条 / 拼接 {v['join_n']:>6} 条")
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
    try:
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
    finally:
        # 资源指标落盘：verify 用它对比 2 万/8 万大册的峰值内存与事务规模
        metrics_path = os.environ.get("CERAMICS_METRICS_FILE")
        if metrics_path:
            try:
                with open(metrics_path, "w", encoding="utf-8") as fh:
                    json.dump(METRICS.snapshot(), fh, ensure_ascii=False, indent=2)
            except OSError as exc:
                print(f"[采集] 指标写出失败: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
