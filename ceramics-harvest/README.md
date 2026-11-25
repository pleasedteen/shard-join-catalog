# 陶片登记与拼接清单：采集与查询系统

修复中心汇总合作馆公开发布的**陶片登记清单**与**拼接清单**。本项目包含三个可独立运行的
组件，全部只用 Python 3 标准库，数据完全自带，无外部依赖：

| 组件 | 模块 | 作用 |
| --- | --- | --- |
| 本地模拟站 | `ceramics.site` | 模拟合作馆发布服务：会话、按序翻页令牌、签名、暂停、过期、损坏页、错时切版；既发整版也发只含增改删的更正单 |
| 采集服务 | `ceramics.collector` | 按协议采集两个入口，校验后按发布版原子激活，整版全量落库、更正单按目标版落盘并登记谱系，支持断点续采 |
| 查询服务 | `ceramics.query` | 按当前有效拼接关系返回陶片连通组，整版直读、更正版沿谱系本地物化解说，支持复核撤销/恢复，CLI 与 HTTP 两种入口 |

## 一条命令完成本地验证

```bash
./run.sh verify
```

约 14 秒（要求 80 秒内）输出 11 组共 52 项可读检查，全部通过退出码为 0。覆盖：签名与按序
令牌、暂停恢复、会话过期重登、首轮激活、撤销后连通组拆分、两入口错时切版、采集期间查询不
中断、损坏/引用不完整拦截且旧版继续可用、进程重启断点续采、HTTP 查询服务，以及**更正单**
场景：新增/改写/删除三类更正、漏过中间整版后从本地整版+更正页补齐、重启后续用已确认更正
页、链式小更正只传更正页、问题更正单（引用缺失/基版缺失）拦截。

## 启动与使用

需要三个终端（或用 `&` 后台运行）。默认模拟站 `127.0.0.1:8080`、查询服务 `127.0.0.1:8090`、
数据库 `./ceramics.db`，均可用环境变量 `PORT_SITE` / `PORT_QUERY` / `DB` 覆盖。

```bash
# 1) 启动模拟站
./run.sh site

# 2) 采集（先执行一轮；持续轮询用 ./run.sh collect）
./run.sh collect-once
./run.sh status            # 查看版本、已保存页、失败原因、撤销记录

# 3) 查询与复核
./run.sh find C-001                 # 陶片 + 当前有效拼接连通组 + 数据发布版
./run.sh find C-001 --json
./run.sh groups                     # 当前发布版全部连通组
./run.sh revoke C-005 C-006 --reason "复核接缝不成立"
./run.sh find C-004                 # 撤销后原一组已拆成多组，按最新清单返回
./run.sh restore C-005 C-006        # 恢复撤销

# 4) HTTP 查询服务（可选）
./run.sh query-serve
#   GET  /find?catalog_no=C-001
#   GET  /groups
#   GET  /status
#   POST /revoke   {"a":"C-005","b":"C-006","reason":"..."}
#   POST /restore  {"a":"C-005","b":"C-006"}
```

## 模拟站控制接口（仅本地验证/演示用）

`POST /control`，请求头 `X-Control-Key: ceramics-local-control`，JSON 指令可组合：

```bash
curl -s -X POST http://127.0.0.1:8080/control \
  -H "X-Control-Key: ceramics-local-control" -H "Content-Type: application/json" \
  -d '{"entry_versions":{"registry":"v2"}}'          # 只切登记入口，制造错时切版
# 入口版本号可以是整版（v1..v5），也可以是更正单（c1/c2 等，kind=correction）；
# 更正单只发变化记录，站点不再重发整版。
# 其它指令：
#   {"pause_seconds": 3}                 站点暂停受理，503 + Retry-After
#   {"resume": true}                     立即恢复
#   {"session_ttl": 1.0}                 调整会话有效期
#   {"expire_sessions": true}            立即过期全部会话
#   {"broken_pages":{"registry@v3":[0]}} 声明某入口某版本的损坏页（@后为入口当前版本号）
#   {"clear_broken": true}               清除损坏声明
```

## 数据模型、更正单与发布版规则

自带数据集见 `data/fixtures.py`：v1/v2/v3/v5 为四个正常**整版**（分别使用 pipe、jsonl、
record、pipe 四种页面排版），`v4-bad` 的拼接清单引用了登记中不存在的 `X-999`，用于演示
引用完整性拦截。`c1`、`c2` 是两张**更正单**（v5→v6、v6→v7），`c-bad-ref` 与 `c-orphan`
是两张问题更正单（引用缺失陶片、基版本地不存在）。

更正单只携带 `add` / `upsert` / `delete` 三种操作：

- 登记更正按 `catalog_no` 定位：`add` 要求编号不存在、`upsert` 要求已存在并整体改写、
  `delete` 只需编号；
- 拼接更正按无向边 `a/b` 定位，语义同上，`delete` 只需 `a/b`；
- 每张更正单声明 `base_version`（基版）与 `target_version`（目标版），站点页面带
  `kind=correction` 标识，采集端按目标版编号把更正页落盘。

