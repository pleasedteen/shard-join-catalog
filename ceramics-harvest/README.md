# 陶片登记与拼接清单：采集与查询系统

修复中心汇总合作馆公开发布的**陶片登记清单**与**拼接清单**。本项目包含三个可独立运行的
组件，全部只用 Python 3 标准库，数据完全自带，无外部依赖：

| 组件 | 模块 | 作用 |
| --- | --- | --- |
| 本地模拟站 | `ceramics.site` | 模拟合作馆发布服务：会话、按序翻页令牌、签名、暂停、过期、损坏页、错时切版 |
| 采集服务 | `ceramics.collector` | 按协议采集两个入口，校验后按发布版原子激活，支持断点续采 |
| 查询服务 | `ceramics.query` | 按当前有效拼接关系返回陶片连通组，支持复核撤销/恢复，CLI 与 HTTP 两种入口 |

## 一条命令完成本地验证

```bash
./run.sh verify
```

约 20 秒（要求 80 秒内）输出 12 组共 38 项可读检查，全部通过退出码为 0。覆盖：签名与按序
令牌、暂停恢复、会话过期重登、首轮激活、撤销后连通组拆分、两入口错时切版、采集期间查询不
中断、损坏/引用不完整拦截且旧版继续可用、进程重启断点续采、HTTP 查询服务，以及大册专项：
**2 万 → 8 万（固定每页 500 条）峰值内存对比、单写事务 ≤ 两页（1000 条）校验、整版校验期
查询与撤销不中断、8 万级中断后重启复用已入库页面**。大册数据（`data/big_fixtures.py`）
仅在验证脚本以 `CERAMICS_BIG_LEDGER=1` 启动的模拟站进程中挂载，日常运行不加载。

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
三种页面排版），`v4-bad` 的拼接清单引用了登记中不存在的 `X-999`，用于演示引用完整性拦截；
`data/big_fixtures.py` 提供 2 万 / 8 万大册，固定每页 500 条，按需挂载。

SQLite 表（`src/ceramics/db.py`）：

- `pages`：已确认保存的原始页面，`(version, entry, page_index)` 唯一，带每页校验和与
  `record_count`（该页解析出的记录数）；
- `versions`：`collecting / complete / active / failed`，全局至多一个 `active`；
- `sherds` / `joins`：**采页时逐页解析、与对应 `pages` 行在同一事务写入**（每事务一页
  ≤500 条，绝不超过两页 1000 条），重启后凭校验和与 `record_count` 直接复用，不重新解析；
- `revocations`：复核撤销的拼接，按发布版隔离；
- `version_failures`：操作员可见的失败原因；`activation_log`：版本切换历史。

切换规则（激活只剩一个轻量元数据事务，任何一步失败旧 `active` 保持不变）：

1. 登记与拼接**必须同属一个发布版**；两入口版本不一致时不激活，等下一轮；
2. 该版两个入口**页数齐全、页序号连续、每页可解析、校验和与记录数一致**；
3. 登记馆藏编号唯一，**拼接清单引用的每个编号都能在该版登记中找到**（SQL 集合校验，
   不把整版拉进 Python 内存）；
4. 满足后把旧版降为 `complete`、新版置为 `active`。
   采集与校验期间查询始终命中旧 `active`，已激活版本不会因后续采集失败被降级。

### 内存与写事务边界

- 固定页大小 `parsing.PAGE_SIZE = 500`，采集与校验全程**流式**：同一时刻内存里至多一页
  正文与一页记录，整版从 2 万扩到 8 万（页数 4 倍）Python 峰值内存几乎不变，验证中实测
  比值约 1.01x（要求 ≤2x）；
- 单个采集写事务 = 1 行 `pages` + 同页业务行（≤500 条），护栏
  `MAX_BUSINESS_ROWS_PER_TXN = 1000`（两页）；整版扩大只增加事务数量，不放大单事务；
- 整版校验**全程只读**，WAL 模式下不阻塞查询，也不阻塞当前版撤销/恢复的写事务；
- 采集子进程设置 `CERAMICS_METRICS_FILE=路径` 时，退出会把峰值 RSS、事务规模等指标
  写成 JSON，供 `verify.py` 做资源对比。

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
- **进程重启**：原始页与该页业务记录在同一事务提交，落盘即持久；新进程探针确定当前
  版本后直接从缺口续采，已保存页校验和与记录数一致即整页跳过，不重复抓取、不重复写库。
- **损坏页**：站点可返回截断内容（校验和不符）或损坏标记，采集端识别后记录失败原因并放弃
  该版，查询继续使用旧版。

## 目录结构

```
ceramics-harvest/
├── run.sh                 # 本地入口脚本
├── verify.py              # 一键端到端验证（38 项检查，含大册资源与中断恢复）
├── data/fixtures.py       # 自带发布版数据集（v1~v4-bad）
├── data/big_fixtures.py   # 2 万 / 8 万大册（固定每页 500 条，按需挂载）
└── src/ceramics/
    ├── protocol.py        # 令牌、签名、校验和、边键
    ├── site.py            # 本地模拟站
    ├── collector.py       # 采集服务（逐页落库、流式校验、资源指标）
    ├── parsing.py         # pipe/jsonl/record 三种排版解析 + 固定页大小常量
    ├── db.py              # SQLite 建表、迁移与连接
    └── query.py           # 查询、连通分量、撤销/恢复、HTTP 服务
```
