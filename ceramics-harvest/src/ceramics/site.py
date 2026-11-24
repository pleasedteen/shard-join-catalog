"""本地模拟站：合作馆陶片登记/拼接清单发布服务。

行为覆盖需求中的全部站点故障与协议特性：
- 两个入口 /registry 和 /joins 可在*不同时间*切到不同发布版（控制接口单独设置）。
- 旧站两入口协议照常工作：不带 volume 的 GET /registry、/joins 返回整入口页面。
- 新分册协议：GET /registry?volume=R-1 返回该分册页面；GET /manifest 发现当前版本与分册清单。
- 每个站最多 MAX_PAGING_SESSIONS 个翻页会话名额（登录即占用）。暂停中的会话*仍占名额*，
  但当名额已满且有新分册要登录时，站点会收回一个暂停（或足够久空闲）的会话的名额，
  因此"两册同时暂停时第三册仍能在恢复前采完"。
- 没有别的分册竞争、会话也未过期时，暂停恢复沿用原会话与令牌，不强制重登。
- 会话有 TTL，过期后返回 401；站点可整体暂停，暂停期返回 503 + Retry-After。
- 翻页令牌按会话访问顺序发放，令牌绑定会话与游标；新会话无法使用旧令牌。
- 请求签名随会话 secret 和令牌变化。
- 页面排版按发布版变化（pipe/jsonl/record 三种），个别页可声明为损坏页。
"""
from __future__ import annotations

import json
import math
import secrets
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ceramics.protocol import issue_token, page_checksum, read_token, sign
from fixtures import VERSIONS

CONTROL_KEY = "ceramics-local-control"
ENTRIES = ("registry", "joins")
MAX_PAGING_SESSIONS = 2
# 超过该秒数没有任何访问的会话（多半是进程被杀的残留）可被新分册收回名额
STALE_GRACE = 3.0


def _format_page(records: list[dict], layout: str) -> str:
    if layout == "pipe":
        lines = ["|".join(str(v) for v in row.values()) for row in records]
        return "\n".join(lines)
    if layout == "jsonl":
        return "\n".join(json.dumps(row, ensure_ascii=False) for row in records)
    if layout == "record":
        blocks = []
        for row in records:
            blocks.append("\n".join(f"{k}: {v}" for k, v in row.items()))
        return "\n\n".join(blocks)
    raise ValueError(f"未知排版: {layout}")


def volume_rows(version: str, entry: str, volume: str) -> list[dict] | None:
    """某发布版某入口某分册的记录切片；分册不存在返回 None。"""
    spec = VERSIONS[version]
    vols = spec.get("volumes", {}).get(entry, [])
    if volume not in vols:
        return None
    idx = vols.index(volume)
    rows = spec[entry]
    n = len(rows)
    start, end = idx * n // len(vols), (idx + 1) * n // len(vols)
    return rows[start:end]


def volume_page_size(version: str, entry: str) -> int:
    spec = VERSIONS[version]
    return spec.get("volume_page_size", {}).get(entry, spec["page_size"][entry])


