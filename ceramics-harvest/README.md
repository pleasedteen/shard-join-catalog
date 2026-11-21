# 陶片登记与拼接清单：多合作馆采集与查询系统

修复中心汇总多个合作馆公开发布的**陶片登记清单**与**拼接清单**。项目包含三个可独立
运行的组件，全部只用 Python 3 标准库，数据完全自带，无外部依赖：

| 组件 | 模块 | 作用 |
| --- | --- | --- |
| 本地模拟站 | `ceramics.site` | 一个进程模拟多个合作馆：每馆独立会话、按序翻页令牌、签名、暂停、过期、损坏页、错时切版 |
| 采集服务 | `ceramics.collector` | 一次启动并发采集多个馆；各馆独立校验、独立激活，支持暂停挂起与断点续采 |
| 查询服务 | `ceramics.query` | 按各馆当前发布版返回连通组（含跨馆拼接），支持复核撤销/恢复、跨馆撤下，CLI 与 HTTP |

## 多馆模型

- **馆相互独立**：`jia`（合作馆甲）、`yi`（合作馆乙）、`bing`（合作馆丙）各自有自己的
  版本序列、会话空间、暂停状态与激活事务。一家站点暂停时，其它馆照常发布；暂停馆恢复后
  从原断点继续。
- **同号、同版号**：不同馆可以同时使用 `v1/v2/v3`，也可以登记相同的馆藏编号（甲、乙都有
  `C-001`）。所有存储与游标都按 `(site, version)` / `(site, catalog_no)` 隔离，互不相干。
- **跨馆拼接**：在各自馆的拼接清单里用 `"馆名:馆藏编号"` 显式指向对方，例如甲馆登记
  `C-007 ~ yi:C-001`，乙馆登记 `C-001 ~ jia:C-007`。**两端都登记且都已发布时自动生效**；
  任一端在新版里不再登记（或显式撤下）时**立即退出结果**。晚到的一端补齐后无需重采另一端。
- **不要求跨馆同版**：甲馆 `v3` 与乙馆 `v2` 同时生效时，跨馆拼接照样按两端各自当前版判定。

## 一条命令完成本地验证

```bash
./run.sh verify
```

约 13 秒（要求 80 秒内）输出 14 组共 46 项可读检查，全部通过退出码为 0。覆盖：

1. 会话、按序令牌与签名协议；
2. 暂停按馆隔离（乙馆 503 时甲馆照常受理）；
3. 一次启动并发采集三个馆并各自激活；
4. 跨馆同号馆藏编号按馆隔离、单端登记不生效；
5. 馆内撤销/恢复拼接；
6. 多馆独立切版、一馆暂停时其它馆照常发布、暂停馆按游标续采，以及长时间暂停的挂起与恢复；
7. 跨馆登记晚到后自动生效，按馆查询返回跨馆拼接组及各馆发布版；
8. 任一端撤下跨馆登记（新版不再登记 / 显式撤下）立即退出结果；
9. 各馆版号互不相同时跨馆拼接保持；
10. 引用不完整版本被拦截且其它馆不受影响；
11. 同一馆内两入口错时切版；
12. 进程重启后从已确认页面续采；
13. 会话频繁过期时自动重登续采；
14. HTTP 查询服务（同号多馆、按馆跨馆组、各馆版本）。

## 启动与使用

需要三个终端（或用 `&` 后台运行）。默认模拟站 `127.0.0.1:8080`、查询服务 `127.0.0.1:8090`、
数据库 `./ceramics.db`，均可用环境变量 `PORT_SITE` / `PORT_QUERY` / `DB` 覆盖。
`SITES=jia,yi` 可限定采集的馆，留空自动发现。

```bash
# 1) 启动模拟站（三馆同进程，路径前缀 /jia /yi /bing 区分馆）
./run.sh site

# 2) 采集（一次启动并发采集全部馆；持续轮询用 ./run.sh collect）
./run.sh collect-once
./run.sh collect --sites jia,yi        # 只采部分馆
./run.sh status                        # 各馆版本、已保存页、失败原因、撤销与跨馆撤下

# 3) 查询与复核
./run.sh find C-001                    # 不带馆：返回各馆同号命中与各自连通组
./run.sh find C-001 --site yi          # 按馆查询：该馆所在（可跨馆）连通组
./run.sh find C-001 --json
./run.sh groups                        # 全部连通组（含跨馆组）
./run.sh revoke C-005 C-006 --site jia --reason "复核接缝不成立"
./run.sh restore C-005 C-006 --site jia
./run.sh revoke-cross --site yi C-001 jia C-007 --reason "接缝复核撤下"
./run.sh restore-cross --site yi C-001 jia C-007

# 4) HTTP 查询服务（可选）
./run.sh query-serve
#   GET  /find?catalog_no=C-001[&site=yi]
#   GET  /groups[?site=yi]
#   GET  /status
#   POST /revoke        {"a":"C-005","b":"C-006","site":"jia","reason":"..."}
#   POST /restore       {"a":"C-005","b":"C-006","site":"jia"}
#   POST /revoke-cross  {"site":"yi","a":"C-001","site_b":"jia","b":"C-007","reason":"..."}
#   POST /restore-cross {"site":"yi","a":"C-001","site_b":"jia","b":"C-007"}
```