SQLite 表（`src/ceramics/db.py`）：

- `pages`：已确认保存的原始页面，`(version, entry, page_index)` 唯一，带每页校验和；
  整版存整版页，更正单存更正页（`kind='correction'`，并记录 `base_version/target_version`）；
- `versions`：`collecting / complete / active / failed`，全局至多一个 `active`，带
  `kind/base_version` 区分整版与更正单；
- `edition_lineage`：更正谱系 `目标版 -> 基版`，激活更正单时在单事务内登记；
- `sherds` / `joins`：**只保存整版**激活时解析写入的全量数据；更正版不复制全量，
  查询时由 `ceramics.materialize` 沿谱系从本地整版与已确认更正页实时物化；
- `revocations`：复核撤销的拼接，按发布版隔离（更正版同样按目标版编号隔离）；
- `version_failures`：操作员可见的失败原因；`activation_log`：版本切换历史。

切换规则（全部在一个事务内完成，任何一步失败旧 `active` 保持不变）：

1. 登记与拼接**必须到达同一目标版**：两入口版本不一致、整版/更正单形态不一致或更正单
   基版不一致时不激活，等下一轮；
2. 该目标版两个入口**页数齐全、页序号连续、每页可解析且校验通过**（更正单允许某入口
   0 条操作即 0 页）；
3. 整版要求登记馆藏编号唯一、拼接引用的编号都在登记中；更正单先按谱系试算物化，再校验
   操作合法性（add 不重复、upsert/delete 目标存在）与物化后的引用完整性，且基版整版
   必须本地存在，否则拦截；
4. 整版写入全量 `sherds/joins` 后置 `active`；更正单只登记 `edition_lineage` 即置
   `active`。采集、补采与校验期间查询始终命中旧 `active`，已激活版本不会因后续采集失败
   被降级。

### 漏版补齐与更正单的规模保证

- 合作馆在整版之间只发更正单：采集端错过中间整版也无需重发，只要本地有谱系终点的整版
  与链上各目标版的已确认更正页，`materialize` 就沿 `edition_lineage` 从基版整版开始
  逐版套用更正，在本地还原目标版视图（v5 整版 → c1 → c2 可直接物化为 v7）。
- 进程重启后已确认的更正页与整版页一样在落盘时即提交，新进程按已保存页数续采，
  已保存页校验和一致即跳过。
- 因为更正版不复制全量数据，**传输页数与落库记录数只随更正规模增长**：演示中 v6 只传
  4 张更正页、v7 只传 2 张，`sherds/joins` 中没有任何 v6/v7 行。

## 采集协议与可靠性设计

- **会话与签名**：`POST /session` 返回 `sid`、一次性 `secret` 与首令牌；每个请求带
  `X-Ceramic-Session / X-Ceramic-Token / X-Ceramic-Signature`，签名为
  `HMAC-SHA256(secret, "METHOD 路径\n会话头\n令牌头")`，会话或令牌变化签名即变。
- **按序令牌**：站点按访问顺序为每个会话发放令牌，令牌编码会话与游标；重复、乱序、跨会话
  使用分别返回 409/403。**新会话不复用旧令牌**，而是登录时带续采游标，由站点为新会话换发
  从该游标开始的全新令牌链。
- **暂停 503**：按 `Retry-After` 等待，同一令牌重试（暂停不消耗游标）。
- **会话过期 401 / 签名或令牌被拒 403**：重新登录；采集端按该版本**已确认保存的页数**作为
  游标续采，已保存页校验和一致即跳过，不重复抓取。
- **进程重启**：已确认页面（整版页与更正页）在落盘时即提交事务；新进程探针确定当前
  版本后直接从缺口续采。
- **损坏页**：站点可返回截断内容（校验和不符）或损坏标记，采集端识别后记录失败原因并放弃
  该版，查询继续使用旧版。
- **更正单续采**：更正页按目标版编号落盘，续采、重登、校验和跳过逻辑与整版完全一致；
  激活不依赖站点再次发布，重启后仅凭本地已确认更正页与基版整版即可继续提供目标版查询。

## 目录结构

```
ceramics-harvest/
├── run.sh                 # 本地入口脚本
├── verify.py              # 一键端到端验证（52 项检查）
├── data/fixtures.py       # 自带整版、更正单与问题版本数据集
└── src/ceramics/
    ├── protocol.py        # 令牌、签名、校验和、边键
    ├── site.py            # 本地模拟站（整版 + 更正单发布）
    ├── collector.py       # 采集服务（断点续采、整版/更正单激活）
    ├── parsing.py         # pipe/jsonl/record 三种排版与更正记录解析
    ├── materialize.py     # 沿更正谱系从本地整版+更正页物化目标版
    ├── db.py              # SQLite 建表、平滑迁移与连接
    └── query.py           # 查询、连通分量、撤销/恢复、HTTP 服务
```
