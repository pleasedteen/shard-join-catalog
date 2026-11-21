"""本地模拟站：多合作馆陶片登记/拼接清单发布服务。

一个进程同时模拟多个合作馆，每个馆是一个独立站点：
- 路径前缀 /<site>/... 标识馆；每个馆的登记 /registry 与拼接 /joins 两个入口，
  各自可在不同时间切到不同发布版（控制接口按馆单独设置）。
- 每个馆有独立的会话空间、暂停状态、损坏页声明；一馆暂停不影响其它馆。
- 会话有 TTL，过期返回 401；暂停期返回 503 + Retry-After 恢复时间。
- 翻页令牌按会话访问顺序发放，令牌绑定会话与游标；新会话无法使用旧令牌。
- 请求签名随会话 secret 和令牌变化。
- 页面排版按发布版变化（pipe/jsonl/record），个别页可声明为损坏页。
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ceramics.protocol import issue_token, page_checksum, read_token, sign
from fixtures import SITES

CONTROL_KEY = "ceramics-local-control"
ENTRIES = ("registry", "joins")


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


class SiteRuntime:
    """单个合作馆的运行状态。"""

    def __init__(self, site: str, spec: dict) -> None:
        self.site = site
        self.spec = spec
        self.versions = spec["versions"]
        initial = spec.get("initial_version", "v1")
        self.entry_versions = {"registry": initial, "joins": initial}
        self.sessions: dict[str, dict] = {}
        self.paused_until = 0.0
        self.session_ttl = 20.0
        self.broken_pages: dict[tuple[str, str], list[int]] = {}

    def page_payload(self, entry: str, version: str, index: int) -> dict:
        spec = self.versions[version]
        size = spec["page_size"][entry]
        rows = spec[entry][index * size:(index + 1) * size]
        total = (len(spec[entry]) + size - 1) // size
        broken = index in self.broken_pages.get((entry, version), [])
        body = _format_page(rows, spec["layouts"][entry])
        checksum = page_checksum(rows)
        if broken:
            # 损坏页：内容被截断、校验和按完好内容计算，必然对不上
            body = body[: max(10, len(body) // 2)] + "\n### PAGE CORRUPTED ###"
        return {
            "site": self.site,
            "entry": entry,
            "version": version,
            "page_index": index,
            "total_pages": total,
            "layout": spec["layouts"][entry],
            "published_at": spec["published_at"],
            "corrupted": broken,
            "checksum": checksum,
            "body": body,
        }

    def snapshot(self) -> dict:
        return {
            "site": self.site,
            "entry_versions": dict(self.entry_versions),
            "paused": time.time() < self.paused_until,
            "paused_until": self.paused_until,
            "session_ttl": self.session_ttl,
            "active_sessions": len(self.sessions),
            "broken_pages": {f"{k[0]}@{k[1]}": v for k, v in self.broken_pages.items()},
        }


class SiteState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.runtimes: dict[str, SiteRuntime] = {
            site: SiteRuntime(site, spec) for site, spec in SITES.items()
        }

    def get(self, site: str) -> SiteRuntime | None:
        return self.runtimes.get(site)

    def snapshot(self) -> dict:
        with self.lock:
            return {site: rt.snapshot() for site, rt in self.runtimes.items()}


STATE = SiteState()


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

    def _split_site(self, path: str) -> tuple[str | None, str]:
        """解析 /<site>/<rest>；/control、/sites 等全局路由保留原路径。"""
        parts = path.lstrip("/").split("/", 1)
        if len(parts) == 2 and parts[0] in STATE.runtimes:
            return parts[0], "/" + parts[1]
        return None, path

    def _paused(self, rt: SiteRuntime) -> bool:
        if time.time() >= rt.paused_until:
            return False
        retry = max(1, int(rt.paused_until - time.time()))
        self._send(503, {"error": "site_paused", "site": rt.site,
                         "message": f"{rt.site} 馆暂停受理，稍后恢复"},
                   {"Retry-After": retry})
        return True

    def _sites_payload(self) -> dict:
        return {"sites": [{"site": s, "name": r.spec["name"]}
                          for s, r in sorted(STATE.runtimes.items())]}

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        site, path = self._split_site(parsed.path)

        if path == "/sites":
            self._send(200, self._sites_payload())
            return

        if path == "/session":
            rt = STATE.get(site) if site else None
            if rt is None:
                self._send(404, {"error": "unknown_site"})
                return
            if self._paused(rt):
                return
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
            sid = uuid.uuid4().hex
            secret = secrets.token_hex(16)
            with STATE.lock:
                rt.sessions[sid] = {
                    "secret": secret,
                    "cursor": resume,
                    "expires_at": time.time() + rt.session_ttl,
                }
            self._send(200, {"site": rt.site, "sid": sid, "secret": secret,
                             "expires_in": rt.session_ttl,
                             "first_token": issue_token(sid, resume)})
            return

        if path == "/control":
            if self.headers.get("X-Control-Key") != CONTROL_KEY:
                self._send(403, {"error": "bad_control_key"})
                return
            length = int(self.headers.get("Content-Length", 0))
            cmd = json.loads(self.rfile.read(length).decode() or "{}")
            # 指令可带 "site": "jia" 只作用于该馆；不带则作用于全部馆。
            targets = ([STATE.get(cmd["site"])] if cmd.get("site") in STATE.runtimes
                       else list(STATE.runtimes.values()))
            with STATE.lock:
                for rt in targets:
                    for entry in ENTRIES:
                        if entry in cmd.get("entry_versions", {}):
                            rt.entry_versions[entry] = cmd["entry_versions"][entry]
                    if "pause_seconds" in cmd:
                        rt.paused_until = max(rt.paused_until,
                                              time.time() + float(cmd["pause_seconds"]))
                    if cmd.get("resume"):
                        rt.paused_until = time.time()
                    if "session_ttl" in cmd:
                        rt.session_ttl = float(cmd["session_ttl"])
                    if cmd.get("expire_sessions"):
                        now = time.time()
                        for sess in rt.sessions.values():
                            sess["expires_at"] = now
                    if "broken_pages" in cmd:
                        rt.broken_pages = {}
                        for name, indexes in cmd["broken_pages"].items():
                            entry, version = name.split("@", 1)
                            rt.broken_pages[(entry, version)] = list(indexes)
                    if cmd.get("clear_broken"):
                        rt.broken_pages.clear()
            self._send(200, {"ok": True, "state": STATE.snapshot()})
            return

        self._send(404, {"error": "not_found"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        site, path = self._split_site(parsed.path)
        if path == "/sites":
            self._send(200, self._sites_payload())
            return
        if path not in ("/registry", "/joins") or site is None:
            self._send(404, {"error": "not_found"})
            return
        rt = STATE.get(site)
        assert rt is not None
        if self._paused(rt):
            return

        sid = self.headers.get("X-Ceramic-Session", "")
        token = self.headers.get("X-Ceramic-Token", "")
        signature = self.headers.get("X-Ceramic-Signature", "")
        with STATE.lock:
            sess = rt.sessions.get(sid)
            if sess is None:
                self._send(401, {"error": "unknown_session",
                                 "message": "会话不存在，请重新登录"})
                return
            if time.time() >= sess["expires_at"]:
                rt.sessions.pop(sid, None)
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
            cursor = claim.get("cursor")
            if not isinstance(cursor, int) or cursor != sess["cursor"]:
                self._send(409, {"error": "token_out_of_order",
                                 "message": "令牌必须按站点发放顺序使用",
                                 "expected_cursor": sess["cursor"]})
                return
            sess["cursor"] += 1
            next_token = issue_token(sid, sess["cursor"])
            entry = path.lstrip("/")
            version = rt.entry_versions[entry]

        payload = rt.page_payload(entry, version, cursor)
        payload["next_token"] = next_token
        self._send(200, payload)


def serve(host: str, port: int) -> ThreadingHTTPServer:
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


def main() -> None:
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description="多合作馆陶片清单本地模拟站")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    httpd = serve(args.host, args.port)
    print(f"模拟站已启动: http://{args.host}:{args.port}")
    print("合作馆: " + ", ".join(
        f"{s}({r.spec['name']})" for s, r in sorted(STATE.runtimes.items())))
    print(f"控制密钥: {CONTROL_KEY}（仅本地验证使用）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
