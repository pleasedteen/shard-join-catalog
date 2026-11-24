# 陶片登记与拼接清单：采集与查询系统

修复中心汇总合作馆公开发布的**陶片登记清单**与**拼接清单**。本项目包含四个可独立运行、
独立重启的组件，全部只用 Python 3 标准库，数据完全自带，无外部依赖：

| 组件 | 模块 | 作用 |
| --- | --- | --- |
| 本地模拟站 | `ceramics.site` | 模拟合作馆发布服务：会话、按序翻页令牌、签名、暂停、过期、损坏页、错时切版 |
| 采集服务 | `ceramics.collector` | 按协议采集两个入口并落盘；同一轮确认同版收齐后只登记**收齐清单**，不再直接激活，断源自愈重试，支持断点续采 |
| 发布服务 | `ceramics.publisher` | **不访问站点**，只接手采集端登记的收齐清单，整版校验通过后在单事务内原子切换 active，可独立重启 |
| 查询服务 | `ceramics.query` | 按当前有效拼接关系返回陶片连通组，支持复核撤销/恢复，CLI 与 HTTP 两种入口 |

采集与发布通过 SQLite 中的 `ready_rounds` 收齐清单解耦：采集进程可以随时退出/重启，
发布进程也可以随时退出/重启，两端互不连站依赖；整版校验与切换期间采集端仍能写入
后续版本，查询始终命中旧 `active`，直到发布事务提交。

## 一条命令完成本地验证

```bash
./run.sh verify
```

约 17 秒（要求 80 秒内）输出 12 组共 47 项可读检查，全部通过退出码为 0。覆盖：签名与按序
令牌、暂停恢复、会话过期重登、采集/发布分离的首轮激活、撤销后连通组拆分、两入口错时切版、
采集期间查询不中断、损坏/引用不完整拦截且旧版继续可用、采集进程重启断点续采、HTTP 查询服务，
以及拆分架构的三项关键保证——**断源发布、采集端与发布端分别中断恢复、同一版只成功切换一次**
（含两个发布进程并发），另有整版校验期间采集写入后续版、查询读旧版的并发验证。

## 启动与使用

需要四个终端（或用 `&` 后台运行）。默认模拟站 `127.0.0.1:8080`、查询服务 `127.0.0.1:8090`、
数据库 `./ceramics.db`，均可用环境变量 `PORT_SITE` / `PORT_QUERY` / `DB` 覆盖。

```bash
# 1) 启动模拟站
./run.sh site

# 2) 采集（独立入口；只落盘 + 登记收齐清单，断源自动重试，可随时重启）
./run.sh collect-once          # 执行一轮采集
./run.sh collect               # 持续采集（可加 --rounds N --interval 秒）
./run.sh status                # 查看版本、已保存页、收齐清单、失败原因、撤销记录

# 3) 发布（独立入口；不连站点，只读本地收齐清单，可随时重启）
./run.sh publish-once          # 处理当前全部待发布清单后退出（可加 --version 版号）
./run.sh publish               # 持续轮询清单并原子切换（可加 --interval 秒）

# 4) 查询与复核（用法与拆分前完全一致）
./run.sh find C-001                 # 陶片 + 当前有效拼接连通组 + 数据发布版
./run.sh find C-001 --json
./run.sh groups                     # 当前发布版全部连通组
./run.sh revoke C-005 C-006 --reason "复核接缝不成立"
./run.sh find C-004                 # 撤销后原一组已拆成多组，按最新清单返回
./run.sh restore C-005 C-006        # 恢复撤销

# 5) HTTP 查询服务（可选）
./run.sh query-serve
#   GET  /find?catalog_no=C-001
#   GET  /groups
#   GET  /status
#   POST /revoke   {"a":"C-005","b":"C-006","reason":"..."}
#   POST /restore  {"a":"C-005","b":"C-006"}
```

### 采集与发布如何衔接

1. 采集端每轮抓取两个入口；只有两个入口在**同一轮**确认属于同一发布版、页数齐全且
   每页校验通过时，才向 `ready_rounds` 写一条 `pending` 收齐清单（含两入口页数快照）。
2. 两入口版本不一致时不登记清单，下一轮再看；采集端不做引用完整性等整版校验。
3. 发布端只处理 `ready_rounds` 中 `pending` 的清单：重新做完整整版校验，通过后在单个
   `BEGIN IMMEDIATE` 事务内写入数据并切换 `active`，清单置为 `switched`；校验失败则清单
   置为 `failed` 并在 `version_failures` 记录原因，旧 `active` 原样保留。