## 模拟站控制接口（仅本地验证/演示用）

`POST /control`，请求头 `X-Control-Key: ceramics-local-control`。指令可带 `"site": "jia"`
只作用于该馆，不带则作用于全部馆：

```bash
curl -s -X POST http://127.0.0.1:8080/control \
  -H "X-Control-Key: ceramics-local-control" -H "Content-Type: application/json" \
  -d '{"site":"jia","entry_versions":{"registry":"v2"}}'  # 只切该馆登记入口，制造错时切版
# 其它指令：
#   {"site":"yi","pause_seconds":3}      该馆暂停受理，503 + Retry-After（其它馆不受影响）
#   {"site":"yi","resume":true}          立即恢复该馆
#   {"site":"yi","session_ttl":1.0}      调整该馆会话有效期
#   {"site":"yi","expire_sessions":true} 立即过期该馆全部会话
#   {"site":"jia","broken_pages":{"registry@v3":[0]}}  声明该馆某入口某版本的损坏页
#   {"site":"jia","clear_broken":true}   清除该馆损坏声明
# GET /sites                             列出全部合作馆
```

## 数据模型与发布版规则

自带数据集见 `data/fixtures.py`：三馆各自拥有多个正常发布版与一个引用不完整的
`v4-bad`（甲馆，拼接引用 `X-999`）；甲馆 `v1` 起登记跨馆拼接一端，乙馆 `v2` 才登记另一端
（晚到），乙馆 `v3` 又撤下该登记。

SQLite 表（`src/ceramics/db.py`，主键均含 `site`）：

- `pages`：已确认保存的原始页面，`(site, version, entry, page_index)` 唯一，带每页校验和；
- `versions`：`(site, version)` 为主键，状态 `collecting / complete / active / failed`，每馆至多一个 `active`；
- `sherds` / `joins`：激活时在单事务内从该馆页面解析写入；
- `revocations`：馆内复核撤销，按 `(site, version)` 隔离；
- `cross_revocations`：跨馆拼接的显式撤下，按 `(edge_key, side)` 隔离；
- `version_failures`：操作员可见的失败原因；`activation_log`：各馆版本切换历史。

切换规则（每馆一个事务，任何一步失败该馆旧 `active` 保持不变，其它馆完全无感）：

1. 该馆登记与拼接**必须同属一个发布版**；两入口版本不一致时不激活，等下一轮；
2. 该版两个入口**页数齐全、页序号连续、每页可解析且校验通过**；
3. 登记馆藏编号在馆内唯一，**拼接清单引用的本馆编号都能在该版登记中找到**
   （跨馆引用不要求对方此刻已发布，由查询端在两端都发布时才生效）；
4. 满足后写入数据并把该馆旧版降为 `complete`、新版置为 `active`。
   采集与校验期间查询始终命中各馆旧 `active`，已激活版本不会因后续采集失败被降级。

## 采集协议与可靠性设计

- **会话与签名**：`POST /<site>/session` 返回 `sid`、一次性 `secret` 与首令牌；每个请求带
  `X-Ceramic-Session / X-Ceramic-Token / X-Ceramic-Signature`，签名为
  `HMAC-SHA256(secret, "METHOD 路径\n会话头\n令牌头")`，路径含馆前缀。
- **按序令牌**：站点按访问顺序为每个会话发放令牌，令牌编码会话与游标；重复、乱序、跨会话
  使用分别返回 409/403。**新会话不复用旧令牌**，而是登录时带续采游标，由站点换发新令牌链。
- **暂停 503**：按 `Retry-After` 等待，同一令牌重试（暂停不消耗游标）。单馆一轮等待最多
  `--pause-budget` 秒（默认 20），超过则**挂起该馆到下一轮**，不拖住其它馆；已确认页全部保留。
- **会话过期 401 / 签名或令牌被拒 403**：重新登录；按该馆该版本**已确认保存的页数**作为
  游标续采，已保存页校验和一致即跳过，不重复抓取。
- **进程重启**：已确认页面在落盘时即提交事务；新进程探针确定当前版本后直接从缺口续采。
- **并发隔离**：每馆一个线程、一个独立 SQLite 连接；单馆异常被捕获并只记在该馆名下。
- **损坏页**：站点可返回截断内容（校验和不符）或损坏标记，采集端识别后记录失败原因并放弃
  该版，查询继续使用该馆旧版。

## 目录结构

```
ceramics-harvest/
├── run.sh                 # 本地入口脚本
├── verify.py              # 一键多馆端到端验证（46 项检查）
├── data/fixtures.py       # 三馆各自的数据集与跨馆登记
└── src/ceramics/
    ├── protocol.py        # 令牌、签名、校验和、馆内/跨馆边键
    ├── site.py            # 多馆本地模拟站
    ├── collector.py       # 多馆并发采集服务
    ├── parsing.py         # pipe/jsonl/record 排版与跨馆端点解析
    ├── cross.py           # 跨馆登记实时配对与有效连通图
    ├── db.py              # SQLite 建表、连接、旧单馆库迁移
    └── query.py           # 查询、连通分量、撤销/恢复、跨馆撤下、HTTP 服务
```
