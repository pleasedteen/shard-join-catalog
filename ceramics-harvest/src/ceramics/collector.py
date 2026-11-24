"""采集服务：按会话协议并行采集各分册，校验后按发布版原子激活。

两种协议：
- 分册协议（站点提供 GET /manifest）：按分册并行采集，每站最多两个翻页会话名额。
- 旧站两入口协议（无 /manifest）：沿用 GET /registry、/joins 整入口翻页，照常工作。

关键保证：
- 名额：最多两个翻页会话/站；暂停中的会话*仍占名额*，但当其它分册要名额时，
  采集端让出最久暂停的会话名额（模拟站随后收回其服务端会话），因此两册同时暂停时
  第三册仍能在恢复前采完。没有别册等名额且会话未过期时，暂停恢复沿用原会话与令牌。
- 401 过期 / 令牌跨会话 / 名额被让出：重新登录，并从*该分册*已确认保存的页数续采；
  登录游标直接跳过已存正文，不重复下载。
- 进程重启：已确认页面落盘即提交事务；新进程读 /manifest 后按分册缺口续采。
- 登记与拼接必须同属一个发布版、所有分册按清单收齐、每页校验通过、拼接引用完整，
  才在一个事务里切换 active；采集与校验期间查询继续命中旧 active 版本。
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
from concurrent.futures import ThreadPoolExecutor, as_completed

from ceramics.db import connect, get_active_version, init_db
from ceramics.parsing import parse_page
from ceramics.protocol import edge_key, page_checksum, sign

ENTRIES = ("registry", "joins")
MAX_SESSION_RESTARTS = 100
SLOT_LIMIT = 2  # 每站最多两个翻页会话名额


class SiteError(Exception):
    def __init__(self, kind: str, message: str, retry_after: int | None = None,
                 code: str = ""):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.code = code


class SessionReset(Exception):
    """会话失效或名额被让出，需要重新登录并从该分册已保存页数续采。"""


class SlotLost(SessionReset):
    """名额被让给其它分册：重新申请时不再抢占，只等真正空位，避免互相踢下线。"""


class EntryVersionChanged(Exception):
    """采集中入口切版，本轮作废，下一轮按新清单重来。"""


# --------------------------------------------------------------------------
# 名额池：每站最多 SLOT_LIMIT 个并发翻页会话；暂停中的会话占名额但可被让位。
# --------------------------------------------------------------------------
class Lease:
    def __init__(self, key: tuple[str, str]):
        self.key = key
        self.state = "active"          # active / paused
        self.pause_kind: str | None = None  # None / "global" / "volume"
        self.paused_at = 0.0
        self.transferred = False       # 名额已让给其它分册，释放时不再归还
        self.lost = threading.Event()

    def mark_paused(self, kind: str) -> None:
        self.state = "paused"
        self.pause_kind = kind
        self.paused_at = time.time()

    def mark_active(self) -> None:
        self.state = "active"
        self.pause_kind = None


class SlotPool:
    """每站翻页名额池。

    名额满时，只有"因*分册*暂停而占位"的会话可被让位；全局暂停时所有翻页都取不到内容，
    让位没有意义，等待即可。被让位的采集方重新申请时不再抢占，只等真正的空位，
    避免两个暂停分册互相把对方踢下线造成活锁。
    """

    def __init__(self, limit: int = SLOT_LIMIT):
        self.limit = limit
        self.cond = threading.Condition()
        self.leases: dict[tuple[str, str], Lease] = {}
        self.events: list[dict] = []   # 供验证/日志读取的名额事件

    @property
    def free(self) -> int:
        return self.limit - len(self.leases)

    def _log(self, kind: str, **detail) -> None:
        rec = {"at": round(time.time(), 3), "kind": kind}
        rec.update(detail)
        self.events.append(rec)

    def acquire(self, key: tuple[str, str], allow_preempt: bool = True) -> Lease:
        with self.cond:
            while True:
                if self.free > 0:
                    lease = Lease(key)
                    self.leases[key] = lease
                    self._log("acquire", volume=key, free=self.free)
                    return lease
                if allow_preempt:
                    paused = sorted(
                        (l for l in self.leases.values()
                         if l.state == "paused" and l.pause_kind == "volume"
                         and not l.lost.is_set()),
                        key=lambda l: l.paused_at)
                    if paused:
                        victim = paused[0]
                        # 名额直接转给申请者：让位者从池中移除，它随后 release 不再改动
                        victim.transferred = True
                        victim.lost.set()
                        self.leases.pop(victim.key, None)
                        lease = Lease(key)
                        self.leases[key] = lease
                        self._log("preempt", victim=list(victim.key), claimant=key,
                                  note="让出分册暂停占用的名额，第三册可在恢复前采集")
                        return lease
                self._log("wait", volume=key, free=0,
                          holders=[list(k) for k in self.leases])
                self.cond.wait(timeout=0.5)

    def release(self, lease: Lease) -> None:
        with self.cond:
            if self.leases.get(lease.key) is lease:
                self.leases.pop(lease.key, None)
                self._log("release", volume=lease.key, free=self.free)
            else:
                self._log("release_transferred", volume=lease.key)
            self.cond.notify_all()

    def pause_events(self) -> list[dict]:
        with self.cond:
            return [e for e in self.events if e["kind"] in ("preempt", "wait")]


# --------------------------------------------------------------------------
# 站点客户端
# --------------------------------------------------------------------------
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
                code = payload.get("error", "")
                raise SiteError("paused", payload.get("message", "站点暂停"),
                                retry_after=min(retry, self.pause_cap), code=code)
            if exc.code == 429:
                retry = int(exc.headers.get("Retry-After", 1))
                raise SiteError("no_slot", payload.get("message", "名额已满"),
                                retry_after=retry)
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

    def manifest(self) -> dict | None:
        """站点清单；旧站无此接口（404）时返回 None，走两入口整入口协议。"""
        try:
            return self._request("GET", "/manifest")
        except SiteError as exc:
            if exc.kind == "http" and "HTTP 404" in str(exc):
                return None
            raise

    def login(self, resume: int = 0, entry: str | None = None,
              volume: str | None = None) -> None:
        # resume 报告该分册已确认保存的页数，站点从该游标签发*新会话*的首令牌，
        # 因此无需复用任何旧令牌，也不会重复下载已存正文。
        # 重登前先归还旧会话，避免失效会话继续占用每站两个翻页名额。
        self.release()
        body = f"key={self.key}&resume={int(resume)}".encode()
        if entry:
            body += f"&entry={entry}".encode()
        if volume:
            body += f"&volume={volume}".encode()
        data = self._request("POST", "/session", body,
                             {"Content-Type": "application/x-www-form-urlencoded"})
        self.sid = data["sid"]
        self.secret = data["secret"]
        self.token = data["first_token"]

    def release(self) -> None:
        sid, self.sid = self.sid, None
        if not sid:
            return
        try:
            self._request("POST", "/release",
                          f"key={self.key}&sid={sid}".encode(),
                          {"Content-Type": "application/x-www-form-urlencoded"})
        except Exception:
            pass

    def fetch_page(self, entry: str, volume: str | None = None) -> dict:
        """取当前令牌对应的一页。暂停/名额问题以 SiteError 抛出，由调用方决定等待。"""
        path = f"/{entry}" + (f"?volume={volume}" if volume else "")
        signature = sign(self.secret, "GET", path.split("?", 1)[0], self.sid, self.token)
        page = self._request("GET", path, headers={
            "X-Ceramic-Session": self.sid,
            "X-Ceramic-Token": self.token,
            "X-Ceramic-Signature": signature,
        })
        self.token = page["next_token"]
        return page


# --------------------------------------------------------------------------
# 持久化辅助
# --------------------------------------------------------------------------
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
                        note: str = "") -> None:
    conn.execute(
        "INSERT INTO versions(version, status, published_at, note, updated_at) "
        "VALUES (?, 'collecting', ?, ?, ?) "
        "ON CONFLICT(version) DO UPDATE SET updated_at=excluded.updated_at",
        (version, published_at, note, time.time()))
    conn.commit()


def _register_manifest(conn: sqlite3.Connection, version: str, entry: str,
                       volumes: list[dict]) -> None:
    """按站点清单登记该版本该入口的全部应采分册（含页数）。"""
    for ordinal, vol in enumerate(volumes):
        conn.execute(
            "INSERT INTO volume_manifest(version, entry, volume, ordinal, total_pages) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(version, entry, volume) DO UPDATE SET "
            "ordinal=excluded.ordinal, total_pages=excluded.total_pages",
            (version, entry, vol["name"], ordinal, vol["total_pages"]))
    conn.commit()


def _saved_page_count(conn: sqlite3.Connection, entry: str, version: str | None,
                      volume: str = "") -> int:
    """该入口该分册在指定版本下已确认保存的连续页数（从 0 起）。"""
    if not version:
        return 0
    rows = conn.execute(
        "SELECT page_index FROM pages WHERE version=? AND entry=? AND volume=? "
        "ORDER BY page_index", (version, entry, volume)).fetchall()
    indexes = {r["page_index"] for r in rows}
    count = 0
    while count in indexes:
        count += 1
    return count


def _save_or_verify_page(conn: sqlite3.Connection, entry: str, volume: str,
                         page: dict, quiet: bool = False) -> None:
    """校验单页并落盘；已存在且校验和一致则跳过，不一致则报错。"""
    page_index = page["page_index"]
    version = page["version"]
    if page.get("corrupted"):
        raise SiteError("corrupt_page",
                        f"{entry}/{volume or '整册'} 第 {page_index} 页被站点标记为损坏")
    try:
        records = parse_page(entry, page["layout"], page["body"])
    except ValueError as exc:
        raise SiteError("parse_error",
                        f"{entry}/{volume or '整册'} 第 {page_index} 页解析失败: {exc}")
    if page_checksum(records) != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry}/{volume or '整册'} 第 {page_index} 页校验和不符，内容不可用")
    saved = conn.execute(
        "SELECT checksum FROM pages WHERE version=? AND entry=? AND volume=? "
        "AND page_index=?", (version, entry, volume, page_index)).fetchone()
    if saved is None:
        conn.execute(
            "INSERT INTO pages(version, entry, volume, page_index, total_pages, layout,"
            " checksum, body, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (version, entry, volume, page_index, page["total_pages"], page["layout"],
             page["checksum"], page["body"], time.time()))
        conn.commit()
    elif saved["checksum"] != page["checksum"]:
        raise SiteError("checksum_mismatch",
                        f"{entry}/{volume or '整册'} 第 {page_index} 页与已保存内容不一致")
    elif not quiet:
        print(f"  · 跳过已确认保存的 {entry}/{volume or '整册'} 第 {page_index} 页（断点续采）")


# --------------------------------------------------------------------------
# 分册采集
# --------------------------------------------------------------------------
def _wait_pause(lease: Lease | None, pool: SlotPool | None, seconds: float,
                kind: str = "volume") -> None:
    """暂停等待；若名额被让出则抛 SlotLost。等待结束恢复同一会话继续。

    kind: "global"（整站暂停）或 "volume"（仅本分册暂停）。只有分册暂停占用的名额
    可被其它分册让位。
    """
    deadline = time.time() + max(0.2, seconds)
    if lease is not None:
        lease.mark_paused(kind)
    while True:
        if lease is not None and lease.lost.is_set():
            raise SlotLost("名额已让给其它分册，重新登录后按已保存页续采")
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if lease is not None:
            lease.lost.wait(min(0.1, remaining))
        else:
            time.sleep(min(0.1, remaining))
    if lease is not None and lease.lost.is_set():
        raise SlotLost("名额已让给其它分册，重新登录后按已保存页续采")
    if lease is not None:
        lease.mark_active()


def collect_volume(base_url: str, db_path: str, entry: str, volume: str,
                   version: str, total_pages: int,
                   pool: SlotPool, tag: str = "") -> tuple[str, int]:
    """并行采集一个分册；返回该分册所属版本与已确认页数。

    续采：登录游标 = 该分册已确认连续页数，站点从该游标发新令牌，已存正文不重下。
    """
    conn = connect(db_path)
    client = SiteClient(base_url)
    label = f"{entry}/{volume}"
    restarts = 0
    lease: Lease | None = None
    # 一旦被让位过，重新申请名额时不再抢占（只等空位），避免两册互踢的活锁
    no_preempt = False
    try:
        while True:
            if restarts > MAX_SESSION_RESTARTS:
                raise SiteError("restart_limit", f"{label} 会话重建次数超限")
            resume = _saved_page_count(conn, entry, version, volume)
            if resume >= total_pages:
                if tag:
                    print(f"  · {tag} {label} 的 {total_pages} 页均已确认（断点续采），跳过采集")
                return version, resume
            if lease is None:
                lease = pool.acquire((entry, volume), allow_preempt=not no_preempt)
            try:
                while True:
                    try:
                        client.login(resume=resume, entry=entry, volume=volume)
                        break
                    except SiteError as exc:
                        if exc.kind in ("paused", "no_slot"):
                            _wait_pause(lease, pool, exc.retry_after or 1,
                                        "global" if exc.code == "site_paused" else "volume")
                            continue
                        raise
                page_index = resume
                while page_index < total_pages:
                    try:
                        page = client.fetch_page(entry, volume)
                    except SiteError as exc:
                        if exc.kind in ("paused", "no_slot"):
                            # 暂停/名额问题：沿用原会话与令牌，等待后重试同一令牌
                            _wait_pause(lease, pool, exc.retry_after or 1,
                                        "global" if exc.code == "site_paused" else "volume")
                            continue
                        raise
                    if page["version"] != version:
                        raise EntryVersionChanged(
                            f"{label} 采集中从 {version} 切到 {page['version']}")
                    if page["page_index"] != page_index:
                        raise SiteError("protocol",
                                        f"{label} 页序号不连续：期望 {page_index}，"
                                        f"实际 {page['page_index']}")
                    _save_or_verify_page(conn, entry, volume, page, quiet=bool(tag))
                    page_index += 1
                return version, page_index
            except SlotLost as exc:
                # 名额被让出：丢掉本会话，下次申请名额时只等空位
                restarts += 1
                no_preempt = True
                client.release()
                if lease is not None:
                    pool.release(lease)
                    lease = None
                print(f"  · {label} 名额让给其它分册，稍后从已确认页续采")
                time.sleep(0.05)
                continue
            except SessionReset as exc:
                restarts += 1
                print(f"  ! {label} 会话失效（{exc}），按已确认保存页数重新登录续采，"
                      f"不重复抓取")
                continue
    finally:
        if lease is not None:
            client.release()
            pool.release(lease)
        conn.close()


def collect_legacy_entry(base_url: str, db_path: str, entry: str,
                         pool: SlotPool) -> str:
    """旧站两入口协议：整入口翻页（无分册），照常工作。"""
    conn = connect(db_path)
    client = SiteClient(base_url)
    lease: Lease | None = None
    version: str | None = None
    total_pages = 0
    restarts = 0
    try:
        while True:
            if restarts > MAX_SESSION_RESTARTS:
                raise SiteError("restart_limit", f"{entry} 会话重建次数超限")
            if lease is None:
                lease = pool.acquire((entry, ""))
            try:
                resume = _saved_page_count(conn, entry, version, "")
                if version is None or total_pages == 0:
                    # 探针：取第 0 页确定入口当前发布版
                    while True:
                        try:
                            client.login(resume=0, entry=entry)
                            break
                        except SiteError as exc:
                            if exc.kind in ("paused", "no_slot"):
                                _wait_pause(lease, pool, exc.retry_after or 1)
                                continue
                            raise
                    probe = client.fetch_page(entry)
                    if probe["page_index"] != 0:
                        raise SiteError("protocol", "探针页序号应为 0")
                    version = probe["version"]
                    total_pages = probe["total_pages"]
                    _ensure_version_row(conn, version, probe["published_at"])
                    _save_or_verify_page(conn, entry, "", probe)
                    resume = _saved_page_count(conn, entry, version, "")
                if resume >= total_pages:
                    return version
                while True:
                    try:
                        client.login(resume=resume, entry=entry)
                        break
                    except SiteError as exc:
                        if exc.kind in ("paused", "no_slot"):
                            _wait_pause(lease, pool, exc.retry_after or 1,
                                        "global" if exc.code == "site_paused" else "volume")
                            continue
                        raise
                page_index = resume
                while page_index < total_pages:
                    try:
                        page = client.fetch_page(entry)
                    except SiteError as exc:
                        if exc.kind in ("paused", "no_slot"):
                            _wait_pause(lease, pool, exc.retry_after or 1,
                                        "global" if exc.code == "site_paused" else "volume")
                            continue
                        raise
                    if page["version"] != version:
                        raise EntryVersionChanged(
                            f"入口 {entry} 采集中从 {version} 切到 {page['version']}")
                    if page["page_index"] != page_index:
                        raise SiteError("protocol",
                                        f"{entry} 页序号不连续：期望 {page_index}，"
                                        f"实际 {page['page_index']}")
                    _save_or_verify_page(conn, entry, "", page)
                    page_index += 1
                return version
            except SessionReset as exc:
                restarts += 1
                print(f"  ! 会话失效（{exc}），按已确认保存页数重新登录后续采，不重复抓取")
                continue
    finally:
        if lease is not None:
            client.release()
            pool.release(lease)
        conn.close()


# --------------------------------------------------------------------------
# 校验与激活
# --------------------------------------------------------------------------
def _entry_pages(conn: sqlite3.Connection, version: str, entry: str) -> list[sqlite3.Row]:
    """该版本该入口全部页面：旧协议按 page_index；分册按清单顺序 + 页序号。"""
    manifest = conn.execute(
        "SELECT volume, ordinal, total_pages FROM volume_manifest "
        "WHERE version=? AND entry=? ORDER BY ordinal, volume", (version, entry)).fetchall()
    if not manifest:
        return conn.execute(
            "SELECT * FROM pages WHERE version=? AND entry=? ORDER BY page_index",
            (version, entry)).fetchall()
    pages: list[sqlite3.Row] = []
    for vol in manifest:
        rows = conn.execute(
            "SELECT * FROM pages WHERE version=? AND entry=? AND volume=? "
            "ORDER BY page_index", (version, entry, vol["volume"])).fetchall()
        pages.extend(rows)
    return pages


def _volume_status(conn: sqlite3.Connection, version: str, entry: str) -> tuple[bool, str]:
    manifest = conn.execute(
        "SELECT volume, total_pages FROM volume_manifest WHERE version=? AND entry=? "
        "ORDER BY ordinal, volume", (version, entry)).fetchall()
    if not manifest:
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
        return True, ""
    for vol in manifest:
        if vol["total_pages"] is None:
            return False, f"{entry}/{vol['volume']} 尚未采到任何页面"
        pages = conn.execute(
            "SELECT page_index FROM pages WHERE version=? AND entry=? AND volume=? "
            "ORDER BY page_index", (version, entry, vol["volume"])).fetchall()
        if len(pages) != vol["total_pages"]:
            return False, (f"{entry}/{vol['volume']} 页数不全："
                           f"{len(pages)}/{vol['total_pages']}")
        if [p["page_index"] for p in pages] != list(range(vol["total_pages"])):
            return False, f"{entry}/{vol['volume']} 页序号不连续"
    return True, ""


def validate_version(conn: sqlite3.Connection, version: str) -> tuple[bool, str]:
    """所有分册收齐 + 可解析 + 登记唯一 + 拼接引用完整性。"""
    for entry in ENTRIES:
        ok, reason = _volume_status(conn, version, entry)
        if not ok:
            return False, reason
        for p in _entry_pages(conn, version, entry):
            try:
                parse_page(entry, p["layout"], p["body"])
            except ValueError as exc:
                return False, f"{entry} 第 {p['page_index']} 页不可解析: {exc}"

    registry_rows = []
    for p in _entry_pages(conn, version, "registry"):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    catalogs = [r["catalog_no"] for r in registry_rows]
    if len(set(catalogs)) != len(catalogs):
        return False, "登记清单存在重复馆藏编号"

    join_rows = []
    for p in _entry_pages(conn, version, "joins"):
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
    for p in _entry_pages(conn, version, "registry"):
        registry_rows.extend(parse_page("registry", p["layout"], p["body"]))
    for p in _entry_pages(conn, version, "joins"):
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


# --------------------------------------------------------------------------
# 一轮采集
# --------------------------------------------------------------------------
def _collect_manifest_round(base_url: str, db_path: str, manifest: dict,
                            pool: SlotPool, summary: dict) -> None:
    entries = manifest.get("entries", {})
    seen: dict[str, str] = {}
    tasks = []
    conn0 = connect(db_path)
    try:
        for entry in ENTRIES:
            info = entries.get(entry)
            if not info:
                summary["error"] = f"站点清单缺少入口 {entry}"
                return
            version = info["version"]
            seen[entry] = version
            volumes = info.get("volumes") or []
            if not volumes:
                summary["error"] = f"入口 {entry} 清单没有分册"
                return
            _ensure_version_row(conn0, version, info.get("published_at") or "")
            _register_manifest(conn0, version, entry, volumes)
            for vol in volumes:
                tasks.append((entry, vol["name"], version, vol["total_pages"]))
    finally:
        conn0.close()

    if seen["registry"] != seen["joins"]:
        summary["error"] = (f"两个入口分属不同发布版：registry={seen['registry']}, "
                            f"joins={seen['joins']}，等待同版后再激活")

    errors: list[Exception] = []
    aborted = False
    with ThreadPoolExecutor(max_workers=max(2, len(tasks))) as ex:
        futures = {
            ex.submit(collect_volume, base_url, db_path, entry, volume, version,
                      total, pool, f"[{entry}/{volume}]"): (entry, volume)
            for entry, volume, version, total in tasks
        }
        for fut in as_completed(futures):
            entry, volume = futures[fut]
            try:
                got_version, saved = fut.result()
                print(f"  · {entry}/{volume} 采集完成：版本 {got_version}，"
                      f"已确认 {saved} 页")
            except EntryVersionChanged as exc:
                aborted = True
                summary.setdefault("notes", []).append(str(exc))
                print(f"  ! {exc}，本轮作废")
            except SiteError as exc:
                errors.append(exc)
                if seen.get(entry):
                    c = connect(db_path)
                    _record_failure(c, seen[entry], f"walk:{entry}/{volume}",
                                    exc.kind, str(exc))
                    c.close()

    if errors:
        summary["error"] = f"{errors[0].kind}: {errors[0]}"
        return
    if aborted:
        summary["error"] = "采集中入口或分册切版，本轮作废，下一轮按新清单重来"
        return
    if seen["registry"] != seen["joins"]:
        summary["versions_seen"] = sorted(set(seen.values()))
        return
    version = seen["registry"]
    summary["versions_seen"] = [version]
    conn = connect(db_path)
    try:
        activate_version(conn, version)
        summary["activated"] = version
    except SiteError as exc:
        summary["error"] = f"{exc.kind}: {exc}"
    finally:
        conn.close()


def _collect_legacy_round(base_url: str, db_path: str, pool: SlotPool,
                          summary: dict) -> None:
    seen = {}
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(collect_legacy_entry, base_url, db_path, entry, pool): entry
                   for entry in ENTRIES}
        for fut in as_completed(futures):
            entry = futures[fut]
            try:
                seen[entry] = fut.result()
            except EntryVersionChanged as exc:
                summary.setdefault("notes", []).append(str(exc))
                print(f"  ! {exc}，本轮作废")
            except SiteError as exc:
                errors.append(exc)
    if errors:
        summary["error"] = f"{errors[0].kind}: {errors[0]}"
        return
    if len(seen) < 2:
        summary["error"] = "两入口未全部采集完成"
        summary["versions_seen"] = sorted(set(seen.values()))
        return
    if seen["registry"] != seen["joins"]:
        # 两入口切版时间不同：不标记失败、不激活，下一轮再看；查询保持旧版
        summary["error"] = (f"两个入口分属不同发布版：registry={seen['registry']}, "
                            f"joins={seen['joins']}，等待同版后再激活")
        summary["versions_seen"] = sorted(set(seen.values()))
        return
    version = seen["registry"]
    summary["versions_seen"] = [version]
    conn = connect(db_path)
    try:
        activate_version(conn, version)
        summary["activated"] = version
    except SiteError as exc:
        summary["error"] = f"{exc.kind}: {exc}"
    finally:
        conn.close()


def run_round(base_url: str, db_path: str, pool: SlotPool | None = None) -> dict:
    init_db(db_path)
    client = SiteClient(base_url)
    summary: dict = {"versions_seen": [], "activated": None, "error": None,
                     "protocol": "legacy", "slot_events": []}
    pool = pool or SlotPool(SLOT_LIMIT)
    try:
        manifest = client.manifest()
        if manifest is None:
            print("  · 站点未提供分册清单，沿用旧站两入口协议")
            _collect_legacy_round(base_url, db_path, pool, summary)
        else:
            summary["protocol"] = "volumes"
            _collect_manifest_round(base_url, db_path, manifest, pool, summary)
        return summary
    finally:
        summary["slot_events"] = pool.pause_events()


def print_status(db_path: str) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        active = get_active_version(conn)
        print(f"当前查询版本(active): {active or '（尚无可用版本）'}")
        print("\n发布版状态:")
        for v in conn.execute("SELECT version, status, published_at, note FROM versions "
                              "ORDER BY version"):
            print(f"  {v['version']:<8} {v['status']:<10} {v['published_at'] or ''}  "
                  f"{v['note'] or ''}")
        print("\n已保存页面（按分册）:")
        for p in conn.execute("SELECT version, entry, volume, COUNT(*) n, "
                              "MIN(total_pages) total FROM pages "
                              "GROUP BY version, entry, volume "
                              "ORDER BY version, entry, volume"):
            print(f"  {p['version']:<8} {p['entry']:<9} {p['volume'] or '整册':<6} "
                  f"{p['n']}/{p['total']} 页")
        print("\n分册清单:")
        manifest = conn.execute("SELECT version, entry, volume, total_pages "
                                "FROM volume_manifest ORDER BY version, entry, ordinal")
        rows = manifest.fetchall()
        if not rows:
            print("  （旧站协议，无分册清单）")
        for r in rows:
            print(f"  {r['version']:<8} {r['entry']:<9} {r['volume']:<6} "
                  f"{r['total_pages'] if r['total_pages'] is not None else '?'} 页")
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
    pool = SlotPool(SLOT_LIMIT)
    while True:
        rounds_done += 1
        print(f"[采集] 第 {rounds_done} 轮开始 {time.strftime('%H:%M:%S')}")
        result = run_round(args.base_url, args.db, pool)
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
