"""跨馆拼接的实时配对与有效连通图。

跨馆拼接不做跨馆版本绑定，而是按"两端各自当前已发布清单"实时判定：

- 各馆 joins 里以 "馆名:馆藏编号" 显式指向另一个馆，即为跨馆登记；
- 同一条跨馆边必须两端都登记（各馆在自己的清单里列出对方），才算"两端登记均已发布"；
- 晚到的登记（乙馆新版才补上另一端）下一轮查询即生效，无需重采甲馆；
- 任一端新版不再登记，或该端显式撤下，这条边立刻从查询结果消失；
- 连接图中的节点键是 (馆, 馆藏编号)，因此不同馆的重号陶片不会串组。
"""
from __future__ import annotations

import sqlite3

from ceramics.db import active_versions
from ceramics.parsing import split_endpoint


def node_key(site: str, catalog_no: str) -> str:
    return f"{site}|{catalog_no}"


def split_node(key: str) -> tuple[str, str]:
    site, catalog_no = key.split("|", 1)
    return site, catalog_no


def edge_identity(na: str, nb: str) -> str:
    return "~".join(sorted((na, nb)))


def build_effective_graph(conn: sqlite3.Connection,
                          versions: dict[str, str] | None = None) -> dict:
    """返回 {versions, adj, edges}；adj 的节点键为 "馆|馆藏编号"。"""
    versions = versions if versions is not None else active_versions(conn)
    local: list[dict] = []
    cross_reg: dict[str, dict] = {}
    for site, version in versions.items():
        rows = conn.execute(
            "SELECT a, b, bond, confidence FROM joins WHERE site=? AND version=? "
            "ORDER BY edge_key", (site, version)).fetchall()
        for row in rows:
            sa, ca = split_endpoint(row["a"], site)
            sb, cb = split_endpoint(row["b"], site)
            if sa == site and sb == site:
                if ca == cb:
                    continue
                local.append({"site": site, "a": ca, "b": cb,
                              "bond": row["bond"], "confidence": row["confidence"]})
            elif sa != sb and site in (sa, sb):
                local_cat, other_site, other_cat = (
                    (ca, sb, cb) if sa == site else (cb, sa, ca))
                na, nb = node_key(site, local_cat), node_key(other_site, other_cat)
                ident = edge_identity(na, nb)
                ent = cross_reg.setdefault(ident, {"nodes": (na, nb), "sides": {}})
                ent["sides"][site] = {"bond": row["bond"],
                                      "confidence": row["confidence"]}

    def _sherd_exists(site: str, catalog_no: str) -> bool:
        version = versions.get(site)
        if not version:
            return False
        return conn.execute(
            "SELECT 1 FROM sherds WHERE site=? AND version=? AND catalog_no=?",
            (site, version, catalog_no)).fetchone() is not None

    def _revoked(kind: str, site: str, edge: dict) -> bool:
        if kind == "local":
            version = versions.get(site)
            return conn.execute(
                "SELECT 1 FROM revocations WHERE site=? AND version=? AND edge_key=?",
                (site, version, "~".join(sorted((edge["a"], edge["b"]))))).fetchone() is not None
        side = edge.get("side")
        return conn.execute(
            "SELECT 1 FROM cross_revocations WHERE edge_key=? AND side=?",
            (edge["edge_key"], side)).fetchone() is not None

    edges: dict[str, dict] = {}
    adj: dict[str, list[dict]] = {}

    def _add(key: str, a: str, b: str, info: dict) -> None:
        edges[key] = info
        adj.setdefault(a, []).append({"to": b, "edge_key": key, **info})
        adj.setdefault(b, []).append({"to": a, "edge_key": key, **info})

    for e in local:
        if not (_sherd_exists(e["site"], e["a"]) and _sherd_exists(e["site"], e["b"])):
            continue
        key = edge_identity(node_key(e["site"], e["a"]), node_key(e["site"], e["b"]))
        if _revoked("local", e["site"], e):
            continue
        _add(key, node_key(e["site"], e["a"]), node_key(e["site"], e["b"]),
             {"kind": "local", "site": e["site"], "a": e["a"], "b": e["b"],
              "bond": e["bond"], "confidence": e["confidence"]})

    for ident, ent in cross_reg.items():
        na, nb = ent["nodes"]
        site_a, cat_a = split_node(na)
        site_b, cat_b = split_node(nb)
        sides = ent["sides"]
        # 两端都必须登记；任一端撤下或未登记则不生效
        if site_a not in sides or site_b not in sides:
            continue
        if not (_sherd_exists(site_a, cat_a) and _sherd_exists(site_b, cat_b)):
            continue
        revoked = False
        for side in (site_a, site_b):
            if _revoked("cross", side, {"edge_key": ident, "side": side}):
                revoked = True
                break
        if revoked:
            continue
        info = {"kind": "cross", "edge_key": ident,
                "site_a": site_a, "a": cat_a, "site_b": site_b, "b": cat_b,
                "bond": sides[site_a]["bond"], "confidence": sides[site_a]["confidence"]}
        _add(ident, na, nb, info)

    return {"versions": dict(versions), "adj": adj, "edges": edges}


def component(adj: dict, start: str) -> tuple[list[str], list[str]]:
    """从 start 出发的连通分量与其中生效边的键。"""
    group, visited, queue = [], set(), [start]
    seen = {start}
    while queue:
        node = queue.pop(0)
        group.append(node)
        for link in adj.get(node, []):
            visited.add(link["edge_key"])
            if link["to"] not in seen:
                seen.add(link["to"])
                queue.append(link["to"])
    return group, sorted(visited)
