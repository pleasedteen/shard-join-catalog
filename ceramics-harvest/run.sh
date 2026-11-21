#!/usr/bin/env bash
# 陶片采集与拼接查询项目 —— 本地入口脚本（仅依赖 Python 3 标准库）
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$PWD/src:$PWD/data${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1   # 保持项目目录干净，不生成 __pycache__
PY="${PYTHON:-python3}"
DB="${DB:-$PWD/ceramics.db}"
PORT_SITE="${PORT_SITE:-8080}"
PORT_QUERY="${PORT_QUERY:-8090}"
SITES="${SITES:-}"                 # 逗号分隔；留空自动发现全部合作馆

case "${1:-}" in
  verify)
    exec "$PY" verify.py
    ;;
  site)
    exec "$PY" -m ceramics.site --port "$PORT_SITE"
    ;;
  collect)
    shift || true
    # 默认持续轮询全部合作馆（并发）；可用 --rounds N 覆盖为固定轮数
    exec "$PY" -m ceramics.collector run --db "$DB" \
      --base-url "http://127.0.0.1:$PORT_SITE" --sites "$SITES" --rounds 0 "$@"
    ;;
  collect-once)
    shift || true
    exec "$PY" -m ceramics.collector run --db "$DB" \
      --base-url "http://127.0.0.1:$PORT_SITE" --sites "$SITES" --rounds 1 "$@"
    ;;
  status)
    shift || true
    exec "$PY" -m ceramics.collector status --db "$DB" "$@"
    ;;
  find)
    shift
    exec "$PY" -m ceramics.query find --db "$DB" "$@"
    ;;
  groups)
    shift || true
    exec "$PY" -m ceramics.query groups --db "$DB" "$@"
    ;;
  revoke)
    shift
    exec "$PY" -m ceramics.query revoke --db "$DB" "$@"
    ;;
  restore)
    shift
    exec "$PY" -m ceramics.query restore --db "$DB" "$@"
    ;;
  revoke-cross)
    shift
    exec "$PY" -m ceramics.query revoke-cross --db "$DB" "$@"
    ;;
  restore-cross)
    shift
    exec "$PY" -m ceramics.query restore-cross --db "$DB" "$@"
    ;;
  query-serve)
    exec "$PY" -m ceramics.query serve --db "$DB" --port "$PORT_QUERY"
    ;;
  *)
    cat <<EOF
用法: ./run.sh <命令>

  verify        一键多馆验证（80 秒内输出全部场景结果）
  site          启动模拟站（三馆同进程）  :$PORT_SITE
  collect-once  并发采集一轮，各馆独立按规则激活版本
  collect       持续采集（可加 --rounds N --interval 秒 --sites 甲,乙）
  status        查看各馆版本/页面/失败原因/撤销与跨馆撤下记录
  find <编号> [--site 馆]   查询陶片及拼接连通组（含跨馆），--json 输出
  groups [--site 馆]        列出连通组（含跨馆组）
  revoke <A> <B> [--site 馆] [--reason 文本]   复核撤销一条馆内拼接
  restore <A> <B> [--site 馆]                  恢复一条撤销
  revoke-cross --site 馆 <A> <对方馆> <B> [--reason 文本]  撤下一条跨馆拼接
  restore-cross --site 馆 <A> <对方馆> <B>                 恢复跨馆拼接
  query-serve   启动 HTTP 查询服务 :$PORT_QUERY

馆名: jia(合作馆甲) yi(合作馆乙) bing(合作馆丙)
数据库: $DB （可用 DB=路径 ./run.sh ... 覆盖；可用 SITES=jia,yi ... 限定馆）
EOF
    ;;
esac
