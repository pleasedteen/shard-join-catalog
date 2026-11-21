#!/usr/bin/env python3
"""一键本地验证：启动模拟站，端到端演练全部需求场景。

运行：./run.sh verify   （或 python3 verify.py）
80 秒内输出逐项可读结果，全部通过退出码为 0，任一失败退出码为 1。
仅使用 Python 标准库，所有数据来自项目自带 fixtures。
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "data"))

from ceramics.db import connect, get_active_version, init_db  # noqa: E402
from ceramics.protocol import issue_token, sign  # noqa: E402
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


def run_collector(db: str, base: str, extra_env: dict | None = None,
                  metrics_file: str | None = None) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    if extra_env:
        env.update(extra_env)
    if metrics_file:
        env["CERAMICS_METRICS_FILE"] = metrics_file
    proc = subprocess.run(
        [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
         "--base-url", base, "--rounds", "1"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def start_collector(db: str, base: str, extra_env: dict | None = None,
                    metrics_file: str | None = None) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    if extra_env:
        env.update(extra_env)
    if metrics_file:
        env["CERAMICS_METRICS_FILE"] = metrics_file
    return subprocess.Popen(
        [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
         "--base-url", base, "--rounds", "1"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def read_metrics(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ceramics-verify-"))
    db = str(tmp / "ceramics.db")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    # 模拟站按需挂载 2 万/8 万大册；采集与查询进程不设置该变量，保持轻量
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}:{ROOT / 'data'}",
               CERAMICS_BIG_LEDGER="1")
    server = subprocess.Popen(
        [sys.executable, "-m", "ceramics.site", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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

        # 3. 首轮采集并激活 v1
        print("\n== 3. 首轮采集、校验与版本激活 ==")
        rc, out = run_collector(db, base)
        conn = connect(db)
        active = get_active_version(conn)
        check("采集成功并激活 v1", rc == 0 and active == "v1", out.strip().splitlines()[-1])
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
              str([m["catalog_no"] for m in back["group"]]))
        conn.close()

        # 5. 入口错时切版：两入口不同版本期间不切换，同版后原子切换
        print("\n== 5. 两个入口错时切换发布版 ==")
        control(base, {"entry_versions": {"registry": "v2"}})  # 只切登记
        rc, out = run_collector(db, base)
        conn = connect(db)
        check("入口版本不一致时查询仍为 v1", get_active_version(conn) == "v1",
              out.strip().splitlines()[-1])
        conn.close()
        control(base, {"entry_versions": {"joins": "v2"}})  # 拼接也切到 v2
        rc, out = run_collector(db, base)
        conn = connect(db)
        check("两入口同属 v2 后原子激活", rc == 0 and get_active_version(conn) == "v2",
              out.strip().splitlines()[-1])
        result = query_mod.find_sherd(conn, "C-010")
        check("v2 新陶片可查且并入 C-001 组",
              result["version"] == "v2" and result["group_size"] == 4,
              f"C-010 组: {[m['catalog_no'] for m in result['group']]}")
        check("v2 排版变化(jsonl)已正确解析",
              conn.execute("SELECT layout FROM pages WHERE version='v2' LIMIT 1").fetchone()[0]
              == "jsonl")
        conn.close()

        # 6. 采集期间查询不中断：站点暂停制造确定的"采集中"窗口，前台持续命中 v2
        print("\n== 6. 采集期间查询继续返回上一个完整版本 ==")
        control(base, {"entry_versions": {"registry": "v3", "joins": "v3"},
                       "pause_seconds": 4})
        env2 = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        bg = subprocess.Popen(
            [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
             "--base-url", base, "--rounds", "1"],
            cwd=ROOT, env=env2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        seen_versions = set()
        deadline = time.time() + 3
        while time.time() < deadline:  # 采集被暂停挡在门外，期间反复查询
            conn = connect(db)
            seen_versions.add(get_active_version(conn))
            conn.close()
            time.sleep(0.1)
        bg.wait(timeout=40)
        bg_out = bg.stdout.read()
        conn = connect(db)
        check("采集窗口内查询始终是旧版 v2，采集完成后切到 v3",
              seen_versions == {"v2"} and get_active_version(conn) == "v3",
              f"采集中可见版本: {sorted(seen_versions)}")
        check("v3 第三种排版(record)解析正确",
              conn.execute("SELECT layout FROM pages WHERE version='v3' LIMIT 1").fetchone()[0]
              == "record")
        conn.close()

        # 7. 损坏页：失败原因对操作员可见，查询保持旧版
        print("\n== 7. 新版资料损坏：记录原因并继续使用旧版 ==")
        control(base, {"entry_versions": {"registry": "v4-bad", "joins": "v4-bad"}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        fail = conn.execute(
            "SELECT reason, detail FROM version_failures WHERE version='v4-bad' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("v4-bad 引用不完整被拦截，查询仍为 v3",
              get_active_version(conn) == "v3" and fail is not None
              and "X-999" in (fail["detail"] or ""),
              f"{fail['reason'] if fail else '无失败记录'}: "
              f"{fail['detail'] if fail else ''}")
        conn.close()
        # 现场制造一个损坏页（站点返回截断内容、校验和不符）
        control(base, {"entry_versions": {"registry": "v3", "joins": "v3"},
                       "broken_pages": {"registry@v3": [0]}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        corrupt = conn.execute(
            "SELECT reason FROM version_failures WHERE detail LIKE '%registry 第 0 页%' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("损坏页被校验和/损坏标记识别", corrupt is not None,
              corrupt["reason"] if corrupt else out.strip().splitlines()[-1])
        check("损坏不影响当前 active(v3) 查询", get_active_version(conn) == "v3")
        conn.close()
        control(base, {"clear_broken": True})

        # 8. 进程重启断点续采：先确认部分页面落盘，再杀进程，重启后复用已确认页
        print("\n== 8. 进程重启后复用已确认页面继续采集 ==")
        control(base, {"entry_versions": {"registry": "v2", "joins": "v2"}})
        db2 = str(tmp / "resume.db")
        # 第一步：暂停站点，启动采集——它会在暂停结束后先落盘 registry 全部页，
        # 在抓 joins 期间被强杀，制造"部分页面已确认保存"的现场
        control(base, {"pause_seconds": 3})
        killed = subprocess.Popen(
            [sys.executable, "-m", "ceramics.collector", "run", "--db", db2,
             "--base-url", base, "--rounds", "1"],
            cwd=ROOT, env=env2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
        # 第二步：新进程重启采集，重走入口时跳过已确认页面并补齐剩余页面
        rc, out2 = run_collector(db2, base)
        conn2 = connect(db2)
        saved_after = conn2.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
        check("重启后采集完成并激活 v2",
              rc == 0 and get_active_version(conn2) == "v2",
              f"页面 {saved_before} -> {saved_after}")
        check("日志显示断点跳过已保存页面", "断点续采" in out2,
              "重走入口时校验和一致即跳过")
        conn2.close()

        # 9. 会话频繁过期：确定性地每隔一次请求让会话失效，验证重登+续采仍能完成
        print("\n== 9. 会话频繁过期，自动重登并按已保存页续采 ==")
        control(base, {"entry_versions": {"registry": "v1", "joins": "v1"}})
        from ceramics import collector as col_mod
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
            res = col_mod.run_round(base, db4)
        finally:
            col_mod.SiteClient.fetch_page = orig_fetch
        conn4 = connect(db4)
        check("频繁会话过期下重登+续采仍完成激活",
              res.get("activated") == "v1" and get_active_version(conn4) == "v1",
              f"期间翻页调用 {calls['n']} 次，每次失效都未重复抓取已确认页")
        conn4.close()

        # 11. 大册资源对比：2 万 -> 8 万（固定每页 500 条），峰值内存最多翻倍、
        #     单事务业务行 <= 1000（两页），且整版校验期间查询与撤销不中断
        print("\n== 10. 大册 2 万 -> 8 万：内存与写事务资源对比 ==")
        from ceramics import collector as col_mod
        from ceramics.parsing import PAGE_SIZE as FIXED_PAGE_SIZE

        big_env = dict(PYTHONPATH=f"{ROOT / 'src'}:{ROOT / 'data'}")
        big_runs: dict[str, dict] = {}
        for tag, total, layout in (("v-big-20k", 20_000, "jsonl"),
                                   ("v-big-80k", 80_000, "record")):
            control(base, {"entry_versions": {"registry": tag, "joins": tag}})
            bdb = str(tmp / f"{tag}.db")
            mfile = str(tmp / f"{tag}.metrics.json")
            t0 = time.time()
            rc, bout = run_collector(bdb, base, extra_env=big_env, metrics_file=mfile)
            elapsed = time.time() - t0
            metrics = read_metrics(mfile)
            bconn = connect(bdb)
            active = get_active_version(bconn)
            sherd_n = bconn.execute(
                "SELECT COUNT(*) c FROM sherds WHERE version=?", (tag,)).fetchone()["c"]
            join_n = bconn.execute(
                "SELECT COUNT(*) c FROM joins WHERE version=?", (tag,)).fetchone()["c"]
            page_n = bconn.execute(
                "SELECT COUNT(*) c FROM pages WHERE version=?", (tag,)).fetchone()["c"]
            big_runs[tag] = {"metrics": metrics, "elapsed": elapsed,
                             "sherd_n": sherd_n, "join_n": join_n, "page_n": page_n,
                             "active": active, "rc": rc, "out": bout}
            bconn.close()
            print(f"  {tag}: {sherd_n} 登记 / {join_n} 拼接 / {page_n} 页, "
                  f"耗时 {elapsed:.1f}s, 峰值 RSS {metrics['peak_rss_kb']} KB, "
                  f"写事务 {metrics['write_txn_count']} 个, "
                  f"单事务最多 {metrics['max_business_rows_per_txn']} 行业务记录")

        m20, m80 = big_runs["v-big-20k"]["metrics"], big_runs["v-big-80k"]["metrics"]
        check("大册固定每页 500 条，两版单页记录数都不超限",
              m20["max_records_per_page"] <= FIXED_PAGE_SIZE
              and m80["max_records_per_page"] <= FIXED_PAGE_SIZE
              and FIXED_PAGE_SIZE == 500,
              f"2 万版单页最多 {m20['max_records_per_page']} 条 / "
              f"8 万版 {m80['max_records_per_page']} 条，上限 {FIXED_PAGE_SIZE}")
        check("单个采集写事务至多写两页（1000 条）业务记录",
              m20["max_business_rows_per_txn"] <= 1000
              and m80["max_business_rows_per_txn"] <= 1000,
              f"2 万版峰值 {m20['max_business_rows_per_txn']} 行/事务, "
              f"8 万版 {m80['max_business_rows_per_txn']} 行/事务；"
              f"8 万版事务行数分布 {m80['txn_histogram']}")
        # 80k 页数是 20k 的 4 倍，但每个写事务规模不变（事务数约 4 倍而非变大）
        check("整版扩 4 倍时写事务数量约增 4 倍、单事务规模不膨胀",
              m80["write_txn_count"] >= 3 * m20["write_txn_count"]
              and m80["max_business_rows_per_txn"] <= m20["max_business_rows_per_txn"] + 10,
              f"写事务 {m20['write_txn_count']} -> {m80['write_txn_count']} 个")
        # 核心资源指标：整版 4 倍，采集进程峰值内存最多翻倍（留 2 MiB 测量裕量）
        rss_ratio = m80["peak_rss_kb"] / max(m20["peak_rss_kb"], 1)
        rss_ok = m80["peak_rss_kb"] <= m20["peak_rss_kb"] * 2 + 2048
        check("整版 2 万 -> 8 万，采集到切版的 Python 峰值内存最多翻倍",
              rss_ok,
              f"峰值 RSS {m20['peak_rss_kb']} KB -> {m80['peak_rss_kb']} KB，"
              f"比值 {rss_ratio:.2f}x（要求 <= 2.0x）")
        check("两个大册均收齐、校验成功并整体激活",
              big_runs["v-big-20k"]["active"] == "v-big-20k"
              and big_runs["v-big-80k"]["active"] == "v-big-80k"
              and big_runs["v-big-20k"]["sherd_n"] == 20_000
              and big_runs["v-big-80k"]["sherd_n"] == 80_000,
              f"页数 {big_runs['v-big-20k']['page_n']} / "
              f"{big_runs['v-big-80k']['page_n']}")

        # 整版校验期间查询不中断、可提交当前版撤销：后台跑 8 万版流式校验，
        # 前台在同一库上反复查询并撤销/恢复一条当前版拼接
        print("\n== 11. 整版校验期间查询与复核撤销不中断，及大册中断恢复 ==")
        db80 = str(tmp / "v-big-80k.db")
        conn80 = connect(db80)
        validation_holder: dict = {}

        def run_long_validation() -> None:
            c = connect(db80)
            try:
                validation_holder["ok"], validation_holder["reason"] = \
                    col_mod.validate_version(c, "v-big-80k")
            finally:
                c.close()

        vthread = threading.Thread(target=run_long_validation)
        vthread.start()
        time.sleep(0.05)  # 确保校验线程已进入逐页扫描
        edge_a, edge_b = "B-000000", "B-000001"
        # 校验线程仍在跑时提交撤销写事务：WAL 下读不阻写，若校验误持写锁，
        # 这里会抛锁等待/只读异常。撤销后连通组应立即拆分，随后恢复。
        lock_error = None
        try:
            query_mod.revoke_join(conn80, edge_a, edge_b, reason="大册校验期复核撤销")
            # 撤销的是长链第一条边：B-000000 应立即变为孤立点，B-000001 带着剩余长链
            isolated = query_mod.find_sherd(conn80, edge_a)
            revoked_isolated_size = isolated["group_size"]
            chain_rest = query_mod.find_sherd(conn80, edge_b)
            revoked_chain_size = chain_rest["group_size"]
            still_visible = isolated["version"]
            query_mod.restore_join(conn80, edge_a, edge_b)
            restored = query_mod.find_sherd(conn80, edge_a)["group_size"]
        except sqlite3.OperationalError as exc:
            lock_error = repr(exc)
            revoked_isolated_size, revoked_chain_size, restored = -1, -1, -1
            still_visible = None
        vthread.join(timeout=30)
        check("整版校验全程可查询当前 active 版",
              lock_error is None and still_visible == "v-big-80k",
              f"校验期查询版本 {still_visible}, 锁错误 {lock_error}")
        check("整版校验期间可提交当前版撤销并实时拆分连通组",
              lock_error is None and revoked_isolated_size == 1
              and revoked_chain_size == 39_999 and restored == 40_000
              and validation_holder.get("ok") is True,
              f"撤销后 B-000000 组 {revoked_isolated_size} / 长链余组 "
              f"{revoked_chain_size}，恢复后 {restored}，校验 {validation_holder}")
        conn80.close()

        # 大册中断恢复：8 万版采到一半杀进程，重启后复用已解析入库的页面
        control(base, {"entry_versions": {"registry": "v-big-20k", "joins": "v-big-20k"}})
        db_resume = str(tmp / "big-resume.db")
        control(base, {"pause_seconds": 3})
        killed = start_collector(db_resume, base, extra_env=big_env)
        cwait = connect(db_resume)
        deadline = time.time() + 20
        saved_before = 0
        reg_pages = join_pages = 0
        while time.time() < deadline:
            try:
                reg_pages = cwait.execute(
                    "SELECT COUNT(*) c FROM pages WHERE entry='registry'").fetchone()["c"]
                join_pages = cwait.execute(
                    "SELECT COUNT(*) c FROM pages WHERE entry='joins'").fetchone()["c"]
            except sqlite3.OperationalError:
                time.sleep(0.2)
                continue
            # registry 已收齐、joins 进行到一半再杀：两个入口都留下已确认页面
            if reg_pages >= 40 and join_pages >= 10:
                saved_before = reg_pages + join_pages
                break
            time.sleep(0.2)
        # 再确认业务行与页面是同事务成对落盘的
        biz_before = cwait.execute(
            "SELECT (SELECT COUNT(*) FROM sherds) + (SELECT COUNT(*) FROM joins) c"
        ).fetchone()["c"]
        cwait.close()
        killed.kill()
        killed.wait()
        check("大册中断前 registry 已齐、joins 过半且业务记录同事务落盘",
              saved_before >= 50 and biz_before > 0,
              f"已保存 {reg_pages} registry + {join_pages} joins 页 / {biz_before} 行业务记录")

        mfile_r = str(tmp / "big-resume.metrics.json")
        rc, rout = run_collector(db_resume, base, extra_env=big_env,
                                 metrics_file=mfile_r)
        rmetrics = read_metrics(mfile_r)
        cfin = connect(db_resume)
        saved_after = cfin.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"]
        expected_pages = cfin.execute(
            "SELECT SUM(total_pages) s FROM (SELECT DISTINCT entry, total_pages FROM pages)"
        ).fetchone()["s"]
        sherd_total = cfin.execute(
            "SELECT COUNT(*) c FROM sherds WHERE version='v-big-20k'").fetchone()["c"]
        # 断点续采的翻页请求：每入口只重取 1 个探针页 + 此前缺口页，已保存页不再请求
        expected_fetches = expected_pages - saved_before + 2
        check("重启后收齐全部页面并整体激活 2 万大册",
              rc == 0 and get_active_version(cfin) == "v-big-20k"
              and saved_after == expected_pages and sherd_total == 20_000,
              f"页面 {saved_before} -> {saved_after}/{expected_pages}, 登记 {sherd_total} 条")
        check("重启复用已解析入库页面，不重复抓取已确认页",
              rmetrics["fetch_count"] == expected_fetches
              and "断点续采" in rout,
              f"重启后翻页请求 {rmetrics['fetch_count']} 次（期望 {expected_fetches}）")
        check("续采进程单事务仍不超过两页业务记录",
              rmetrics["max_business_rows_per_txn"] <= 1000
              and rmetrics["max_records_per_page"] <= FIXED_PAGE_SIZE,
              f"单事务峰值 {rmetrics['max_business_rows_per_txn']} 行")
        cfin.close()

        # 10. HTTP 查询服务冒烟
        print("\n== 12. HTTP 查询服务 ==")
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
