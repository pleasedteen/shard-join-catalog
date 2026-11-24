"""查询服务：按当前 active 发布版返回陶片及其拼接连通组，支持撤销/恢复拼接。

- 有效拼接 = 该发布版 joins 去掉本发布版 revocations；撤销后原一组自动拆成多组，
  由图遍历实时计算（自动成环也安全）。
- 查询结果带发布版编号，明确数据来自哪一版。
- 没有任何 active 版本时明确报错，不猜测数据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ceramics.db import connect, get_active_version, init_db

EFFECTIVE_EDGES_SQL = """
SELECT j.edge_key, j.a, j.b, j.bond, j.confidence
FROM joins j
WHERE j.version = ?
  AND NOT EXISTS (
      SELECT 1 FROM revocations r
      WHERE r.version = j.version AND r.edge_key = j.edge_key)
"""


def _load_graph(conn: sqlite3.Connection, version: str) -> tuple[dict, dict]:
    adj: dict[str, list[dict]] = {}
    edges: dict[str, dict] = {}
    for row in conn.execute(EFFECTIVE_EDGES_SQL, (version,)):
        edges[row["edge_key"]] = dict(row)
        adj.setdefault(row["a"], []).append({"to": row["b"], "edge_key": row["edge_key"]})
        adj.setdefault(row["b"], []).append({"to": row["a"], "edge_key": row["edge_key"]})
    return adj, edges


def find_sherd(conn: sqlite3.Connection, catalog_no: str) -> dict:
    version = get_active_version(conn)
    if not version:
        raise LookupError("尚无完整可用的发布版，查询未开放")
    sherd = conn.execute("SELECT * FROM sherds WHERE version=? AND catalog_no=?",
                         (version, catalog_no)).fetchone()
    if not sherd:
        raise LookupError(f"馆藏编号 {catalog_no} 不在当前发布版 {version} 中")
    adj, edges = _load_graph(conn, version)
    group, visited_edges = [], set()
    queue = deque([catalog_no])
    seen = {catalog_no}
    while queue:
        node = queue.popleft()
        group.append(node)
        for link in adj.get(node, []):
            visited_edges.add(link["edge_key"])
            if link["to"] not in seen:
                seen.add(link["to"])
                queue.append(link["to"])
    members = []
    for no in sorted(group):
        row = conn.execute("SELECT * FROM sherds WHERE version=? AND catalog_no=?",
                           (version, no)).fetchone()
        members.append(dict(row) if row else {"catalog_no": no})
    return {
        "query": catalog_no,
        "version": version,
        "published_at": conn.execute(
            "SELECT published_at FROM versions WHERE version=?", (version,)).fetchone()[0],
        "sherd": dict(sherd),
        "group_size": len(members),
        "group": members,
        "effective_joins": [edges[k] for k in sorted(visited_edges)],
    }


def revoke_join(conn: sqlite3.Connection, a: str, b: str, reason: str = "") -> dict:
    version = get_active_version(conn)
    if not version:
        raise LookupError("尚无完整可用的发布版")
    from ceramics.protocol import edge_key
    key = edge_key(a, b)
    row = conn.execute("SELECT * FROM joins WHERE version=? AND edge_key=?",
                       (version, key)).fetchone()
    if not row:
        raise LookupError(f"当前发布版 {version} 中不存在拼接 {a}~{b}")
    conn.execute(
        "INSERT INTO revocations(version, edge_key, reason, revoked_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(version, edge_key) DO UPDATE SET reason=excluded.reason",
        (version, key, reason, time.time()))
    conn.commit()
    return {"revoked": key, "version": version,
            "note": "已复核撤销；后续查询按最新清单返回，连通组可能拆分"}


def restore_join(conn: sqlite3.Connection, a: str, b: str) -> dict:
    version = get_active_version(conn)
    if not version:
        raise LookupError("尚无完整可用的发布版")
    from ceramics.protocol import edge_key
    key = edge_key(a, b)
    cur = conn.execute("DELETE FROM revocations WHERE version=? AND edge_key=?",
                       (version, key))
    conn.commit()
    if cur.rowcount == 0:
        raise LookupError(f"撤销记录中没有 {a}~{b}")
    return {"restored": key, "version": version}


def list_groups(conn: sqlite3.Connection) -> dict:
    version = get_active_version(conn)
    if not version:
        raise LookupError("尚无完整可用的发布版，查询未开放")
    adj, _ = _load_graph(conn, version)
    nodes = [r["catalog_no"] for r in conn.execute(
        "SELECT catalog_no FROM sherds WHERE version=? ORDER BY catalog_no", (version,))]
    groups, seen = [], set()
    for start in nodes:
        if start in seen:
            continue
        queue, component = deque([start]), []
        seen.add(start)
        while queue:
            node = queue.popleft()
            component.append(node)
            for link in adj.get(node, []):
                if link["to"] not in seen:
                    seen.add(link["to"])
                    queue.append(link["to"])
        groups.append(sorted(component))
    groups.sort(key=lambda g: g[0])
    return {"version": version, "group_count": len(groups), "groups": groups}


def status(conn: sqlite3.Connection) -> dict:
    version = get_active_version(conn)
    out = {"active_version": version, "revisions": [], "history": [],
           "page_issues": []}
    # 坏页位置：按发布版/入口/页序号标明，即使该版未激活也可见
    out["page_issues"] = [dict(r) for r in conn.execute(
        "SELECT version, entry, page_index, kind, detail, detected_at "
        "FROM page_issues ORDER BY id")]
    if version:
        out["revisions"] = [dict(r) for r in conn.execute(
            "SELECT edge_key, reason, revoked_at FROM revocations WHERE version=? ORDER BY id",
            (version,))]
        out["history"] = [dict(r) for r in conn.execute(
            "SELECT version, activated_at FROM activation_log ORDER BY id")]
    return out


def _print_find(result: dict) -> None:
    print(f"查询编号: {result['query']}  |  数据发布版: {result['version']} "
          f"({result['published_at']})")
    s = result["sherd"]
    print(f"陶片: {s['catalog_no']}  馆藏: {s['museum']}  材质: {s['material']}  "
          f"重量: {s['weight_g']}g")
    print(f"当前有效拼接连通组（{result['group_size']} 片）:")
    for m in result["group"]:
        mark = " <= 查询目标" if m["catalog_no"] == result["query"] else ""
        print(f"  - {m['catalog_no']}  {m.get('material', '')}  {m.get('weight_g', '')}g{mark}")
    print(f"组内有效拼接 {len(result['effective_joins'])} 条:")
    for e in result["effective_joins"]:
        print(f"  - {e['a']} ~ {e['b']}  [{e['bond']}, {e['confidence']}]")


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
                    self._send(200, find_sherd(conn, cat))
                elif parsed.path == "/groups":
                    self._send(200, list_groups(conn))
                elif parsed.path == "/status":
                    self._send(200, status(conn))
                else:
                    self._send(404, {"error": "not_found",
                                     "routes": ["/find?catalog_no=", "/groups", "/status"]})
            except LookupError as exc:
                self._send(404, {"error": str(exc)})
            finally:
                conn.close()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path not in ("/revoke", "/restore"):
                self._send(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode() or "{}")
            init_db(db_path)
            conn = connect(db_path)
            try:
                if parsed.path == "/revoke":
                    self._send(200, revoke_join(conn, body["a"], body["b"],
                                                body.get("reason", "")))
                else:
                    self._send(200, restore_join(conn, body["a"], body["b"]))
            except LookupError as exc:
                self._send(404, {"error": str(exc)})
            finally:
                conn.close()

    return QueryHandler


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="陶片拼接查询服务")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("find", "revoke", "restore", "groups", "status", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--db", default="./ceramics.db")
        if name == "find":
            p.add_argument("catalog_no")
            p.add_argument("--json", action="store_true")
        if name in ("revoke", "restore"):
            p.add_argument("a")
            p.add_argument("b")
        if name == "revoke":
            p.add_argument("--reason", default="")
        if name == "serve":
            p.add_argument("--host", default="127.0.0.1")
            p.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()

    init_db(args.db)
    conn = connect(args.db)
    try:
        if args.cmd == "find":
            result = find_sherd(conn, args.catalog_no)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                _print_find(result)
        elif args.cmd == "revoke":
            print(json.dumps(revoke_join(conn, args.a, args.b, args.reason),
                             ensure_ascii=False))
        elif args.cmd == "restore":
            print(json.dumps(restore_join(conn, args.a, args.b), ensure_ascii=False))
        elif args.cmd == "groups":
            print(json.dumps(list_groups(conn), ensure_ascii=False, indent=2))
        elif args.cmd == "status":
            print(json.dumps(status(conn), ensure_ascii=False, indent=2))
        elif args.cmd == "serve":
            httpd = ThreadingHTTPServer((args.host, args.port), make_http_handler(args.db))
            print(f"查询服务已启动: http://{args.host}:{args.port}")
            print("路由: GET /find?catalog_no=  GET /groups  GET /status  "
                  "POST /revoke  POST /restore")
            httpd.serve_forever()
    except LookupError as exc:
        print(f"错误: {exc}")
        raise SystemExit(2)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
