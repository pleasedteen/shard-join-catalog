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

case "${1:-}" in
  verify)
    exec "$PY" verify.py
    ;;
  site)
    exec "$PY" -m ceramics.site --port "$PORT_SITE"
    ;;
  collect)
    shift || true
    # 默认持续轮询；可用 --rounds N 覆盖为固定轮数
    exec "$PY" -m ceramics.collector run --db "$DB" \
      --base-url "http://127.0.0.1:$PORT_SITE" --rounds 0 "$@"
    ;;
  collect-once)
    exec "$PY" -m ceramics.collector run --db "$DB" \
      --base-url "http://127.0.0.1:$PORT_SITE" --rounds 1
    ;;
  status)
    exec "$PY" -m ceramics.collector status --db "$DB"
    ;;
  find)
    shift
    exec "$PY" -m ceramics.query find --db "$DB" "$@"
    ;;
  groups)
    exec "$PY" -m ceramics.query groups --db "$DB"
    ;;
  revoke)
    shift
    exec "$PY" -m ceramics.query revoke --db "$DB" "$@"
    ;;
  restore)
    shift
    exec "$PY" -m ceramics.query restore --db "$DB" "$@"
    ;;
  query-serve)
    exec "$PY" -m ceramics.query serve --db "$DB" --port "$PORT_QUERY"
    ;;
  *)
    cat <<EOF
用法: ./run.sh <命令>

  verify        一键本地验证（80 秒内输出全部场景结果）
  site          启动模拟站        :$PORT_SITE
  collect-once  执行一轮采集并按规则激活版本
  collect       持续采集（可加 --rounds N --interval 秒）
  status        查看版本/页面/失败原因/撤销记录
  find <编号>   查询陶片及其当前有效拼接连通组（可加 --json）
  groups        列出当前发布版全部连通组
  revoke <A> <B> [--reason 文本]   复核撤销一条拼接
  restore <A> <B>                  恢复一条撤销
  query-serve   启动 HTTP 查询服务 :$PORT_QUERY

数据库: $DB （可用 DB=路径 ./run.sh ... 覆盖）
EOF
    ;;
esac
