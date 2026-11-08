#!/bin/bash
set -e

echo "等待 MySQL 就绪..."
until python - <<'PY' 2>/dev/null
import os
import pymysql

connection = pymysql.connect(
    host=os.environ.get("MYSQL_HOST", "mysql"),
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    user=os.environ.get("MYSQL_USER", "root"),
    password=os.environ.get("MYSQL_PASSWORD", ""),
    database=os.environ.get("MYSQL_DATABASE", "ai_grade_db"),
    connect_timeout=3,
)
connection.close()
PY
do
    echo "  尚不可用，2秒后重试..."
    sleep 2
done

echo "等待 Redis 就绪..."
# 应用当前未使用 Redis（进度与增量走进程内总线），只有显式配置 REDIS_URL 时才等待，
# 避免无 Redis 的部署被无意义的健康检查阻塞。
if [ -n "${REDIS_URL}" ]; then
    until python - <<'PY' 2>/dev/null
import os
import redis

client = redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3)
client.ping()
PY
    do
        echo "  尚不可用，2秒后重试..."
        sleep 2
    done
    echo "  Redis 已就绪"
else
    echo "  未配置 REDIS_URL，跳过（当前为单进程内进度总线）"
fi

echo "所有依赖服务已就绪，启动应用..."
exec "$@"
