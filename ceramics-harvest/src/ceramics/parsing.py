"""三种页面排版的解析器，把模拟站页面正文还原为记录列表。

整版记录字段固定；更正单记录首列为 op（add / upsert / delete），
delete 只携带定位键（登记 catalog_no；拼接 a/b），其余字段可为空。
"""
from __future__ import annotations

import json

REGISTRY_FIELDS = ("catalog_no", "museum", "material", "weight_g")
JOIN_FIELDS = ("a", "b", "bond", "confidence")
CORRECTION_OPS = ("add", "upsert", "delete")

# 更正单各入口的字段顺序（排版按此顺序序列化）
REGISTRY_CORRECTION_FIELDS = ("op", "catalog_no", "museum", "material", "weight_g")
JOIN_CORRECTION_FIELDS = ("op", "a", "b", "bond", "confidence")


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


def _coerce_correction(row: dict, entry: str) -> dict:
    if "op" not in row:
        raise ValueError(f"{entry} 更正记录缺少 op 字段")
    op = str(row["op"]).strip()
    if op not in CORRECTION_OPS:
        raise ValueError(f"{entry} 更正记录 op 非法: {op!r}")
    out = {"op": op}
    if entry == "registry":
        if not row.get("catalog_no"):
            raise ValueError("登记更正记录缺少 catalog_no")
        out["catalog_no"] = str(row["catalog_no"])
        if op == "delete":
            return out
        for f in ("museum", "material", "weight_g"):
            if f not in row or row[f] in (None, ""):
                raise ValueError(f"登记 {op} 更正记录缺少字段: {f}")
        try:
            out["weight_g"] = float(row["weight_g"])
        except (TypeError, ValueError):
            raise ValueError(f"登记更正 weight_g 不是数字: {row.get('weight_g')!r}")
        out["museum"] = str(row["museum"])
        out["material"] = str(row["material"])
        return out
    # joins
    if not row.get("a") or not row.get("b"):
        raise ValueError("拼接更正记录缺少 a/b 定位键")
    out["a"], out["b"] = str(row["a"]), str(row["b"])
    if op == "delete":
        return out
    for f in ("bond", "confidence"):
        if f not in row or row[f] in (None, ""):
            raise ValueError(f"拼接 {op} 更正记录缺少字段: {f}")
    out["bond"] = str(row["bond"])
    out["confidence"] = str(row["confidence"])
    return out


def parse_page(entry: str, layout: str, body: str, kind: str = "full") -> list[dict]:
    if kind == "correction":
        return _parse_correction(entry, layout, body)
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


def _parse_correction(entry: str, layout: str, body: str) -> list[dict]:
    """解析更正单页面：每行/块是一条更正操作。"""
    if layout == "pipe":
        fields = (REGISTRY_CORRECTION_FIELDS if entry == "registry"
                  else JOIN_CORRECTION_FIELDS)
        rows = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != len(fields):
                raise ValueError(f"更正单 pipe 页面列数不符：期望 {len(fields)}，实际 {len(parts)}")
            rows.append(_coerce_correction(dict(zip(fields, parts)), entry))
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
                raise ValueError(f"更正单 jsonl 页面无法解析: {exc}")
            rows.append(_coerce_correction(raw, entry))
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
                    raise ValueError(f"更正单 record 页面行格式错误: {line!r}")
                k, v = line.split(":", 1)
                raw[k.strip()] = v.strip()
            rows.append(_coerce_correction(raw, entry))
        return rows
    raise ValueError(f"未知排版: {layout}")