class SiteState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # 两个入口各自的当前发布版，初始都在 v1
        self.entry_versions = {"registry": "v1", "joins": "v1"}
        # sid -> {"secret","cursor","expires_at","entry","volume","state","last_seen"}
        self.sessions: dict[str, dict] = {}
        self.paused_until = 0.0
        self.session_ttl = 20.0
        # {(entry, version, volume): [损坏页序号...]}，volume 为空串代表整入口
        self.broken_pages: dict[tuple[str, str, str], list[int]] = {}
        # (entry, volume) -> 暂停截止时间；只暂停该分册的翻页
        self.volume_paused_until: dict[tuple[str, str], float] = {}
        self.page_delay = 0.0
        # 旧站模式：不提供 /manifest，只有两入口整册翻页协议（验证兼容性用）
        self.legacy = False
        # 可观测的名额事件与每页抓取次数，验证脚本据此断言"名额争用/未重复下载"
        self.events: deque[dict] = deque(maxlen=400)
        self.fetches: dict[tuple[str, str, int], int] = {}
        self.peak_paging = 0

    def event(self, kind: str, **detail) -> None:
        rec = {"at": round(time.time(), 3), "kind": kind}
        rec.update(detail)
        self.events.append(rec)

    def snapshot(self) -> dict:
        with self.lock:
            now = time.time()
            sessions = []
            for sid, s in self.sessions.items():
                if s["expires_at"] <= now:
                    continue
                sessions.append({
                    "sid": sid[:8], "entry": s["entry"], "volume": s["volume"],
                    "state": s["state"], "idle_for": round(now - s["last_seen"], 2),
                })
            return {
                "entry_versions": dict(self.entry_versions),
                "paused": now < self.paused_until,
                "paused_until": self.paused_until,
                "session_ttl": self.session_ttl,
                "active_sessions": len(sessions),
                "slot_limit": MAX_PAGING_SESSIONS,
                "sessions": sessions,
                "peak_paging": self.peak_paging,
                "page_delay": self.page_delay,
                "legacy": self.legacy,
                "volume_paused": {f"{k[0]}:{k[1]}": v
                                  for k, v in self.volume_paused_until.items() if v > now},
                "broken_pages": {f"{k[0]}@{k[1]}" + (f"@{k[2]}" if k[2] else ""): v
                                 for k, v in self.broken_pages.items()},
                "events": list(self.events)[-80:],
                "page_fetches": {f"{k[0]}/{k[1] or '-'}/{k[2]}": n
                                 for k, n in self.fetches.items()},
            }


STATE = SiteState()


