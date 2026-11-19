"""三种页面排版的解析器，把模拟站页面正文还原为记录列表。"""
from __future__ import annotations

import json

REGISTRY_FIELDS = ("catalog_no", "museum", "material", "weight_g")
JOIN_FIELDS = ("a", "b", "bond", "confidence")


def _coerce(row: dict, fields: tuple[str, ...], entry: str) -> dict:
    missing = [f for f in fields if f not in row]
    if missing:
        raise ValueError(f"{entry} 页面记录缺少字段: {', '.join(missing)}")
    if entry == "registry":
        try:
            row["weight_g"] = float(row["weight_g"])
        except (TypeError, ValueError):
            raise ValueError(f"登记页 weight_g 不是数字: {row.get('weight_g')!r}")
    return {f: row[f] for f in fields}


def parse_page(entry: str, layout: str, body: str) -> list[dict]:
    fields = REGISTRY_FIELDS if entry == "registry" else JOIN_FIELDS
    if layout == "pipe":
        rows = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != len(fields):
                raise ValueError(f"pipe 页面列数不符：期望 {len(fields)}，实际 {len(parts)}")
            rows.append(_coerce(dict(zip(fields, parts)), fields, entry))
        return rows
    if layout == "jsonl":
        rows = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"jsonl 页面无法解析: {exc}")
            rows.append(_coerce(raw, fields, entry))
        return rows
    if layout == "record":
        rows = []
        for block in body.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            raw = {}
            for line in block.splitlines():
                if ":" not in line:
                    raise ValueError(f"record 页面行格式错误: {line!r}")
                k, v = line.split(":", 1)
                raw[k.strip()] = v.strip()
            rows.append(_coerce(raw, fields, entry))
        return rows
    raise ValueError(f"未知排版: {layout}")
