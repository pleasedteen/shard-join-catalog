"""模拟站与采集端共用的会话/令牌/签名协议。

- 会话：POST /session 获得 sid + secret，secret 只在登录响应里出现一次。
- 令牌：模拟站按*访问顺序*为每个会话发放翻页令牌；令牌里编码了会话与游标，
  新会话不能使用旧会话的令牌（站点会重新签发，采集端也不缓存旧令牌）。
- 签名：HMAC-SHA256(secret, f"{method} {path}\nX-Ceramic-Session: {sid}\nX-Ceramic-Token: {token}")，
  因此会话（secret）和令牌变化时签名都不同。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, method: str, path: str, sid: str, token: str) -> str:
    message = f"{method.upper()} {path}\nX-Ceramic-Session: {sid}\nX-Ceramic-Token: {token}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def issue_token(sid: str, cursor: int) -> str:
    payload = json.dumps({"sid": sid, "cursor": cursor}, separators=(",", ":")).encode()
    return b64e(hashlib.sha256(sid.encode()).digest()[:8] + payload)


def read_token(token: str) -> dict:
    raw = b64d(token)
    return json.loads(raw[8:])


def page_checksum(records: list[dict]) -> str:
    """页内容完整性摘要：页面被篡改/损坏时采集端可直接发现。"""
    blob = json.dumps(records, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def body_digest(body: str) -> str:
    """页面原始正文的摘要，与解析结果无关。

    站点清单与采集端本地缓存都用它比较"这一页的字节有没有变"，
    因此损坏页（正文被截断）与馆方更正正文都能被识别。
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def manifest_digest(pages: list[dict]) -> str:
    """入口清单摘要：按页序号汇总每页正文摘要与损坏标记。

    资料未变时站点返回同一摘要，采集端只需一次清单请求即可判定无需重抓，
    核验流量与页数无关；摘要变化时再取逐页摘要定位需要补抓的页面。
    """
    normalized = [
        {"page_index": p["page_index"],
         "body_digest": p["body_digest"],
         "corrupted": bool(p["corrupted"])}
        for p in sorted(pages, key=lambda p: p["page_index"])
    ]
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def edge_key(a: str, b: str) -> str:
    """无向拼接边的规范键，避免 (A,B)/(B,A) 重复。"""
    return "~".join(sorted((a, b)))