def _page_payload(entry: str, version: str, index: int, volume: str | None) -> dict:
    spec = VERSIONS[version]
    layout = spec["layouts"][entry]
    vol_key = volume or ""
    if volume is not None:
        rows_all = volume_rows(version, entry, volume)
        if rows_all is None:
            raise KeyError(volume)
        size = volume_page_size(version, entry)
        rows = rows_all[index * size:(index + 1) * size]
        total = (len(rows_all) + size - 1) // size
    else:
        size = spec["page_size"][entry]
        rows = spec[entry][index * size:(index + 1) * size]
        total = (len(spec[entry]) + size - 1) // size
    broken = (index in STATE.broken_pages.get((entry, version, vol_key), [])
              or index in STATE.broken_pages.get((entry, version, ""), []))
    if broken:
        # 损坏页：内容被截断、校验和按完好内容计算，必然对不上
        body = _format_page(rows, layout)
        body = body[: max(10, len(body) // 2)] + "\n### PAGE CORRUPTED ###"
        checksum = page_checksum(rows)  # 完好内容的摘要，与实际 body 不符
    else:
        body = _format_page(rows, layout)
        checksum = page_checksum(rows)
    return {
        "entry": entry,
        "version": version,
        "volume": vol_key,
        "page_index": index,
        "total_pages": total,
        "layout": layout,
        "published_at": spec["published_at"],
        "corrupted": broken,
        "checksum": checksum,
        "body": body,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CeramicMock/2.0"

    def log_message(self, *args):  # 安静运行，验证输出只保留业务信息
        pass

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _paused(self) -> bool:
        if time.time() >= STATE.paused_until:
            return False
        retry = max(1, int(STATE.paused_until - time.time()))
        self._send(503, {"error": "site_paused", "message": "站点暂停受理，稍后恢复"},
                   {"Retry-After": retry})
        return True

    # ---- 名额管理 -------------------------------------------------------
    def _login(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        params = parse_qs(self.rfile.read(length).decode() or "")
        if params.get("key", [""])[0] != CONTROL_KEY:
            self._send(403, {"error": "bad_key"})
            return
        try:
            resume = int(params.get("resume", ["0"])[0])
            if resume < 0:
                raise ValueError
        except ValueError:
            self._send(400, {"error": "bad_resume"})
            return
        entry = params.get("entry", [""])[0] or None
        volume = params.get("volume", [""])[0] or None
        if entry is not None and entry not in ENTRIES:
            self._send(400, {"error": "bad_entry"})
            return
        if volume is not None and (entry is None or volume_rows(
                STATE.entry_versions[entry], entry, volume) is None):
            self._send(404, {"error": "unknown_volume"})
            return

        sid = uuid.uuid4().hex
        secret = secrets.token_hex(16)
        with STATE.lock:
            now = time.time()
            for dead in [k for k, s in STATE.sessions.items() if s["expires_at"] <= now]:
                STATE.sessions.pop(dead, None)
                STATE.event("session_expired", sid=dead[:8])
            holders = list(STATE.sessions.items())
            if len(holders) >= MAX_PAGING_SESSIONS:
                # 1) 暂停中的会话仍占名额，但可被需要名额的新分册收回
                candidates = sorted(
                    ((sid_, s) for sid_, s in holders if s["state"] == "paused"),
                    key=lambda x: x[1]["last_seen"])
                # 2) 长时间无请求的会话（多半是进程被杀的残留）也可收回
                if not candidates:
                    candidates = sorted(
                        ((sid_, s) for sid_, s in holders
                         if now - s["last_seen"] >= STALE_GRACE),
                        key=lambda x: x[1]["last_seen"])
                if not candidates:
                    STATE.event("wait", entry=entry, volume=volume,
                                holders=len(holders), reason="slots_busy")
                    self._send(429, {"error": "no_slot",
                                     "message": "翻页名额已满，稍后重试",
                                     "holders": len(holders)},
                               {"Retry-After": 1})
                    return
                victim_sid, victim = candidates[0]
                STATE.sessions.pop(victim_sid, None)
                STATE.event("evict", sid=victim_sid[:8], entry=victim["entry"],
                            volume=victim["volume"], state=victim["state"],
                            for_entry=entry, for_volume=volume)
            STATE.sessions[sid] = {
                "secret": secret, "cursor": resume, "expires_at": now + STATE.session_ttl,
                "entry": entry, "volume": volume, "state": "idle", "last_seen": now,
            }
            STATE.event("grant", sid=sid[:8], entry=entry, volume=volume,
                        holders=len(STATE.sessions))
        # 续采游标：站点从该游标起为新会话签发全新令牌链（不复用旧令牌）
        self._send(200, {"sid": sid, "secret": secret,
                         "expires_in": STATE.session_ttl,
                         "first_token": issue_token(sid, resume)})

    def _release(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        params = parse_qs(self.rfile.read(length).decode() or "")
        if params.get("key", [""])[0] != CONTROL_KEY:
            self._send(403, {"error": "bad_key"})
            return
        sid = params.get("sid", [""])[0]
        with STATE.lock:
            gone = STATE.sessions.pop(sid, None)
            if gone:
                STATE.event("release", sid=sid[:8], entry=gone["entry"],
                            volume=gone["volume"])
        self._send(200, {"ok": True, "released": bool(gone)})

    def _control(self) -> None:
        if self.headers.get("X-Control-Key") != CONTROL_KEY:
            self._send(403, {"error": "bad_control_key"})
            return
        length = int(self.headers.get("Content-Length", 0))
        cmd = json.loads(self.rfile.read(length).decode() or "{}")
        with STATE.lock:
            for entry in ENTRIES:
                if entry in cmd.get("entry_versions", {}):
                    STATE.entry_versions[entry] = cmd["entry_versions"][entry]
            if "pause_seconds" in cmd:
                STATE.paused_until = max(STATE.paused_until,
                                         time.time() + float(cmd["pause_seconds"]))
            if "resume" in cmd and cmd["resume"]:
                STATE.paused_until = time.time()
            if "session_ttl" in cmd:
                STATE.session_ttl = float(cmd["session_ttl"])
            if "expire_sessions" in cmd and cmd["expire_sessions"]:
                now = time.time()
                for sess in STATE.sessions.values():
                    sess["expires_at"] = now
            if "page_delay" in cmd:
                STATE.page_delay = float(cmd["page_delay"])
            if "reset_events" in cmd and cmd["reset_events"]:
                STATE.events.clear()
            if "reset_stats" in cmd and cmd["reset_stats"]:
                STATE.fetches.clear()
                STATE.peak_paging = 0
            if "reset_sessions" in cmd and cmd["reset_sessions"]:
                # 仅本地验证用：清空全部会话与名额占用，保证用例起点确定
                STATE.sessions.clear()
                STATE.events.append({"at": round(time.time(), 3), "kind": "reset_sessions"})
            if "legacy" in cmd:
                STATE.legacy = bool(cmd["legacy"])
            if "pause_volumes" in cmd:
                for name, secs in cmd["pause_volumes"].items():
                    entry, volume = name.split(":", 1)
                    key = (entry, volume)
                    STATE.volume_paused_until[key] = max(
                        STATE.volume_paused_until.get(key, 0.0),
                        time.time() + float(secs))
                    # 该分册的在线会话立即视为"暂停中"，仍占名额但可被新分册收回
                    for sess in STATE.sessions.values():
                        if sess["entry"] == entry and sess["volume"] == volume:
                            sess["state"] = "paused"
                    STATE.event("volume_pause", entry=entry, volume=volume, seconds=secs)
            if "resume_volumes" in cmd:
                for name in cmd["resume_volumes"]:
                    entry, volume = name.split(":", 1)
                    STATE.volume_paused_until.pop((entry, volume), None)
            if "broken_pages" in cmd:
                for name, indexes in cmd["broken_pages"].items():
                    parts = name.split("@")
                    entry, version = parts[0], parts[1]
                    volume = parts[2] if len(parts) > 2 else ""
                    STATE.broken_pages[(entry, version, volume)] = list(indexes)
            if "clear_broken" in cmd and cmd["clear_broken"]:
                STATE.broken_pages.clear()
        self._send(200, {"ok": True, "state": STATE.snapshot()})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/session":
            if self._paused():
                return
            self._login()
            return
        if parsed.path == "/release":
            self._release()
            return
        if parsed.path == "/control":
            self._control()
            return
        self._send(404, {"error": "not_found"})

    def _manifest(self) -> None:
        if STATE.legacy:
            self._send(404, {"error": "not_found"})
            return
        with STATE.lock:
            out = {}
            for entry in ENTRIES:
                version = STATE.entry_versions[entry]
                spec = VERSIONS.get(version, {})
                size = volume_page_size(version, entry)
                vols = []
                for name in spec.get("volumes", {}).get(entry, []):
                    rows = volume_rows(version, entry, name) or []
                    total = (len(rows) + size - 1) // size
                    vols.append({"name": name, "total_pages": total,
                                 "layout": spec["layouts"][entry]})
                out[entry] = {
                    "version": version,
                    "volumes": vols,
                    "published_at": spec.get("published_at"),
                }
        self._send(200, {"entries": out, "slot_limit": MAX_PAGING_SESSIONS})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/manifest":
            self._manifest()
            return
        if parsed.path not in ("/registry", "/joins"):
            self._send(404, {"error": "not_found"})
            return
        if self._paused():
            return

        entry = parsed.path.lstrip("/")
        query = parse_qs(parsed.query)
        volume = query.get("volume", [None])[0] or None
        if volume is not None:
            with STATE.lock:
                version_now = STATE.entry_versions[entry]
            if volume_rows(version_now, entry, volume) is None:
                self._send(404, {"error": "unknown_volume", "volume": volume})
                return

        sid = self.headers.get("X-Ceramic-Session", "")
        token = self.headers.get("X-Ceramic-Token", "")
        signature = self.headers.get("X-Ceramic-Signature", "")
        pause_retry = None
        with STATE.lock:
            sess = STATE.sessions.get(sid)
            if sess is None:
                self._send(401, {"error": "unknown_session",
                                 "message": "会话不存在，请重新登录"})
                return
            now = time.time()
            if now >= sess["expires_at"]:
                STATE.sessions.pop(sid, None)
                STATE.event("session_expired", sid=sid[:8])
                self._send(401, {"error": "session_expired",
                                 "message": "会话已过期，请重新登录"})
                return
            expected = sign(sess["secret"], "GET", parsed.path, sid, token)
            if not signature or not secrets.compare_digest(expected, signature):
                self._send(403, {"error": "bad_signature",
                                 "message": "签名与会话或令牌不匹配"})
                return
            try:
                claim = read_token(token)
            except Exception:
                self._send(403, {"error": "bad_token"})
                return
            if claim.get("sid") != sid:
                self._send(403, {"error": "token_session_mismatch",
                                 "message": "新会话不能使用旧令牌"})
                return
            if sess["entry"] and sess["entry"] != entry:
                self._send(403, {"error": "wrong_entry",
                                 "message": "该会话绑定其它入口"})
                return
            if sess["volume"] and volume != sess["volume"]:
                self._send(403, {"error": "wrong_volume",
                                 "message": "该会话绑定其它分册"})
                return
            cursor = claim.get("cursor")
            if not isinstance(cursor, int) or cursor != sess["cursor"]:
                self._send(409, {"error": "token_out_of_order",
                                 "message": "令牌必须按站点发放顺序使用",
                                 "expected_cursor": sess["cursor"]})
                return
            vol_key = (entry, volume or "")
            pause_until = STATE.volume_paused_until.get(vol_key, 0.0)
            if pause_until > now:
                # 该分册暂停：会话仍占用名额，令牌与游标都不消耗，恢复后沿用
                sess["state"] = "paused"
                sess["last_seen"] = now
                STATE.event("pause_hold", sid=sid[:8], entry=entry,
                            volume=volume or "-")
                pause_retry = max(1, math.ceil(pause_until - now))
            else:
                # 校验通过，按访问顺序为下一页发放新令牌
                if sess["state"] == "paused":
                    # 暂停结束、会话未过期且名额未被收回：沿用原会话与令牌链继续
                    STATE.event("resume_same_session", sid=sid[:8], entry=entry,
                                volume=volume or "-")
                sess["cursor"] += 1
                sess["state"] = "paging"
                sess["last_seen"] = now
                paging = sum(1 for s in STATE.sessions.values()
                             if s["state"] == "paging"
                             and s["expires_at"] > now)
                STATE.peak_paging = max(STATE.peak_paging, paging)
                next_token = issue_token(sid, sess["cursor"])
                version = STATE.entry_versions[entry]
                delay = STATE.page_delay

        if pause_retry is not None:
            self._send(503, {"error": "volume_paused",
                             "message": "该分册暂停受理，稍后恢复"},
                       {"Retry-After": pause_retry})
            return

        if delay:
            time.sleep(delay)
        payload = _page_payload(entry, version, cursor, volume)
        payload["next_token"] = next_token
        with STATE.lock:
            live = STATE.sessions.get(sid)
            if live is not None:
                live["state"] = "idle"
                live["last_seen"] = time.time()
            key = (entry, volume or "", cursor)
            STATE.fetches[key] = STATE.fetches.get(key, 0) + 1
        self._send(200, payload)


def serve(host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main() -> None:
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="合作馆陶片清单本地模拟站")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    httpd = serve(args.host, args.port)
    print(f"模拟站已启动: http://{args.host}:{args.port}")
    print(f"控制密钥: {CONTROL_KEY}（仅本地验证使用）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