4. 同一版本只要在 `activation_log` 出现过就不会再次切换——发布进程中断重启、或两个发布
   进程并发，同一版都**只成功切换一次**。


## 模拟站控制接口（仅本地验证/演示用）

`POST /control`，请求头 `X-Control-Key: ceramics-local-control`，JSON 指令可组合：

```bash
curl -s -X POST http://127.0.0.1:8080/control \
  -H "X-Control-Key: ceramics-local-control" -H "Content-Type: application/json" \
  -d '{"entry_versions":{"registry":"v2"}}'          # 只切登记入口，制造错时切版
# 其它指令：
#   {"pause_seconds": 3}                 站点暂停受理，503 + Retry-After
#   {"resume": true}                     立即恢复
#   {"session_ttl": 1.0}                 调整会话有效期
#   {"expire_sessions": true}            立即过期全部会话
#   {"broken_pages":{"registry@v3":[0]}} 声明某入口某版本的损坏页
#   {"clear_broken": true}               清除损坏声明
```

## 数据模型与发布版规则

自带数据集见 `data/fixtures.py`：v1/v2/v3 为三个正常发布版（分别使用 pipe、jsonl、record
三种页面排版），`v4-bad` 的拼接清单引用了登记中不存在的 `X-999`，用于演示引用完整性拦截。

SQLite 表（`src/ceramics/db.py`）：

- `pages`：已确认保存的原始页面，`(version, entry, page_index)` 唯一，带每页校验和；
- `versions`：`collecting / complete / active / failed`，全局至多一个 `active`；
- `ready_rounds`：采集端在同一轮确认同版收齐后写入的收齐清单，状态
  `pending / switched / failed`，是采集与发布之间唯一的交接凭据；
- `sherds` / `joins`：发布切换时在单事务内从页面解析写入；
- `revocations`：复核撤销的拼接，按发布版隔离；
- `version_failures`：操作员可见的失败原因（采集/发布两个阶段都会写入）；
  `activation_log`：版本切换历史，同一版本在此表出现过即视为已发布、不可再切换。

发布切换规则（由独立的发布进程执行，全部在一个事务内完成，任何一步失败旧 `active`
保持不变）：

1. 发布端只接手 `ready_rounds` 中采集端在**同一轮**确认同版收齐的清单，不自行挑页面；
2. 该版两个入口**页数齐全、页序号连续、每页可解析且校验通过**（发布端重新整版校验）；
3. 登记馆藏编号唯一，**拼接清单引用的每个编号都能在该版登记中找到**；
4. 满足后写入数据并把旧版降为 `complete`、新版置为 `active`，清单置 `switched` 并写
   `activation_log`。整版校验与事务期间采集端可继续写入后续版页面与清单，查询始终
   命中旧 `active`（SQLite WAL 快照），提交后才看到新版；已激活版本不会因后续采集
   或发布失败被降级。

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
- **断源自愈**：无法连接站点时采集进程不退出，按 `--retry-interval`（默认 3s）重试整轮，
  已确认页面落盘不丢，站点恢复后自动断点续采；发布端不访问站点，断站不影响其发布本地清单。
- **进程重启**：已确认页面在落盘时即提交事务，收齐清单同步落盘；采集进程重启后从缺口续采，
  发布进程重启后继续处理 `pending` 清单，两端互不依赖。
- **损坏页**：站点可返回截断内容（校验和不符）或损坏标记，采集端识别后记录失败原因并放弃
  该版（不产生收齐清单）；即便清单已登记，发布端整版校验也会拦截，查询继续使用旧版。

## 目录结构

```
ceramics-harvest/
├── run.sh                 # 本地入口脚本（collect / publish 等独立入口）
├── verify.py              # 一键端到端验证（47 项检查）
├── data/fixtures.py       # 自带发布版数据集
└── src/ceramics/
    ├── protocol.py        # 令牌、签名、校验和、边键
    ├── site.py            # 本地模拟站
    ├── collector.py       # 采集服务：抓页落盘 + 登记 ready_rounds 收齐清单
    ├── publisher.py       # 发布服务：接手清单、整版校验、原子切换 active
    ├── parsing.py         # pipe/jsonl/record 三种排版解析
    ├── db.py              # SQLite 建表与连接（含 ready_rounds）
    └── query.py           # 查询、连通分量、撤销/恢复、HTTP 服务
```
