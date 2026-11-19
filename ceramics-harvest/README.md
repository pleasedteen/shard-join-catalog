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

约 10 秒（要求 80 秒内）输出 10 组共 27 项可读检查，全部通过退出码为 0。覆盖：签名与按序
令牌、暂停恢复、会话过期重登、首轮激活、撤销后连通组拆分、两入口错时切版、采集期间查询不
中断、损坏/引用不完整拦截且旧版继续可用、进程重启断点续采、HTTP 查询服务。

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
三种页面排版），`v4-bad` 的拼接清单引用了登记中不存在的 `X-999`，用于演示引用完整性拦截。

SQLite 表（`src/ceramics/db.py`）：

- `pages`：已确认保存的原始页面，`(version, entry, page_index)` 唯一，带每页校验和；
- `versions`：`collecting / complete / active / failed`，全局至多一个 `active`；
- `sherds` / `joins`：激活时在单事务内从页面解析写入；
- `revocations`：复核撤销的拼接，按发布版隔离；
- `version_failures`：操作员可见的失败原因；`activation_log`：版本切换历史。

切换规则（全部在一个事务内完成，任何一步失败旧 `active` 保持不变）：

1. 登记与拼接**必须同属一个发布版**；两入口版本不一致时不激活，等下一轮；
2. 该版两个入口**页数齐全、页序号连续、每页可解析且校验通过**；
3. 登记馆藏编号唯一，**拼接清单引用的每个编号都能在该版登记中找到**；
4. 满足后写入数据并把旧版降为 `complete`、新版置为 `active`。
   采集与校验期间查询始终命中旧 `active`，已激活版本不会因后续采集失败被降级。

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
- **进程重启**：已确认页面在落盘时即提交事务；新进程探针确定当前版本后直接从缺口续采。
- **损坏页**：站点可返回截断内容（校验和不符）或损坏标记，采集端识别后记录失败原因并放弃
  该版，查询继续使用旧版。

## 目录结构

```
ceramics-harvest/
├── run.sh                 # 本地入口脚本
├── verify.py              # 一键端到端验证（27 项检查）
├── data/fixtures.py       # 自带发布版数据集
└── src/ceramics/
    ├── protocol.py        # 令牌、签名、校验和、边键
    ├── site.py            # 本地模拟站
    ├── collector.py       # 采集服务
    ├── parsing.py         # pipe/jsonl/record 三种排版解析
    ├── db.py              # SQLite 建表与连接
    └── query.py           # 查询、连通分量、撤销/恢复、HTTP 服务
```
