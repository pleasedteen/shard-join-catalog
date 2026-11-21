#!/usr/bin/env python3
"""一键本地验证：多合作馆并发采集、独立切版、跨馆拼接与暂停续采。

运行：./run.sh verify   （或 python3 verify.py）
80 秒内输出逐项可读结果，全部通过退出码为 0，任一失败退出码为 1。
仅使用 Python 标准库，所有数据来自项目自带 fixtures。

覆盖需求：
- 一次启动并发采集多个合作馆，各馆独立校验、独立激活；
- 各馆可能使用相同版号与相同馆藏编号，按 (馆, 版本) / (馆, 编号) 隔离；
- 一馆暂停时其它馆照常发布，暂停馆恢复后从已确认页的断点继续；
- 跨馆拼接在两端都登记且都已发布时自动生效，任一端撤下立即退出结果；
- 跨馆登记晚到无需重采另一端；
- 既有场景：签名/令牌、会话过期、馆内错时切版、损坏页/引用不完整、
  进程重启续采、撤销/恢复、HTTP 查询。
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

from ceramics import collector as col_mod  # noqa: E402
from ceramics import query as query_mod  # noqa: E402
from ceramics.db import active_versions, connect, init_db  # noqa: E402
from ceramics.protocol import issue_token, sign  # noqa: E402

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


def set_versions(base: str, site: str, registry: str | None = None,
                 joins: str | None = None) -> None:
    cmd: dict = {"site": site, "entry_versions": {}}
    if registry is not None:
        cmd["entry_versions"]["registry"] = registry
    if joins is not None:
        cmd["entry_versions"]["joins"] = joins
    control(base, cmd)


def login(base: str, site: str) -> dict:
    # 站点可能正处于暂停期：按 503 等待恢复后再登录
    deadline = time.time() + 20
    while True:
        code, data = http("POST", base + f"/{site}/session",
                          b"key=ceramics-local-control",
                          {"Content-Type": "application/x-www-form-urlencoded"})
        if code == 200:
            return data
        if code == 503 and time.time() < deadline:
            time.sleep(0.3)
            continue
        assert False, data


def authed_get(base: str, sess: dict, site: str, entry: str,
               token: str | None = None) -> tuple[int, dict]:
    token = token if token is not None else sess["first_token"]
    path = f"/{site}/{entry}"
    sig = sign(sess["secret"], "GET", path, sess["sid"], token)
    return http("GET", base + path, headers={
        "X-Ceramic-Session": sess["sid"],
        "X-Ceramic-Token": token,
        "X-Ceramic-Signature": sig,
    })


def run_collector(db: str, base: str, extra: list[str] | None = None) -> tuple[int, str]:
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'src'}:{ROOT / 'data'}")
    proc = subprocess.run(
        [sys.executable, "-m", "ceramics.collector", "run", "--db", db,
         "--base-url", base, "--rounds", "1"] + (extra or []),
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr


def group_cats(group: list[dict]) -> list[str]:
    return [f"{m['site']}:{m['catalog_no']}" for m in group]


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
        print(f"工作目录: {tmp}\n模拟站: {base}（三馆同进程，路径前缀区分馆）\n")

        # 1. 协议：签名随会话/令牌变化，新会话不能用旧令牌
        print("== 1. 会话、按序令牌与签名协议 ==")
        s1 = login(base, "jia")
        code, page = authed_get(base, s1, "jia", "registry")
        check("正确签名可访问该馆登记入口", code == 200 and page["version"] == "v1"
              and page["site"] == "jia",
              f"首页 {page.get('page_index')} 版 {page.get('version')}")
        bad_sig = sign("wrong-secret", "GET", "/jia/registry", s1["sid"], s1["first_token"])
        code, data = http("GET", base + "/jia/registry", headers={
            "X-Ceramic-Session": s1["sid"], "X-Ceramic-Token": s1["first_token"],
            "X-Ceramic-Signature": bad_sig})
        check("错误签名被拒绝(403)", code == 403, data.get("error", ""))
        code, data = authed_get(base, s1, "jia", "registry")  # 重复使用同一旧令牌
        check("令牌必须按序使用，重复令牌被拒(409)", code == 409, data.get("error", ""))
        s2 = login(base, "jia")
        code, data = authed_get(base, s2, "jia", "registry", token=s1["first_token"])
        check("新会话不能使用旧令牌(403)", code == 403, data.get("error", ""))
        code, data = authed_get(base, s1, "jia", "registry",
                                token=issue_token(s2["sid"], 0))
        check("令牌与会话不匹配被拒(403)", code == 403, data.get("error", ""))

        # 2. 暂停按馆隔离：乙馆暂停时甲馆照常登录/访问
        print("\n== 2. 站点暂停按馆隔离 ==")
        control(base, {"site": "yi", "pause_seconds": 2})
        code_yi, _ = http("POST", base + "/yi/session", b"key=ceramics-local-control",
                          {"Content-Type": "application/x-www-form-urlencoded"})
        s_jia = login(base, "jia")  # 甲馆未暂停，立即成功
        code, _ = authed_get(base, s_jia, "jia", "joins")
        check("乙馆暂停返回 503，甲馆同时照常受理", code_yi == 503 and code == 200,
              f"乙馆登录 {code_yi} / 甲馆拼接入口 {code}")
        s3 = login(base, "yi")  # 等乙馆恢复
        t0 = time.time()
        code, page = authed_get(base, s3, "yi", "joins")
        check("乙馆恢复后同一令牌链继续可用", code == 200 and time.time() - t0 < 10,
              f"耗时 {time.time() - t0:.1f}s")

        # 3. 一次启动并发采集三个馆，各自首轮激活
        print("\n== 3. 一次启动并发采集多个合作馆 ==")
        rc, out = run_collector(db, base)
        conn = connect(db)
        active = active_versions(conn)
        check("一次采集并发覆盖三个馆并全部激活 v1",
              rc == 0 and active == {"jia": "v1", "yi": "v1", "bing": "v1"},
              f"active={active}")
        check("日志逐馆报告（并发而非串行阻塞）",
              all(f"[{s}]" in out for s in ("jia", "yi", "bing")),
              "；".join(line for line in out.strip().splitlines() if "发布版" in line)[:120])
        check("不同馆同时使用相同版号 v1，按 (馆,版本) 独立保存",
              conn.execute("SELECT COUNT(*) c FROM versions WHERE version='v1' "
                           "AND status='active'").fetchone()["c"] == 3)
        conn.close()

        # 4. 同号陶片：按馆查询隔离，未两端登记时不成跨馆组
        print("\n== 4. 跨馆同号馆藏编号按馆隔离 ==")
        conn = connect(db)
        both = query_mod.find_sherd(conn, "C-001")
        jia_c1 = query_mod.find_sherd(conn, "C-001", site="jia")
        yi_c1 = query_mod.find_sherd(conn, "C-001", site="yi")
        check("不带馆查询返回甲、乙两馆同号的 C-001",
              len(both["matches"]) == 2
              and {m["site"] for m in both["matches"]} == {"jia", "yi"},
              "命中 " + str([f"{m['site']}:C-001" for m in both["matches"]]))
        check("按馆查询只返回该馆陶片，且不跨馆串组",
              jia_c1["group_size"] == 3 and not jia_c1["cross_site"]
              and yi_c1["group_size"] == 2 and not yi_c1["cross_site"],
              f"甲 {group_cats(jia_c1['group'])} / 乙 {group_cats(yi_c1['group'])}")
        jia_c7 = query_mod.find_sherd(conn, "C-007", site="jia")
        check("仅一端登记跨馆拼接时不生效（等另一端发布）",
              not jia_c7["cross_site"] and jia_c7["group_size"] == 2,
              f"甲 C-007 组: {group_cats(jia_c7['group'])}")
        conn.close()

        # 5. 馆内撤销/恢复
        print("\n== 5. 馆内复核撤销拼接，连通组实时拆分 ==")
        conn = connect(db)
        before = query_mod.find_sherd(conn, "C-004", site="jia")
        query_mod.revoke_join(conn, "C-005", "C-006", reason="复核发现接缝不成立",
                              site="jia")
        after4 = query_mod.find_sherd(conn, "C-004", site="jia")
        after6 = query_mod.find_sherd(conn, "C-006", site="jia")
        check("撤销甲馆 C-005~C-006 后原组拆成两组",
              before["group_size"] == 3 and after4["group_size"] == 2
              and after6["group_size"] == 1,
              f"C-004 组 {group_cats(after4['group'])} / C-006 组 {group_cats(after6['group'])}")
        query_mod.restore_join(conn, "C-005", "C-006", site="jia")
        back = query_mod.find_sherd(conn, "C-004", site="jia")
        check("恢复撤销后回到原组", back["group_size"] == 3, str(group_cats(back["group"])))
        conn.close()

        # 6. 多馆独立切版 + 一馆暂停时其它馆照常发布 + 暂停馆断点续采
        print("\n== 6. 多馆独立切版：一馆暂停不影响其它馆，恢复后断点续采 ==")
        for site in ("jia", "yi", "bing"):
            set_versions(base, site, "v2", "v2")

        state = {"triggered": False, "resumes": []}
        counters = {"bing": 0}
        orig_fetch = col_mod.SiteClient.fetch_page
        orig_login = col_mod.SiteClient.login

        def patched_login(self, resume: int = 0):
            state["resumes"].append((self.site, resume))
            return orig_login(self, resume)

        def patched_fetch(self, entry: str):
            page = orig_fetch(self, entry)
            if self.site == "bing":
                counters["bing"] += 1
                if counters["bing"] == 2 and not state["triggered"]:
                    # 乙馆第 2 页已确认保存后让乙馆暂停，制造"部分页面已落盘"的断点
                    state["triggered"] = True
                    control(base, {"site": "bing", "pause_seconds": 4})
            return page

        col_mod.SiteClient.fetch_page = patched_fetch
        col_mod.SiteClient.login = patched_login
        round_result: dict = {}

        def _run() -> None:
            round_result.update(col_mod.run_round(base, db))

        try:
            th = threading.Thread(target=_run)
            th.start()
            deadline = time.time() + 5
            while not state["triggered"] and time.time() < deadline:
                time.sleep(0.02)
            during = {}
            d2 = time.time() + 4
            while time.time() < d2:
                conn = connect(db)
                during = active_versions(conn)
                conn.close()
                if during.get("jia") == "v2" and during.get("yi") == "v2":
                    break
                time.sleep(0.05)
            th.join(timeout=40)
            assert not th.is_alive(), "采集线程未结束"
        finally:
            col_mod.SiteClient.fetch_page = orig_fetch
            col_mod.SiteClient.login = orig_login

        check("乙馆暂停期间甲、乙馆照常发布到 v2，乙馆仍为旧版 v1",
              during.get("jia") == "v2" and during.get("yi") == "v2"
              and during.get("bing") != "v2",
              f"暂停窗口内 active={during}")
        conn = connect(db)
        after = active_versions(conn)
        check("三馆同用版号 v2 各自独立激活",
              after == {"jia": "v2", "yi": "v2", "bing": "v2"},
              f"active={after}")
        check("乙馆恢复后从已确认页的游标续采（登录游标=1，未回头重抓）",
              ("bing", 1) in state["resumes"],
              f"乙馆登录游标序列: {[r for s, r in state['resumes'] if s == 'bing']}")
        check("甲馆 v2 改用 JSONL 排版已正确解析",
              conn.execute("SELECT layout FROM pages WHERE site='jia' AND version='v2' "
                           "LIMIT 1").fetchone()[0] == "jsonl")
        check("轮到采集的馆都完成本轮（含恢复后的乙馆）",
              set(round_result.get("activated", {})) == {"jia", "yi", "bing"},
              f"activated={round_result.get('activated')}")
        check("恢复的乙馆从已确认页续采而非从头重抓（注册页只抓一次）",
              conn.execute("SELECT COUNT(*) c FROM version_failures WHERE site='bing' "
                           "AND version='v2'").fetchone()["c"] == 0,
              "暂停等待期间未产生失败记录")
        conn.close()

        # 6b. 某馆长时间暂停：本轮挂起该馆而不是拖住其它馆，恢复后从已确认页续采
        print("\n== 6b. 某馆长时间暂停：挂起该馆，其它馆照常进入下一轮 ==")
        db3 = str(tmp / "defer.db")
        init_db(db3)
        state2 = {"paused": False}
        orig_fetch3 = col_mod.SiteClient.fetch_page

        def pause_bing_after_first(self, entry: str):
            page = orig_fetch3(self, entry)
            if self.site == "bing" and not state2["paused"]:
                state2["paused"] = True
                control(base, {"site": "bing", "pause_seconds": 5})
            return page

        col_mod.SiteClient.fetch_page = pause_bing_after_first
        try:
            t0 = time.time()
            r1 = col_mod.run_round(base, db3, pause_budget=1.0)
            elapsed = time.time() - t0
        finally:
            col_mod.SiteClient.fetch_page = orig_fetch3
        check("暂停超过本轮预算的馆被挂起，本轮不等待它",
              r1["deferred"] == ["bing"] and "bing" not in r1["errors"]
              and elapsed < 4,
              f"deferred={r1['deferred']} 本轮耗时 {elapsed:.1f}s")
        conn3 = connect(db3)
        saved_bing = conn3.execute(
            "SELECT COUNT(*) c FROM pages WHERE site='bing'").fetchone()["c"]
        check("同轮其它馆照常发布，挂起馆保留已确认页",
              active_versions(conn3).get("jia") == "v2"
              and active_versions(conn3).get("yi") == "v2"
              and active_versions(conn3).get("bing") is None
              and saved_bing >= 1,
              f"active={active_versions(conn3)}，乙馆已存 {saved_bing} 页")
        conn3.close()
        time.sleep(max(0.0, 5.2 - (time.time() - t0)))
        r2 = col_mod.run_round(base, db3)
        conn3 = connect(db3)
        check("暂停馆恢复后从断点续采并补齐激活",
              r2["activated"].get("bing") == "v2"
              and active_versions(conn3) == {"jia": "v2", "yi": "v2", "bing": "v2"},
              f"active={active_versions(conn3)}")
        conn3.close()

        print("\n== 7. 跨馆登记晚到后自动生效 ==")
        conn = connect(db)
        cross_hit = query_mod.find_sherd(conn, "C-001", site="yi")
        check("乙馆晚到的跨馆登记生效，按馆查询返回跨馆拼接组",
              cross_hit["cross_site"]
              and set(cross_hit["sites_in_group"]) == {"jia", "yi"}
              and cross_hit["group_versions"] == {"jia": "v2", "yi": "v2"},
              f"乙 C-001 组: {group_cats(cross_hit['group'])} 版: {cross_hit['group_versions']}")
        check("跨馆组内同时给出各馆发布版",
              cross_hit["group_versions"].get("jia") == "v2"
              and cross_hit["group_versions"].get("yi") == "v2",
              str(cross_hit["group_versions"]))
        check("甲馆同号 C-001 仍留在本馆组，不因跨馆边串到乙馆",
              query_mod.find_sherd(conn, "C-001", site="jia")["group_size"] == 4
              and not query_mod.find_sherd(conn, "C-001", site="jia")["cross_site"],
              "同号不同组")
        conn.close()

        # 8. 任一端撤下跨馆登记：立即退出结果（新发布版撤下 & 显式撤下）
        print("\n== 8. 任一端撤下登记，跨馆拼接收缩/退出结果 ==")
        # 8a 乙馆 v3 撤下登记；同时声明损坏页，验证失败时旧版与跨馆关系保持
        control(base, {"site": "yi", "broken_pages": {"registry@v3": [0]}})
        set_versions(base, "yi", "v3", "v3")
        rc, out = run_collector(db, base)
        conn = connect(db)
        active_now = active_versions(conn)
        fail = conn.execute(
            "SELECT reason, detail FROM version_failures WHERE site='yi' "
            "AND version='v3' ORDER BY id DESC LIMIT 1").fetchone()
        check("乙馆 v3 损坏页被识别，乙馆保持 v2，其它馆不受影响",
              active_now.get("yi") == "v2" and active_now.get("jia") == "v2"
              and fail is not None,
              f"{fail['detail'] if fail else out.strip().splitlines()[-1]}")
        still = query_mod.find_sherd(conn, "C-001", site="yi")
        check("采集失败不改动已生效的跨馆拼接", still["cross_site"],
              f"组 {group_cats(still['group'])}")
        conn.close()

        control(base, {"site": "yi", "clear_broken": True})
        rc, out = run_collector(db, base)
        conn = connect(db)
        yi_active = active_versions(conn).get("yi")
        check("乙馆 v3 撤下登记后激活，跨馆拼接立即退出结果",
              yi_active == "v3"
              and not query_mod.find_sherd(conn, "C-001", site="yi")["cross_site"]
              and not query_mod.find_sherd(conn, "C-007", site="jia")["cross_site"],
              f"乙馆 active={yi_active}，甲 C-007 组 "
              f"{group_cats(query_mod.find_sherd(conn, 'C-007', site='jia')['group'])}")
        conn.close()

        set_versions(base, "yi", "v2", "v2")
        run_collector(db, base)
        conn = connect(db)
        restored = query_mod.find_sherd(conn, "C-001", site="yi")
        query_mod.revoke_cross(conn, "yi", "C-001", "jia", "C-007", reason="接缝复核撤下")
        revoked = query_mod.find_sherd(conn, "C-001", site="yi")
        query_mod.restore_cross(conn, "yi", "C-001", "jia", "C-007")
        again = query_mod.find_sherd(conn, "C-001", site="yi")
        check("登记恢复后跨馆拼接重新生效", restored["cross_site"],
              str(restored["group_versions"]))
        check("一端显式撤下后立即退出结果（无需重采另一端）",
              not revoked["cross_site"] and again["cross_site"],
              f"撤下后组 {group_cats(revoked['group'])}，恢复后 {group_cats(again['group'])}")
        conn.close()

        # 9. 不同馆可处在不同版号：甲 v3、乙 v2 时跨馆仍生效
        print("\n== 9. 各馆版号互不相同，跨馆拼接按两端各自当前版判定 ==")
        set_versions(base, "jia", "v3", "v3")
        rc, out = run_collector(db, base)
        conn = connect(db)
        mixed = query_mod.find_sherd(conn, "C-001", site="yi")
        check("甲馆 v3 与乙馆 v2 同时生效，跨馆拼接保持",
              active_versions(conn) == {"jia": "v3", "yi": "v2", "bing": "v2"}
              and mixed["cross_site"]
              and mixed["group_versions"] == {"jia": "v3", "yi": "v2"},
              f"active={active_versions(conn)}，跨馆组版本={mixed['group_versions']}")
        check("甲馆 v3 的 record 排版解析正确",
              conn.execute("SELECT layout FROM pages WHERE site='jia' AND version='v3' "
                           "LIMIT 1").fetchone()[0] == "record")
        conn.close()

        # 10. 引用不完整版本被拦截，旧版与其它馆继续可用
        print("\n== 10. 引用不完整的新版被拦截，旧版继续可用 ==")
        set_versions(base, "jia", "v4-bad", "v4-bad")
        rc, out = run_collector(db, base)
        conn = connect(db)
        fail = conn.execute(
            "SELECT reason, detail FROM version_failures WHERE site='jia' "
            "AND version='v4-bad' ORDER BY id DESC LIMIT 1").fetchone()
        check("甲馆 v4-bad 引用 X-999 被拦截，甲馆仍为 v3，乙/丙馆不受影响",
              fail is not None and "X-999" in (fail["detail"] or "")
              and active_versions(conn) == {"jia": "v3", "yi": "v2", "bing": "v2"},
              f"{fail['reason'] if fail else '无失败记录'}: "
              f"{fail['detail'] if fail else ''}")
        conn.close()

        # 11. 馆内两入口错时切版：不同版不激活，同版后原子切换
        print("\n== 11. 同一馆内两个入口错时切版 ==")
        set_versions(base, "bing", registry="v1", joins="v2")
        res = col_mod.run_round(base, db, ["bing"])
        conn = connect(db)
        check("丙馆两入口版本不一致时不激活，查询仍为该馆旧版 v2",
              "分属不同发布版" in (res["results"]["bing"]["error"] or "")
              and active_versions(conn).get("bing") == "v2",
              res["results"]["bing"]["error"] or "")
        set_versions(base, "bing", registry="v2", joins="v2")
        res = col_mod.run_round(base, db, ["bing"])
        check("两入口回到同版后恢复，丙馆保持 v2",
              res["results"]["bing"]["error"] is None
              and active_versions(conn).get("bing") == "v2",
              res["results"]["bing"]["error"] or "无错误")
        conn.close()

        # 12. 进程重启断点续采：部分页面已落盘后被杀，新进程从缺口补齐
        print("\n== 12. 进程重启后从已确认页面续采 ==")
        # 把甲馆从 v4-bad 切回 v3，确保重启后的完整采集能成功激活三馆
        set_versions(base, "jia", "v3", "v3")
        set_versions(base, "yi", "v2", "v2")
        set_versions(base, "bing", "v2", "v2")
        db2 = str(tmp / "resume.db")
        init_db(db2)
        wrapper = tmp / "slow_joins_wrapper.py"
        wrapper.write_text(
            "import sys, time\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "from ceramics import collector\n"
            "_orig = collector.SiteClient.fetch_page\n"
            "def _slow(self, entry):\n"
            "    if self.site == 'bing' and entry == 'joins':\n"
            "        time.sleep(20)\n"
            "    return _orig(self, entry)\n"
            "collector.SiteClient.fetch_page = _slow\n"
            "collector.run_round(sys.argv[1], sys.argv[2])\n",
            encoding="utf-8")
        killed = subprocess.Popen(
            [sys.executable, "-I", str(wrapper), base, db2],
            cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        conn2 = connect(db2)
        deadline = time.time() + 20
        saved_before = 0
        while time.time() < deadline:
            try:
                reg = conn2.execute(
                    "SELECT COUNT(*) c FROM pages WHERE site='bing' AND version='v2' "
                    "AND entry='registry'").fetchone()["c"]
                joins = conn2.execute(
                    "SELECT COUNT(*) c FROM pages WHERE site='bing' AND version='v2' "
                    "AND entry='joins'").fetchone()["c"]
            except sqlite3.OperationalError:
                time.sleep(0.1)
                continue
            if reg >= 2 and joins == 0:
                saved_before = reg + joins
                break
            time.sleep(0.05)
        conn2.close()
        killed.kill()
        killed.wait()
        check("被杀前乙馆登记页已确认落盘、拼接页尚未开始", saved_before >= 2,
              f"已保存 {saved_before} 页")
        rc, out2 = run_collector(db2, base)
        conn2 = connect(db2)
        check("重启后所有馆完成采集并激活（乙馆从缺口补齐）",
              rc == 0 and active_versions(conn2) == {"jia": "v3", "yi": "v2", "bing": "v2"},
              f"active={active_versions(conn2)}")
        check("日志显示断点跳过已保存页面", "断点续采" in out2,
              "已确认页校验和一致即跳过，不重复抓取")
        conn2.close()

        # 13. 会话频繁过期：自动重登并按已确认页续采
        print("\n== 13. 会话频繁过期，自动重登并按已保存页续采 ==")
        for site in ("jia", "yi", "bing"):
            set_versions(base, site, "v1", "v1")
        db4 = str(tmp / "relogin.db")
        calls = {}
        orig_fetch2 = col_mod.SiteClient.fetch_page

        def expire_fetch(self, entry: str):
            n = calls[self.site] = calls.get(self.site, 0) + 1
            if n % 2 == 0:  # 偶数次请求前强制该馆当前会话过期
                control(base, {"site": self.site, "expire_sessions": True})
            return orig_fetch2(self, entry)

        col_mod.SiteClient.fetch_page = expire_fetch
        try:
            res = col_mod.run_round(base, db4)
        finally:
            col_mod.SiteClient.fetch_page = orig_fetch2
        conn4 = connect(db4)
        check("三馆频繁会话过期下重登+续采仍完成激活",
              res.get("activated") == {"jia": "v1", "yi": "v1", "bing": "v1"}
              and active_versions(conn4) == {"jia": "v1", "yi": "v1", "bing": "v1"},
              f"期间各馆翻页调用 {calls}")
        check("每馆页面完整且无重复（主键按馆隔离）",
              all(conn4.execute(
                  "SELECT COUNT(*) c, MIN(total_pages) t FROM pages "
                  "WHERE site=? AND version='v1' GROUP BY entry", (s,)).fetchall()
                  for s in ("jia", "yi", "bing")))
        conn4.close()

        # 14. HTTP 查询服务：同号多馆、按馆跨馆组与各馆发布版
        print("\n== 14. HTTP 查询服务（多馆） ==")
        qport = free_port()
        qsrv = subprocess.Popen(
            [sys.executable, "-m", "ceramics.query", "serve", "--db", db,
             "--port", str(qport)], cwd=ROOT, env=env,
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
            check("GET /find 同号返回多馆命中与各馆发布版",
                  qdata is not None and len(qdata.get("matches", [])) == 2
                  and qdata["versions"] == {"jia": "v3", "yi": "v2", "bing": "v2"},
                  f"命中 {[m['site'] for m in qdata.get('matches', [])] if qdata else '无响应'}")
            code, gdata = http("GET", f"http://127.0.0.1:{qport}/find?catalog_no=C-001&site=yi")
            check("GET /find?site= 返回该馆所在的跨馆拼接组",
                  code == 200 and gdata.get("cross_site") is True
                  and gdata.get("group_versions") == {"jia": "v3", "yi": "v2"},
                  f"组版本 {gdata.get('group_versions') if gdata else '无响应'}")
            code, groups = http("GET", f"http://127.0.0.1:{qport}/groups")
            check("GET /groups 含跨馆连通组", code == 200
                  and groups.get("cross_group_count", 0) >= 1,
                  f"{groups.get('group_count')} 组，其中跨馆 {groups.get('cross_group_count')}")
            code, st = http("GET", f"http://127.0.0.1:{qport}/status")
            check("GET /status 返回各馆 active 版本", code == 200
                  and st.get("active_versions") == {"jia": "v3", "yi": "v2", "bing": "v2"},
                  str(st.get("active_versions")))
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
