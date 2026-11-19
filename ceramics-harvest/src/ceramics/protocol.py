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


def edge_key(a: str, b: str) -> str:
    """无向拼接边的规范键，避免 (A,B)/(B,A) 重复。"""
    return "~".join(sorted((a, b)))
