"""查询服务：按各馆当前 active 发布版返回陶片及拼接组，含跨馆拼接。

- 各馆独立生效：一个馆暂停/失败不影响其它馆，查询结果里每个陶片都标明来自哪个馆。
- 馆藏编号按馆唯一；不带 --site 的查询会返回所有馆中该编号的条目，并按跨馆拼接
  关系给出跨馆连通组。
- 有效拼接 = 各馆 active 版本 joins 去掉该版撤销记录，再叠加"两端均已发布"的
  跨馆登记（见 cross.py）；跨馆另一端撤下或撤下登记时立即退出结果。
- 查询结果带各馆发布版，明确数据来自哪一版。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ceramics import cross
from ceramics.db import active_versions, all_sites, connect, init_db
from ceramics.protocol import edge_key, node_key


def _sherd(conn: sqlite3.Connection, site: str, version: str, catalog_no: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sherds WHERE site=? AND version=? AND catalog_no=?",
        (site, version, catalog_no)).fetchone()
    return dict(row) if row else None


def _describe_node(conn: sqlite3.Connection, key: str, versions: dict[str, str]) -> dict:
    site, catalog_no = cross.split_node(key)
    row = _sherd(conn, site, versions.get(site, ""), catalog_no)
    out = {"site": site, "catalog_no": catalog_no, "node": key,
           "version": versions.get(site)}
    if row:
        out.update({"museum": row["museum"], "material": row["material"],
                    "weight_g": row["weight_g"]})
    return out


def _group_payload(graph: dict, conn: sqlite3.Connection, start: str) -> dict:
    versions = graph["versions"]
    nodes, edge_keys = cross.component(graph["adj"], start)
    members = [_describe_node(conn, n, versions) for n in sorted(nodes)]
    sites_in_group = sorted({m["site"] for m in members})
    joins = []
    for key in edge_keys:
        e = graph["edges"][key]
        item = {"edge_key": key, "kind": e["kind"], "bond": e["bond"],
                "confidence": e["confidence"]}
        if e["kind"] == "cross":
            item.update({"site_a": e["site_a"], "a": e["a"],
                         "site_b": e["site_b"], "b": e["b"]})
        else:
            item.update({"site": e["site"], "a": e["a"], "b": e["b"]})
        joins.append(item)
    return {
        "group_size": len(members),
        "group": members,
        "sites_in_group": sites_in_group,
        "cross_site": len(sites_in_group) > 1,
        "effective_joins": sorted(joins, key=lambda j: j["edge_key"]),
        "versions": {s: versions[s] for s in sites_in_group},
    }


def find_sherd(conn: sqlite3.Connection, catalog_no: str,
               site: str | None = None) -> dict:
    versions = active_versions(conn)
    if not versions:
        raise LookupError("尚无完整可用的发布版，查询未开放")
    if site is not None and site not in versions:
        if site not in all_sites(conn):
            raise LookupError(f"未知合作馆 {site}")
        raise LookupError(f"合作馆 {site} 尚无完整可用的发布版")

    candidates = [site] if site else sorted(versions)
    targets = []
    for s in candidates:
        row = _sherd(conn, s, versions[s], catalog_no)
        if row:
            targets.append({"site": s, "sherd": dict(row)})
    if not targets:
        where = f"合作馆 {site} 的" if site else "任一合作馆的"
        raise LookupError(f"馆藏编号 {catalog_no} 不在{where}当前发布版中")

    graph = cross.build_effective_graph(conn, versions)
    # 每个匹配条目各自的连通组；不同馆重号陶片可能落在不同组
    per_hit = []
    seen_groups: dict[str, dict] = {}
    for hit in targets:
        key = node_key(hit["site"], catalog_no)
        payload = _group_payload(graph, conn, key)
        group_id = "|".join(m["node"] for m in payload["group"])
        payload["group_id"] = group_id
        seen_groups.setdefault(group_id, payload)
        per_hit.append({"site": hit["site"], "sherd": hit["sherd"],
                        "group_id": group_id})

    primary = per_hit[0]
    main_group = seen_groups[primary["group_id"]]
    # versions 是全部馆的当前发布版；group_versions 只列该连通组实际涉及的馆，
    # 避免把与查询无关的馆混进来。
    group_versions = {s: versions[s] for s in main_group["sites_in_group"]}
    return {
        "query": catalog_no,
        "query_site": site,
        "matches": per_hit,
        "version": versions[primary["site"]],
        "versions": dict(versions),
        "group_versions": group_versions,
        "sherd": primary["sherd"],
        "group_id": primary["group_id"],
        "group_size": main_group["group_size"],
        "group": main_group["group"],
        "sites_in_group": main_group["sites_in_group"],
        "cross_site": main_group["cross_site"],
        "effective_joins": main_group["effective_joins"],
        "groups": list(seen_groups.values()) if len(seen_groups) > 1 else [],
    }


def revoke_join(conn: sqlite3.Connection, a: str, b: str, reason: str = "",
                site: str | None = None) -> dict:
    versions = active_versions(conn)
    if not versions:
        raise LookupError("尚无完整可用的发布版")
    if site is None:
        row = conn.execute("SELECT COUNT(*) c FROM versions WHERE status='active'").fetchone()
        if row["c"] != 1:
            raise LookupError("存在多个合作馆，撤销馆内拼接必须指定 --site")
        site = next(iter(versions))
    version = versions.get(site)
    if not version:
        raise LookupError(f"合作馆 {site} 尚无完整可用的发布版")
    key = edge_key(a, b)
    row = conn.execute("SELECT * FROM joins WHERE site=? AND version=? AND edge_key=?",
                       (site, version, key)).fetchone()
    if not row:
        raise LookupError(f"合作馆 {site} 当前发布版 {version} 中不存在拼接 {a}~{b}")
    conn.execute(
        "INSERT INTO revocations(site, version, edge_key, reason, revoked_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(site, version, edge_key) "
        "DO UPDATE SET reason=excluded.reason",
        (site, version, key, reason, time.time()))
    conn.commit()
    return {"revoked": key, "site": site, "version": version,
            "note": "已复核撤销；后续查询按最新清单返回，连通组可能拆分"}


def restore_join(conn: sqlite3.Connection, a: str, b: str,
                 site: str | None = None) -> dict:
    versions = active_versions(conn)
    if not versions:
        raise LookupError("尚无完整可用的发布版")
    if site is None:
        if len(versions) != 1:
            raise LookupError("存在多个合作馆，恢复馆内拼接必须指定 --site")
        site = next(iter(versions))
    key = edge_key(a, b)
    cur = conn.execute(
        "DELETE FROM revocations WHERE site=? AND edge_key=?", (site, key))
    conn.commit()
    if cur.rowcount == 0:
        raise LookupError(f"合作馆 {site} 的撤销记录中没有 {a}~{b}")
    return {"restored": key, "site": site}


def _cross_identity(conn: sqlite3.Connection, versions: dict[str, str],
                    site_a: str, a: str, site_b: str, b: str) -> str:
    """校验两侧确已登记同一条跨馆拼接，返回其规范边键。"""
    from ceramics.parsing import split_endpoint

    def _side_registers(site: str, local: str, other_site: str, other: str) -> bool:
        version = versions.get(site)
        if not version:
            return False
        for row in conn.execute("SELECT a, b FROM joins WHERE site=? AND version=?",
                                (site, version)):
            sa, ca = split_endpoint(row["a"], site)
            sb, cb = split_endpoint(row["b"], site)
            if (sa == site and ca == local) or (sb == site and cb == local):
                other_side = (sb, cb) if (sa == site and ca == local) else (sa, ca)
                if other_side == (other_site, other):
                    return True
        return False

    if not _side_registers(site_a, a, site_b, b):
        raise LookupError(f"合作馆 {site_a} 当前发布版未登记跨馆拼接 {a}~{site_b}:{b}")
    if not _side_registers(site_b, b, site_a, a):
        raise LookupError(f"合作馆 {site_b} 当前发布版未登记跨馆拼接 {b}~{site_a}:{a}")
    return cross.edge_identity(node_key(site_a, a), node_key(site_b, b))


def revoke_cross(conn: sqlite3.Connection, site: str, a: str,
                 site_b: str, b: str, reason: str = "") -> dict:
    versions = active_versions(conn)
    ident = _cross_identity(conn, versions, site, a, site_b, b)
    conn.execute(
        "INSERT INTO cross_revocations(edge_key, side, reason, revoked_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(edge_key, side) DO UPDATE SET reason=excluded.reason",
        (ident, site, reason, time.time()))
    conn.commit()
    return {"revoked": ident, "site": site, "version": versions.get(site),
            "note": "跨馆拼接任一端撤下即全局退出结果；恢复需显式 restore-cross"}


def restore_cross(conn: sqlite3.Connection, site: str, a: str,
                  site_b: str, b: str) -> dict:
    versions = active_versions(conn)
    ident = _cross_identity(conn, versions, site, a, site_b, b)
    cur = conn.execute("DELETE FROM cross_revocations WHERE edge_key=? AND side=?",
                       (ident, site))
    conn.commit()
    if cur.rowcount == 0:
        raise LookupError(f"跨馆拼接 {ident} 在 {site} 端没有撤下记录")
    return {"restored": ident, "site": site}


def list_groups(conn: sqlite3.Connection, site: str | None = None) -> dict:
    versions = active_versions(conn)
    if not versions:
        raise LookupError("尚无完整可用的发布版，查询未开放")
    if site is not None:
        if site not in versions:
            raise LookupError(f"合作馆 {site} 尚无完整可用的发布版")
        versions = {site: versions[site]}
    graph = cross.build_effective_graph(conn, versions)
    nodes = []
    for s, v in versions.items():
        nodes.extend(node_key(s, r["catalog_no"]) for r in conn.execute(
            "SELECT catalog_no FROM sherds WHERE site=? AND version=? ORDER BY catalog_no",
            (s, v)))
    groups, seen = [], set()
    for start in sorted(nodes):
        if start in seen:
            continue
        members, _ = cross.component(graph["adj"], start)
        seen.update(members)
        groups.append(_group_payload(graph, conn, start))
    groups.sort(key=lambda g: g["group"][0]["node"])
    return {"versions": dict(versions), "group_count": len(groups),
            "cross_group_count": sum(1 for g in groups if g["cross_site"]),
            "groups": groups}


def status(conn: sqlite3.Connection) -> dict:
    versions = active_versions(conn)
    revisions = [dict(r) for r in conn.execute(
        "SELECT site, version, edge_key, reason, revoked_at FROM revocations ORDER BY id")]
    cross_revs = [dict(r) for r in conn.execute(
        "SELECT edge_key, side, reason, revoked_at FROM cross_revocations ORDER BY id")]
    history = [dict(r) for r in conn.execute(
        "SELECT site, version, activated_at FROM activation_log ORDER BY id")]
    return {"active_versions": versions, "revisions": revisions,
            "cross_revisions": cross_revs, "history": history}


def _print_find(result: dict) -> None:
    print(f"查询编号: {result['query']}"
          + (f"  （限定合作馆 {result['query_site']}）" if result["query_site"] else ""))
    print("各馆当前发布版: " + ", ".join(
        f"{s}={v}" for s, v in sorted(result["versions"].items())))
    print(f"命中 {len(result['matches'])} 条: " + ", ".join(
        f"{m['site']}/{m['sherd']['catalog_no']}({m['sherd']['museum']})"
        for m in result["matches"]))
    print(f"\n连通组 {result['group_id']}（{result['group_size']} 片"
          + ("，跨馆拼接" if result["cross_site"] else "") + "）:")
    for m in result["group"]:
        print(f"  - [{m['site']}] {m['catalog_no']}  {m.get('material', '')}  "
              f"{m.get('weight_g', '')}g  版 {m.get('version')}")
    print(f"组内有效拼接 {len(result['effective_joins'])} 条:")
    for e in result["effective_joins"]:
        if e["kind"] == "cross":
            print(f"  - 跨馆 {e['site_a']}:{e['a']} ~ {e['site_b']}:{e['b']}  "
                  f"[{e['bond']}, {e['confidence']}]")
        else:
            print(f"  - 馆内 {e['site']}: {e['a']} ~ {e['b']}  [{e['bond']}, {e['confidence']}]")
    if result["groups"]:
        print("\n注：该编号在多个馆命中且落在不同连通组，另列如下:")
        for g in result["groups"]:
            print(f"  - {g['group_id']}（{g['group_size']} 片）")


def make_http_handler(db_path: str):
    class QueryHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            q = parse_qs(parsed.query)
            init_db(db_path)
            conn = connect(db_path)
            try:
                if parsed.path == "/find":
                    cat = q.get("catalog_no", [""])[0]
                    site = q.get("site", [None])[0]
                    self._send(200, find_sherd(conn, cat, site))
                elif parsed.path == "/groups":
                    site = q.get("site", [None])[0]
                    self._send(200, list_groups(conn, site))
                elif parsed.path == "/status":
                    self._send(200, status(conn))
                else:
                    self._send(404, {"error": "not_found",
                                     "routes": ["/find?catalog_no=&site=", "/groups",
                                                "/status", "/revoke", "/restore"]})
            except LookupError as exc:
                self._send(404, {"error": str(exc)})
            finally:
                conn.close()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in ("/revoke", "/restore", "/revoke-cross", "/restore-cross"):
                self._send(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode() or "{}")
            init_db(db_path)
            conn = connect(db_path)
            try:
                if parsed.path == "/revoke":
                    self._send(200, revoke_join(conn, body["a"], body["b"],
                                                body.get("reason", ""), body.get("site")))
                elif parsed.path == "/restore":
                    self._send(200, restore_join(conn, body["a"], body["b"],
                                                 body.get("site")))
                elif parsed.path == "/revoke-cross":
                    self._send(200, revoke_cross(conn, body["site"], body["a"],
                                                 body["site_b"], body["b"],
                                                 body.get("reason", "")))
                else:
                    self._send(200, restore_cross(conn, body["site"], body["a"],
                                                  body["site_b"], body["b"]))
            except (LookupError, KeyError) as exc:
                self._send(404, {"error": str(exc)})
            finally:
                conn.close()

    return QueryHandler


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="陶片拼接查询服务（多合作馆）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("find", "revoke", "restore", "revoke-cross", "restore-cross",
                 "groups", "status", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--db", default="./ceramics.db")
        if name == "find":
            p.add_argument("catalog_no")
            p.add_argument("--site", default=None, help="只在该馆查询")
            p.add_argument("--json", action="store_true")
        if name in ("revoke", "restore"):
            p.add_argument("a")
            p.add_argument("b")
            p.add_argument("--site", default=None, help="第二端所在合作馆")
        if name in ("revoke", "revoke-cross"):
            p.add_argument("--reason", default="")
        if name in ("revoke-cross", "restore-cross"):
            p.add_argument("a")
            p.add_argument("site_b")
            p.add_argument("b")
            p.add_argument("--site", dest="site", required=True,
                           help="发起撤下/恢复的一端合作馆")
        if name == "groups":
            p.add_argument("--site", default=None)
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    init_db(args.db)
    conn = connect(args.db)
    try:
        if args.cmd == "find":
            result = find_sherd(conn, args.catalog_no, args.site)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                _print_find(result)
        elif args.cmd == "revoke":
            print(json.dumps(revoke_join(conn, args.a, args.b, args.reason, args.site),
                             ensure_ascii=False))
        elif args.cmd == "restore":
            print(json.dumps(restore_join(conn, args.a, args.b, args.site),
                             ensure_ascii=False))
        elif args.cmd == "revoke-cross":
            print(json.dumps(revoke_cross(conn, args.site, args.a, args.site_b,
                                          args.b, args.reason), ensure_ascii=False))
        elif args.cmd == "restore-cross":
            print(json.dumps(restore_cross(conn, args.site, args.a, args.site_b,
                                           args.b), ensure_ascii=False))
        elif args.cmd == "groups":
            print(json.dumps(list_groups(conn, args.site), ensure_ascii=False, indent=2))
        elif args.cmd == "status":
            print(json.dumps(status(conn), ensure_ascii=False, indent=2))
        elif args.cmd == "serve":
            httpd = ThreadingHTTPServer((args.host, args.port), make_http_handler(args.db))
            print(f"查询服务已启动: http://{args.host}:{args.port}")
            print("路由: GET /find?catalog_no=&site=  GET /groups  GET /status  "
                  "POST /revoke /restore /revoke-cross /restore-cross")
            httpd.serve_forever()
    except LookupError as exc:
        print(f"错误: {exc}")
        raise SystemExit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
