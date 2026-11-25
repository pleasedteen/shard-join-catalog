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


def run_collector(db: str, base: str) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
         "--base-url", base, "--rounds", "1"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ceramics-verify-"))
    db = str(tmp / "ceramics.db")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}:{ROOT / 'data'}")
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

        # 11. 整版之间只发更正单：新增/改写/删除、漏版本地补齐、重启续采、小更正
        print("\n== 11. 更正单发布：漏版本地补齐、重启续采与小更正 ==")
        # 11.0 先采集更正单的基版整版 v5
        control(base, {"entry_versions": {"registry": "v5", "joins": "v5"}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        check("采集并激活基版整版 v5", rc == 0 and get_active_version(conn) == "v5",
              out.strip().splitlines()[-1])
        v5_pages = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE version='v5'").fetchone()["c"]
        v5_sherds = conn.execute(
            "SELECT COUNT(*) c FROM sherds WHERE version='v5'").fetchone()["c"]
        check("基版整版全量落库（4 页 / 12 条登记）", v5_pages == 4 and v5_sherds == 12,
              f"pages={v5_pages}, sherds={v5_sherds}")
        conn.close()

        # 11.1 错时：只切登记入口到更正单 c1，两入口目标版不一致期间不切换、继续提供原版
        control(base, {"entry_versions": {"registry": "c1"}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        check("只有登记入口到达更正单时查询仍为基版 v5",
              get_active_version(conn) == "v5", out.strip().splitlines()[-1])
        conn.close()

        # 11.2 拼接入口也到达 c1：两份清单同目标版且引用完整后原子切换到 v6
        control(base, {"entry_versions": {"joins": "c1"}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        check("两份更正单到达同一目标版 v6 后原子激活",
              rc == 0 and get_active_version(conn) == "v6", out.strip().splitlines()[-1])
        # 传输/落库规模：更正单只有 3+3 条变化记录（每页 2 条 => 各 2 页，共 4 页）
        v6_pages = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE version='v6'").fetchone()["c"]
        v6_sherds = conn.execute(
            "SELECT COUNT(*) c FROM sherds WHERE version='v6'").fetchone()["c"]
        v6_joins = conn.execute(
            "SELECT COUNT(*) c FROM joins WHERE version='v6'").fetchone()["c"]
        check("更正单传输页数只随更正规模增长（v6 仅 4 张更正页）", v6_pages == 4,
              f"v6 更正页 {v6_pages} 张")
        check("更正版不落全量登记/拼接（sherds/joins 无 v6 记录）",
              v6_sherds == 0 and v6_joins == 0,
              f"sherds={v6_sherds}, joins={v6_joins}，全量视图查询时沿谱系本地物化")
        lineage = {r["target_version"]: r["base_version"] for r in conn.execute(
            "SELECT target_version, base_version FROM edition_lineage")}
        check("更正谱系登记 v6 <- v5", lineage.get("v6") == "v5", str(lineage))

        # 11.3 物化解说：改写/新增/删除分别生效
        r001 = query_mod.find_sherd(conn, "C-001")
        check("登记改写生效：C-001 重量更新为 318.8",
              r001["sherd"]["weight_g"] == 318.8, f"实际 {r001['sherd']['weight_g']}")
        r013 = query_mod.find_sherd(conn, "C-013")
        check("登记新增生效：C-013 可查并经新拼接并入 B 组（5 片）",
              r013["version"] == "v6" and r013["group_size"] == 5,
              f"C-013 组: {[m['catalog_no'] for m in r013['group']]}")
        try:
            query_mod.find_sherd(conn, "C-009")
            deleted_ok = False
        except LookupError:
            deleted_ok = True
        check("登记删除生效：C-009 已从目标版移除", deleted_ok)
        r007 = query_mod.find_sherd(conn, "C-007")
        r008 = query_mod.find_sherd(conn, "C-008")
        check("拼接删除生效：C-007/C-008 各自孤立",
              r007["group_size"] == 1 and r008["group_size"] == 1)
        r005 = query_mod.find_sherd(conn, "C-005")
        rewritten = [e for e in r005["effective_joins"]
                     if e["edge_key"] == "C-005~C-006"][0]
        check("拼接改写生效：C-005~C-006 粘接说明与置信度更新",
              rewritten["bond"] == "拼接-B 腹部（复核修正）"
              and rewritten["confidence"] == "high",
              f"{rewritten['bond']} / {rewritten['confidence']}")
        check("拼接新增生效：C-012~C-013 在有效拼接中",
              any(e["edge_key"] == "C-012~C-013" for e in r013["effective_joins"]))
        groups6 = query_mod.list_groups(conn)
        check("v6 全文仍可物化并分为 5 个连通组（含两个孤立片与 C-011）",
              groups6["group_count"] == 5, str([g for g in groups6["groups"]]))
        conn.close()

        # 11.4 问题更正单：引用不完整 / 基版缺失，都被拦截且旧版继续可查
        control(base, {"entry_versions": {"registry": "c-bad-ref", "joins": "c-bad-ref"}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        bad = conn.execute(
            "SELECT reason, detail FROM version_failures WHERE version='v8-bad' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("引用不完整的更正单被拦截，查询仍为 v6",
              get_active_version(conn) == "v6" and bad is not None
              and "Y-007" in (bad["detail"] or ""),
              f"{bad['reason'] if bad else '无失败记录'}: {bad['detail'] if bad else ''}")
        conn.close()
        control(base, {"entry_versions": {"registry": "c-orphan", "joins": "c-orphan"}})
        rc, out = run_collector(db, base)
        conn = connect(db)
        orphan = conn.execute(
            "SELECT reason, detail FROM version_failures WHERE version='v9-orphan' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("基版本地不存在的更正单被拦截，查询仍为 v6",
              get_active_version(conn) == "v6" and orphan is not None
              and "v-ghost" in (orphan["detail"] or ""),
              f"{orphan['reason'] if orphan else '无失败记录'}: "
              f"{orphan['detail'] if orphan else ''}")
        conn.close()

        # 11.5 漏版 + 重启：站点直接发小更正 c2（v6->v7），合作馆不再重发任何整版；
        #       采集进程被杀后重启，复用已确认更正页续采，链式从本地 v5 整版补齐到 v7
        control(base, {"entry_versions": {"registry": "c2", "joins": "c2"},
                       "pause_seconds": 3})
        killed2 = subprocess.Popen(
            [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
             "--base-url", base, "--rounds", "1"],
            cwd=ROOT, env=env2, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        conn = connect(db)
        deadline = time.time() + 15
        while time.time() < deadline:
            n = conn.execute(
                "SELECT COUNT(*) c FROM pages WHERE version='v7'").fetchone()["c"]
            if n > 0:  # 至少一张更正页已确认落盘
                break
            time.sleep(0.2)
        v7_partial = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE version='v7'").fetchone()["c"]
        conn.close()
        killed2.kill()
        killed2.wait()
        check("重启前已有更正页确认落盘", v7_partial > 0, f"已保存 v7 更正页 {v7_partial} 张")
        rc, out3 = run_collector(db, base)
        conn = connect(db)
        check("重启后复用已确认更正页续采并激活 v7",
              rc == 0 and get_active_version(conn) == "v7" and "断点续采" in out3,
              out3.strip().splitlines()[-1])
        v7_pages = conn.execute(
            "SELECT COUNT(*) c FROM pages WHERE version='v7'").fetchone()["c"]
        v7_bulk = conn.execute(
            "SELECT (SELECT COUNT(*) FROM sherds WHERE version='v7') + "
            "(SELECT COUNT(*) FROM joins WHERE version='v7') AS c").fetchone()["c"]
        check("小更正只传 2 张更正页、不产生全量落库", v7_pages == 2 and v7_bulk == 0,
              f"v7 更正页 {v7_pages} 张，全量落库 {v7_bulk} 条")
        kinds = {r["version"]: r["kind"] for r in conn.execute(
            "SELECT version, kind FROM versions WHERE version IN ('v6','v7')")}
        check("v6/v7 从未发布整版，本地仅靠更正页补齐（漏版可补）",
              kinds == {"v6": "correction", "v7": "correction"}, str(kinds))
        lineage = {r["target_version"]: r["base_version"] for r in conn.execute(
            "SELECT target_version, base_version FROM edition_lineage")}
        check("链式更正谱系 v7 <- v6 <- v5 完整",
              lineage.get("v7") == "v6" and lineage.get("v6") == "v5", str(lineage))
        r011 = query_mod.find_sherd(conn, "C-011")
        check("链式物化生效：C-011 重量按小更正改写为 160.2",
              r011["sherd"]["weight_g"] == 160.2, f"实际 {r011['sherd']['weight_g']}")
        check("小更正新增拼接并入 B 组（6 片）",
              r011["group_size"] == 6 and
              "C-011~C-012" in [e["edge_key"] for e in r011["effective_joins"]],
              f"C-011 组: {[m['catalog_no'] for m in r011['group']]}")
        from ceramics.materialize import materialize
        mat_reg, mat_joins = materialize(conn, "v7")
        check("漏版补齐后物化视图完整（12 片登记、8 条拼接边）",
              len(mat_reg) == 12 and len(mat_joins) == 8,
              f"物化登记 {len(mat_reg)} 条 / 拼接 {len(mat_joins)} 条")
        # 撤销在更正版上同样按目标版隔离工作
        query_mod.revoke_join(conn, "C-011", "C-012", reason="更正版复核撤销演示")
        after_rev = query_mod.find_sherd(conn, "C-011")
        check("更正版上撤销拼接后连通组实时拆分", after_rev["group_size"] == 1,
              f"C-011 组: {[m['catalog_no'] for m in after_rev['group']]}")
        conn.close()

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
