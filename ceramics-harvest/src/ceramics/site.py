"""本地模拟站：合作馆陶片登记/拼接清单发布服务。

行为覆盖需求中的全部站点故障与协议特性：
- 两个入口 /registry 和 /joins 可在*不同时间*切到不同发布版（控制接口单独设置）。
- 会话有 TTL，过期后返回 401；站点可暂停受理，暂停期返回 503 + Retry-After 恢复时间。
- 翻页令牌按会话访问顺序发放，令牌绑定会话与游标；新会话无法使用旧令牌。
- 请求签名随会话 secret 和令牌变化。
- 页面排版按发布版变化（pipe/jsonl/record 三种），个别页可声明为损坏页。
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
from fixtures import VERSIONS

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


class SiteState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # 两个入口各自的当前发布版，初始都在 v1
        self.entry_versions = {"registry": "v1", "joins": "v1"}
        # sid -> {"secret", "cursor", "expires_at"}
        self.sessions: dict[str, dict] = {}
        self.paused_until = 0.0
        self.session_ttl = 20.0
        # {(entry, version): [损坏页序号...]}，验证脚本可现场制造损坏
        self.broken_pages: dict[tuple[str, str], list[int]] = {}

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "entry_versions": dict(self.entry_versions),
                "paused": time.time() < self.paused_until,
                "paused_until": self.paused_until,
                "session_ttl": self.session_ttl,
                "active_sessions": len(self.sessions),
                "broken_pages": {f"{k[0]}@{k[1]}": v for k, v in self.broken_pages.items()},
            }


STATE = SiteState()


def _page_payload(entry: str, version: str, index: int) -> dict:
    spec = VERSIONS[version]
    size = spec["page_size"][entry]
    rows = spec[entry][index * size:(index + 1) * size]
    total = (len(spec[entry]) + size - 1) // size
    broken = index in STATE.broken_pages.get((entry, version), [])
    if broken:
        # 损坏页：内容被截断、校验和按完好内容计算，必然对不上
        body = _format_page(rows, spec["layouts"][entry])
        body = body[: max(10, len(body) // 2)] + "\n### PAGE CORRUPTED ###"
        checksum = page_checksum(rows)  # 完好内容的摘要，与实际 body 不符
    else:
        body = _format_page(rows, spec["layouts"][entry])
        checksum = page_checksum(rows)
    return {
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


class Handler(BaseHTTPRequestHandler):
    server_version = "CeramicMock/1.0"

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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/session":
            if self._paused():
                return
            length = int(self.headers.get("Content-Length", 0))
            params = parse_qs(self.rfile.read(length).decode() or "")
            if params.get("key", [""])[0] != CONTROL_KEY:
                self._send(403, {"error": "bad_key"})
                return
            sid = uuid.uuid4().hex
            secret = secrets.token_hex(16)
            # 续采游标：采集端报告该入口已确认保存的页数，站点从该游标起
            # 为*新会话*签发全新令牌链（不复用旧令牌，符合"新会话不能用旧令牌"）
            try:
                resume = int(params.get("resume", ["0"])[0])
                if resume < 0:
                    raise ValueError
            except ValueError:
                self._send(400, {"error": "bad_resume"})
                return
            with STATE.lock:
                STATE.sessions[sid] = {
                    "secret": secret,
                    "cursor": resume,
                    "expires_at": time.time() + STATE.session_ttl,
                }
            self._send(200, {"sid": sid, "secret": secret,
                             "expires_in": STATE.session_ttl,
                             "first_token": issue_token(sid, resume)})
            return

        if parsed.path == "/control":
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
                if "broken_pages" in cmd:
                    for name, indexes in cmd["broken_pages"].items():
                        entry, version = name.split("@", 1)
                        STATE.broken_pages[(entry, version)] = list(indexes)
                if "clear_broken" in cmd and cmd["clear_broken"]:
                    STATE.broken_pages.clear()
            self._send(200, {"ok": True, "state": STATE.snapshot()})
            return

        self._send(404, {"error": "not_found"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/registry", "/joins"):
            self._send(404, {"error": "not_found"})
            return
        if self._paused():
            return

        sid = self.headers.get("X-Ceramic-Session", "")
        token = self.headers.get("X-Ceramic-Token", "")
        signature = self.headers.get("X-Ceramic-Signature", "")
        with STATE.lock:
            sess = STATE.sessions.get(sid)
            if sess is None:
                self._send(401, {"error": "unknown_session",
                                 "message": "会话不存在，请重新登录"})
                return
            if time.time() >= sess["expires_at"]:
                STATE.sessions.pop(sid, None)
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
            # 校验通过，按访问顺序为下一页发放新令牌
            sess["cursor"] += 1
            next_token = issue_token(sid, sess["cursor"])

        entry = parsed.path.lstrip("/")
        with STATE.lock:
            version = STATE.entry_versions[entry]
        payload = _page_payload(entry, version, cursor)
        payload["next_token"] = next_token
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
