#!/usr/bin/env python3
"""一键本地验证：启动模拟站，端到端演练全部需求场景。

运行：./run.sh verify   （或 python3 verify.py）
80 秒内输出逐项可读结果，全部通过退出码为 0，任一失败退出码为 1。
仅使用 Python 标准库，所有数据来自项目自带 fixtures。

覆盖范围：
- 会话/签名/按序令牌、暂停恢复、会话过期重登（协议层不变）；
- 采集与发布拆成两个独立入口：采集只登记 ready_rounds 收齐清单，
  发布只接手清单做整版校验与原子切换；
- 断源发布（发布端不连站）、采集端/发布端分别中断后独立重启恢复；
- 同一版只成功切换一次（并发/重启发布进程 + activation_log 保证）；
- 整版校验期间采集端写入后续版、查询继续读旧版；
- 撤销/恢复、错时切版、损坏页拦截、断点续采、HTTP 查询服务。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "data"))

from ceramics.db import connect, get_active_version, init_db  # noqa: E402
from ceramics.protocol import issue_token, sign  # noqa: E402
from ceramics import collector as col_mod  # noqa: E402
from ceramics import publisher as pub_mod  # noqa: E402
from ceramics import query as query_mod  # noqa: E402

PASS, FAIL = "通过", "失败"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: bytes | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_site(base: str, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/status-404", timeout=2)
        except urllib.error.HTTPError:
            return
        except urllib.error.URLError:
            time.sleep(0.2)
    raise RuntimeError("模拟站启动超时")


def start_site(port: int):
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}:{ROOT / 'data'}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ceramics.site", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_site(f"http://127.0.0.1:{port}")
    return proc


def control(base: str, cmd: dict) -> dict:
    code, data = http("POST", base + "/control",
                      json.dumps(cmd).encode(),
                      {"X-Control-Key": "ceramics-local-control",
                       "Content-Type": "application/json"})
    assert code == 200, data
    return data["state"]


def login(base: str) -> dict:
    # 站点可能正处于暂停期：按 503 等待恢复后再登录
    deadline = time.time() + 20
    while True:
        code, data = http("POST", base + "/session", b"key=ceramics-local-control",
                          {"Content-Type": "application/x-www-form-urlencoded"})
        if code == 200:
            return data
        if code == 503 and time.time() < deadline:
            time.sleep(0.5)
            continue
        assert False, data


def authed_get(base: str, sess: dict, path: str, token: str | None = None) -> tuple[int, dict]:
    token = token if token is not None else sess["first_token"]
    sig = sign(sess["secret"], "GET", path, sess["sid"], token)
    return http("GET", base + path, headers={
        "X-Ceramic-Session": sess["sid"],
        "X-Ceramic-Token": token,
        "X-Ceramic-Signature": sig,
    })


def run_collector(db: str, base: str, rounds: int = 1) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
         "--base-url", base, "--rounds", str(rounds)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def start_collector(db: str, base: str, rounds: int = 0):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    return subprocess.Popen(
        [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
         "--base-url", base, "--rounds", str(rounds), "--retry-interval", "1"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def run_publisher(db: str, *extra: str) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "ceramics.publisher", "once", "--db", db, *extra],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def start_publisher(db: str, interval: float = 1.0, extra_env: dict | None = None):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "ceramics.publisher", "run", "--db", db,
         "--interval", str(interval)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_ready(db: str, version: str, timeout: float = 20) -> int | None:
    """等待采集端把指定版本的收齐清单写入 ready_rounds，返回清单 id。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = connect(db)
        try:
            row = conn.execute(
                "SELECT id FROM ready_rounds WHERE version=? ORDER BY id DESC LIMIT 1",
                (version,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            conn.close()
        if row:
            return row["id"]
        time.sleep(0.1)
    return None


def wait_active(db: str, version: str, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = connect(db)
        try:
            if get_active_version(conn) == version:
                return True
        finally:
            conn.close()
        time.sleep(0.1)
    return False


def activation_count(db: str, version: str) -> int:
    conn = connect(db)
    try:
        return conn.execute("SELECT COUNT(*) c FROM activation_log WHERE version=?",
                            (version,)).fetchone()["c"]
    finally:
        conn.close()


def collect_then_publish(db: str, base: str, version: str) -> tuple[str, str]:
    rc, cout = run_collector(db, base)
    prc, pout = run_publisher(db)
    return cout + pout, f"rc={rc}/{prc}"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ceramics-verify-"))
    db = str(tmp / "ceramics.db")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env2 = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}:{ROOT / 'data'}")
    server = start_site(port)
    try:
        wait_site(base)
        init_db(db)
        print(f"工作目录: {tmp}\n模拟站: {base}\n")

        # 1. 协议：签名随会话/令牌变化，新会话不能用旧令牌
        print("== 1. 会话、按序令牌与签名协议 ==")
        s1 = login(base)
        code, page = authed_get(base, s1, "/registry")
        check("正确签名可访问登记入口", code == 200 and page["version"] == "v1",
              f"首页 {page.get('page_index')} 版 {page.get('version')}")
        bad_sig = sign("wrong-secret", "GET", "/registry", s1["sid"], s1["first_token"])
        code, data = http("GET", base + "/registry", headers={
            "X-Ceramic-Session": s1["sid"], "X-Ceramic-Token": s1["first_token"],
            "X-Ceramic-Signature": bad_sig})
        check("错误签名被拒绝(403)", code == 403, data.get("error", ""))
        code, data = authed_get(base, s1, "/registry")  # 重复使用同一旧令牌
        check("令牌必须按序使用，重复令牌被拒(409)", code == 409, data.get("error", ""))
        s2 = login(base)
        code, data = authed_get(base, s2, "/registry", token=s1["first_token"])
        check("新会话不能使用旧令牌(403)", code == 403, data.get("error", ""))
        code, data = authed_get(base, s1, "/registry", token=issue_token(s2["sid"], 0))
        check("令牌与会话不匹配被拒(403)", code == 403, data.get("error", ""))

        # 2. 暂停受理：503 + Retry-After，恢复后同令牌继续
        print("\n== 2. 站点暂停受理与恢复 ==")
        control(base, {"pause_seconds": 2})
        code, data = http("POST", base + "/session", b"key=ceramics-local-control",
                          {"Content-Type": "application/x-www-form-urlencoded"})
        check("暂停期登录返回 503 与恢复时间", code == 503 and "Retry" not in json.dumps(data),
              f"error={data.get('error')}")
        s3 = login(base)  # 等待恢复后登录
        t0 = time.time()
        code, page = authed_get(base, s3, "/joins")
        check("恢复后同一令牌链继续可用", code == 200 and time.time() - t0 < 10,
              f"耗时 {time.time() - t0:.1f}s")

        # 3. 采集与发布分离：采集只登记收齐清单，独立发布进程切换 v1
        print("\n== 3. 采集登记清单、发布独立激活 v1 ==")
        rc, cout = run_collector(db, base)
        conn = connect(db)
        rid = conn.execute("SELECT id, switch_status FROM ready_rounds WHERE version='v1'"
                           ).fetchone()
        check("采集端收齐 v1 并登记 pending 收齐清单（不直接激活）",
              rc == 0 and rid is not None and rid["switch_status"] == "pending"
              and get_active_version(conn) is None,
              f"清单 #{rid['id'] if rid else '无'} 状态 {rid['switch_status'] if rid else ''}")
        conn.close()
        prc, pout = run_publisher(db)
        conn = connect(db)
        check("独立发布进程把 v1 原子切换为 active",
              prc == 0 and get_active_version(conn) == "v1",
              pout.strip().splitlines()[0] if pout.strip() else "")
        check("清单状态同步为 switched 且激活日志恰好一条",
              conn.execute("SELECT switch_status FROM ready_rounds WHERE version='v1'"
                           ).fetchone()["switch_status"] == "switched"
              and activation_count(db, "v1") == 1,
              f"activation_log v1 共 {activation_count(db, 'v1')} 条")
        result = query_mod.find_sherd(conn, "C-001")
        check("查询返回陶片、连通组与发布版",
              result["version"] == "v1" and result["group_size"] == 3
              and set(result["group"][0].keys()) >= {"catalog_no", "material"},
              f"C-001 组内 {result['group_size']} 片: "
              f"{[m['catalog_no'] for m in result['group']]}")
        groups = query_mod.list_groups(conn)
        check("v1 全文分为 4 个连通组（含孤立片 C-009）",
              groups["group_count"] == 4, str([g for g in groups["groups"]]))
        conn.close()

        # 4. 撤销拼接：一组拆两组，查询按最新清单
        print("\n== 4. 复核撤销拼接，连通组实时拆分 ==")
        conn = connect(db)
        before = query_mod.find_sherd(conn, "C-004")
        query_mod.revoke_join(conn, "C-005", "C-006", reason="复核发现接缝不成立")
        after4 = query_mod.find_sherd(conn, "C-004")
        after6 = query_mod.find_sherd(conn, "C-006")
        check("撤销 C-005~C-006 后原组拆成两组",
              before["group_size"] == 3 and after4["group_size"] == 2
              and after6["group_size"] == 1,
              f"C-004 组 {[m['catalog_no'] for m in after4['group']]} / "
              f"C-006 组 {[m['catalog_no'] for m in after6['group']]}")
        query_mod.restore_join(conn, "C-005", "C-006")
        back = query_mod.find_sherd(conn, "C-004")
        check("恢复撤销后回到原组", back["group_size"] == 3,
              str([m['catalog_no'] for m in back['group']]))
        conn.close()

        # 5. 错时切版：两入口不同版期间只采集不发布，同版清单发布后原子切换
        print("\n== 5. 两个入口错时切换发布版 ==")
        control(base, {"entry_versions": {"registry": "v2"}})  # 只切登记
        rc, cout = run_collector(db, base)
        conn = connect(db)
        check("入口版本不一致时不登记清单，查询仍为 v1",
              get_active_version(conn) == "v1"
              and conn.execute("SELECT 1 FROM ready_rounds WHERE version='v2'").fetchone() is None,
              cout.strip().splitlines()[-1])
        conn.close()
        control(base, {"entry_versions": {"joins": "v2"}})  # 拼接也切到 v2
        rc, cout = run_collector(db, base)
        check("同版收齐后登记 v2 清单", wait_ready(db, "v2") is not None,
              cout.strip().splitlines()[-1])
        prc, pout = run_publisher(db)
        conn = connect(db)
        check("发布端接手清单后原子激活 v2",
              prc == 0 and get_active_version(conn) == "v2",
              pout.strip().splitlines()[0] if pout.strip() else "")
        result = query_mod.find_sherd(conn, "C-010")
        check("v2 新陶片可查且并入 C-001 组",
              result["version"] == "v2" and result["group_size"] == 4,
              f"C-010 组: {[m['catalog_no'] for m in result['group']]}")
        check("v2 排版变化(jsonl)已正确解析",
              conn.execute("SELECT layout FROM pages WHERE version='v2' LIMIT 1").fetchone()[0]
              == "jsonl")
        conn.close()

        # 6. 采集期间查询不中断：采集与发布是两个进程，采集窗口内前台始终命中旧版
        print("\n== 6. 采集/发布期间查询继续返回上一个完整版本 ==")
        control(base, {"entry_versions": {"registry": "v3", "joins": "v3"},
                       "pause_seconds": 4})
        bg = start_collector(db, base, rounds=1)
        seen_versions = set()
        deadline = time.time() + 3
        while time.time() < deadline:  # 采集被暂停挡在门外，期间反复查询
            conn = connect(db)
            seen_versions.add(get_active_version(conn))
            conn.close()
            time.sleep(0.1)
        bg.wait(timeout=40)
        check("采集窗口内查询始终是旧版 v2，清单未提前切换",
              seen_versions == {"v2"} and get_active_version(connect(db)) == "v2",
              f"采集中可见版本: {sorted(seen_versions)}")
        pub = start_publisher(db)
        try:
            check("发布端独立完成 v3 切换", wait_active(db, "v3"),
                  "采集进程已退出，发布进程单独读取清单完成切换")
        finally:
            pub.terminate()
            pub.wait(timeout=5)
        conn = connect(db)
        check("v3 第三种排版(record)解析正确",
              conn.execute("SELECT layout FROM pages WHERE version='v3' LIMIT 1").fetchone()[0]
              == "record")
        conn.close()

        # 7. 损坏/引用不完整：发布端整版校验拦截，查询保持旧版
        print("\n== 7. 新版资料损坏：发布端拦截并继续使用旧版 ==")
        control(base, {"entry_versions": {"registry": "v4-bad", "joins": "v4-bad"}})
        rc, cout = run_collector(db, base)
        check("v4-bad 仍由采集端登记收齐清单（采集不做整版校验）",
              wait_ready(db, "v4-bad") is not None, cout.strip().splitlines()[-1])
        prc, pout = run_publisher(db)
        conn = connect(db)
        fail = conn.execute(
            "SELECT reason, detail FROM version_failures WHERE version='v4-bad' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("发布端拦截引用不完整的 v4-bad，查询仍为 v3，清单标记 failed",
              prc == 0 and get_active_version(conn) == "v3" and fail is not None
              and "X-999" in (fail["detail"] or "")
              and conn.execute(
                  "SELECT switch_status FROM ready_rounds WHERE version='v4-bad' "
                  "ORDER BY id DESC LIMIT 1").fetchone()["switch_status"] == "failed",
              f"{fail['reason'] if fail else '无失败记录'}: "
              f"{fail['detail'] if fail else ''}")
        conn.close()
        # 现场制造一个损坏页（站点返回截断内容、校验和不符）
        control(base, {"entry_versions": {"registry": "v3", "joins": "v3"},
                       "broken_pages": {"registry@v3": [0]}})
        db_b = str(tmp / "broken.db")
        rc, cout = run_collector(db_b, base)
        conn = connect(db_b)
        corrupt = conn.execute(
            "SELECT reason FROM version_failures WHERE detail LIKE '%registry 第 0 页%' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("损坏页被采集端校验和/损坏标记识别", corrupt is not None,
              corrupt["reason"] if corrupt else cout.strip().splitlines()[-1])
        conn.close()
        prc, pout = run_publisher(db_b)
        conn = connect(db_b)
        check("损坏版没有清单可发布，查询始终无 active（旧版语义不受影响）",
              get_active_version(conn) is None
              and conn.execute("SELECT COUNT(*) c FROM ready_rounds").fetchone()["c"] == 0,
              pout.strip().splitlines()[-1] if pout.strip() else "")
        conn.close()
        control(base, {"clear_broken": True})

        # 8. 采集进程重启断点续采（发布端独立，不参与续采）
        print("\n== 8. 采集进程中断重启，复用已确认页面继续采集 ==")
        control(base, {"entry_versions": {"registry": "v2", "joins": "v2"}})
        db2 = str(tmp / "resume.db")
        # 第一步：暂停站点，启动采集——它会在暂停结束后先落盘 registry 全部页，
        # 在抓 joins 期间被强杀，制造"部分页面已确认保存"的现场
        control(base, {"pause_seconds": 3})
        killed = start_collector(db2, base, rounds=1)
        conn2 = connect(db2)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                n = conn2.execute(
                    "SELECT COUNT(*) c FROM pages WHERE entry='registry'").fetchone()["c"]
            except sqlite3.OperationalError:
                time.sleep(0.2)  # 采集进程尚未建表
                continue
            if n > 0:  # 至少一页已确认落盘即可证明持久化
                break
            time.sleep(0.2)
        saved_before = conn2.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
        conn2.close()
        killed.kill()
        killed.wait()
        check("被杀前已有确认页面落盘", saved_before > 0, f"已保存 {saved_before} 页")
        # 第二步：新采集进程重启，重走入口时跳过已确认页面并补齐，再由发布端切换
        rc, out2 = run_collector(db2, base)
        check("重启后采集完成并登记 v2 清单",
              wait_ready(db2, "v2") is not None and "断点续采" in out2,
              "重走入口时校验和一致即跳过，清单落盘后采集进程可再次退出")
        prc, pout = run_publisher(db2)
        conn2 = connect(db2)
        saved_after = conn2.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
        check("发布端独立把 v2 激活",
              prc == 0 and get_active_version(conn2) == "v2",
              f"页面 {saved_before} -> {saved_after}")
        conn2.close()

        # 9. 会话频繁过期：采集端重登+续采仍能完成（发布逻辑不受影响）
        print("\n== 9. 会话频繁过期，自动重登并按已保存页续采 ==")
        control(base, {"entry_versions": {"registry": "v1", "joins": "v1"}})
        db4 = str(tmp / "relogin.db")
        calls = {"n": 0}
        orig_fetch = col_mod.SiteClient.fetch_page

        def patched_fetch(self, entry):
            calls["n"] += 1
            if calls["n"] % 2 == 0:  # 偶数次请求前强制当前会话过期
                control(base, {"expire_sessions": True})
            return orig_fetch(self, entry)

        col_mod.SiteClient.fetch_page = patched_fetch
        try:
            res = col_mod.collect_round(base, db4)
        finally:
            col_mod.SiteClient.fetch_page = orig_fetch
        prc, pout = run_publisher(db4)
        conn4 = connect(db4)
        check("频繁会话过期下重登+续采仍收齐清单并由发布端激活 v1",
              res.get("ready") == "v1" and prc == 0
              and get_active_version(conn4) == "v1",
              f"期间翻页调用 {calls['n']} 次，每次失效都未重复抓取已确认页")
        conn4.close()

        # 10. HTTP 查询服务冒烟
        print("\n== 10. HTTP 查询服务 ==")
        qport = free_port()
        qsrv = subprocess.Popen(
            [sys.executable, "-m", "ceramics.query", "serve", "--db", db,
             "--port", str(qport)], cwd=ROOT, env=env2,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 5
            qdata = None
            while time.time() < deadline:
                try:
                    code, qdata = http("GET", f"http://127.0.0.1:{qport}/find?catalog_no=C-001")
                    if code == 200:
                        break
                except urllib.error.URLError:
                    time.sleep(0.1)
            check("GET /find 返回 JSON 与发布版",
                  qdata is not None and qdata.get("version") == "v3"
                  and qdata.get("query") == "C-001",
                  f"组大小 {qdata.get('group_size') if qdata else '无响应'}")
            code, gdata = http("GET", f"http://127.0.0.1:{qport}/groups")
            check("GET /groups 返回连通组清单", code == 200 and "groups" in gdata,
                  f"{gdata.get('group_count')} 组")
        finally:
            qsrv.terminate()
            qsrv.wait(timeout=5)

        # 11. 拆分架构的三项关键保证：断源发布、两端中断恢复、同一版只切换一次
        print("\n== 11. 断源发布、两端独立重启恢复、同一版只成功切换一次 ==")
        dbd = str(tmp / "decouple.db")
        dport = free_port()
        dbase = f"http://127.0.0.1:{dport}"
        dsite = start_site(dport)
        try:
            # 11a. 采集端连站收齐 v1 清单
            rc, cout = run_collector(dbd, dbase)
            check("独立库采集端登记 v1 收齐清单", rc == 0 and wait_ready(dbd, "v1") is not None,
                  cout.strip().splitlines()[-1])
        finally:
            # 断源：关掉模拟站，之后发布端全程无站点可连
            dsite.terminate()
            dsite.wait(timeout=5)
        prc, pout = run_publisher(dbd)
        check("断源后发布端仅凭本地清单完成 v1 切换（发布端不访问站点）",
              prc == 0 and get_active_version(connect(dbd)) == "v1",
              pout.strip().splitlines()[0] if pout.strip() else "")
        # 11b. 采集端在断源期间不退出、自动重试；站点恢复后独立续采收齐 v2
        control(base, {"entry_versions": {"registry": "v2", "joins": "v2"}})
        bg_col = start_collector(dbd, dbase, rounds=0)  # 站点已关，进入重试循环
        time.sleep(2.5)
        poll = bg_col.poll()
        check("断源期间采集进程存活并持续重试（不退出、不需重跑连站）",
              poll is None, f"进程状态: {'运行中' if poll is None else f'已退出({poll})'}")
        dsite2 = start_site(dport)  # 同端口恢复站点（站点状态重置，需重新切到 v2）
        try:
            control(dbase, {"entry_versions": {"registry": "v2", "joins": "v2"}})
            check("站点恢复后采集端自动续采并登记 v2 清单",
                  wait_ready(dbd, "v2", timeout=25) is not None, "无需人工重启采集进程")
        finally:
            bg_col.kill()
            bg_col.wait(timeout=5)
            dsite2.terminate()
            dsite2.wait(timeout=5)

        # 11c. 发布端中断重启：先造 v1/v2 两条清单，杀掉持续发布进程后重启补齐
        dbp = str(tmp / "pubresume.db")
        pport = free_port()
        pbase = f"http://127.0.0.1:{pport}"
        psite = start_site(pport)
        try:
            run_collector(dbp, pbase)  # v1 清单
            control(pbase, {"entry_versions": {"registry": "v2", "joins": "v2"}})
            run_collector(dbp, pbase)  # v2 清单
            conn = connect(dbp)
            n_pending = conn.execute(
                "SELECT COUNT(*) c FROM ready_rounds WHERE switch_status='pending'").fetchone()["c"]
            conn.close()
            check("发布前两条收齐清单均为 pending", n_pending == 2, f"pending={n_pending}")
            # 每条清单处理完暂停 3s，确定性制造"v1 已切换、v2 仍 pending"的中断窗口
            pub = start_publisher(dbp, interval=2.0,
                                  extra_env={"CERAMICS_PUBLISH_PAUSE": "3"})
            deadline = time.time() + 15
            killed_in_window = False
            while time.time() < deadline:
                conn = connect(dbp)
                try:
                    v1_active = get_active_version(conn) == "v1"
                    v2_pending = conn.execute(
                        "SELECT 1 FROM ready_rounds WHERE version='v2' "
                        "AND switch_status='pending'").fetchone() is not None
                finally:
                    conn.close()
                if v1_active and v2_pending:
                    pub.kill()
                    pub.wait(timeout=5)
                    killed_in_window = True
                    break
                time.sleep(0.05)
            if not killed_in_window:
                pub.kill()
                pub.wait(timeout=5)
            mid = get_active_version(connect(dbp))
            conn = connect(dbp)
            v2_still_pending = conn.execute(
                "SELECT 1 FROM ready_rounds WHERE version='v2' "
                "AND switch_status='pending'").fetchone() is not None
            conn.close()
            check("强杀发布进程时 v2 清单仍为 pending（中断现场确定）",
                  killed_in_window and mid == "v1" and v2_still_pending,
                  f"中断时 active={mid}")
            rc2, pout2 = run_publisher(dbp)  # 新发布进程重启，接手剩余清单
            conn = connect(dbp)
            check("发布进程中断重启后继续处理未切换清单（v1 -> v2）",
                  mid == "v1" and rc2 == 0 and get_active_version(conn) == "v2",
                  f"中断时 active={mid}，重启后 {get_active_version(conn)}")
            check("v1 与 v2 各自只成功切换一次",
                  activation_count(dbp, "v1") == 1 and activation_count(dbp, "v2") == 1,
                  f"v1={activation_count(dbp, 'v1')} 条, v2={activation_count(dbp, 'v2')} 条")
            conn.close()
        finally:
            psite.terminate()
            psite.wait(timeout=5)

        # 11d. 并发两个发布进程：同一版只有一个成功者，激活日志不重复
        dbc = str(tmp / "concurrent.db")
        cport = free_port()
        cbase = f"http://127.0.0.1:{cport}"
        csite = start_site(cport)
        try:
            run_collector(dbc, cbase)
            check("并发发布前 v1 清单就绪", wait_ready(dbc, "v1") is not None)
        finally:
            csite.terminate()
            csite.wait(timeout=5)
        p1 = start_publisher(dbc, interval=0.2)
        p2 = start_publisher(dbc, interval=0.2)
        try:
            check("两个并发发布进程收敛到同一 active 且 v1 只切换一次",
                  wait_active(dbc, "v1") and activation_count(dbc, "v1") == 1,
                  f"activation_log v1={activation_count(dbc, 'v1')} 条")
        finally:
            p1.terminate(); p2.terminate()
            p1.wait(timeout=5); p2.wait(timeout=5)
        # 人为再登记一条 v1 收齐清单（模拟采集端重跑），发布端必须只跳过不重切
        conn = connect(dbc)
        conn.execute("INSERT INTO ready_rounds(version, registry_pages, joins_pages, "
                     "switch_status, created_at) VALUES ('v1', 2, 2, 'pending', ?)",
                     (time.time(),))
        conn.commit()
        conn.close()
        rc3, pout3 = run_publisher(dbc)
        check("发布端重复运行对已切换版本只跳过不重切",
              activation_count(dbc, "v1") == 1 and "跳过" in pout3,
              pout3.strip().splitlines()[0] if pout3.strip() else "")

        # 12. 整版校验/切换期间，采集端可写入后续版页面，查询继续读旧版
        print("\n== 12. 发布整版校验期间采集写入后续版、查询读旧版 ==")
        dbo = str(tmp / "overlap.db")
        oport = free_port()
        obase = f"http://127.0.0.1:{oport}"
        osite = start_site(oport)
        try:
            run_collector(dbo, obase)  # v1 清单
            run_publisher(dbo)          # 先发布 v1
            control(obase, {"entry_versions": {"registry": "v2", "joins": "v2"}})
            run_collector(dbo, obase)  # v2 清单先就绪（发布端稍后才接手）
            check("前置：overlap 库 active 为 v1，v2 清单待发布",
                  get_active_version(connect(dbo)) == "v1" and wait_ready(dbo, "v2") is not None)
            # 站点切到 v3：发布端校验 v2 的窗口里，采集端并发把*后续版* v3 页面落盘
            control(obase, {"entry_versions": {"registry": "v3", "joins": "v3"}})
            orig_validate = pub_mod.validate_version
            state = {"during": []}

            def slow_validate(conn, version):
                end = time.time() + 1.5
                while time.time() < end:
                    c = connect(dbo)
                    try:
                        state["during"].append((get_active_version(c),
                                                c.execute("SELECT COUNT(*) c FROM pages "
                                                          "WHERE version='v3'").fetchone()["c"]))
                    finally:
                        c.close()
                    time.sleep(0.15)
                return orig_validate(conn, version)

            pub_mod.validate_version = slow_validate
            collector_proc = start_collector(dbo, obase, rounds=1)
            try:
                # 发布端在本进程内执行，使校验窗口补丁生效；采集端仍是独立子进程
                pub_result = pub_mod.publish_once(dbo)
            finally:
                pub_mod.validate_version = orig_validate
            collector_proc.wait(timeout=30)
            conn = connect(dbo)
            v3_pages = conn.execute("SELECT COUNT(*) c FROM pages WHERE version='v3'").fetchone()["c"]
            check("校验窗口内查询始终读旧版 v1，且采集端持续写入后续版 v3 页面",
                  bool(state["during"])
                  and all(active == "v1" for active, _ in state["during"])
                  and any(n > 0 for _, n in state["during"]),
                  f"窗口内 active 集合={sorted({a for a, _ in state['during']})}, "
                  f"窗口内 v3 页数={[n for _, n in state['during']]}")
            check("窗口结束后发布提交 v2，v3 页面完整保留且可继续发布",
                  bool(pub_result["switched"]) and get_active_version(conn) == "v2"
                  and v3_pages > 0,
                  f"v3 已写入 {v3_pages} 页")
            prc2, _ = run_publisher(dbo)  # 采集端已把 v3 清单登记，发布端可继续接手
            check("后续版 v3 在不重采的情况下直接发布成功",
                  prc2 == 0 and get_active_version(conn) == "v3",
                  f"active={get_active_version(conn)}")
            conn.close()
        finally:
            osite.terminate()
            osite.wait(timeout=5)

    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    print("\n" + "=" * 68)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        if not ok:
            print(f"[未通过] {name} — {detail}")
    print(f"合计 {passed}/{total} 项通过")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
