#!/bin/bash
set -e
# ═══════════════════════════════════
#  祺orQ — Docker 入口脚本
#  自动等待 MySQL + Redis 就绪后再启动应用
# ═══════════════════════════════════

echo "等待 MySQL 就绪 (host: ${MYSQL_HOST:-mysql}:${MYSQL_PORT:-3306})..."

until python -c "
import pymysql
try:
    conn = pymysql.connect(
        host='${MYSQL_HOST:-mysql}',
        port=int('${MYSQL_PORT:-3306}'),
        user='${MYSQL_USER:-root}',
        password='${MYSQL_PASSWORD:-}',
        database='${MYSQL_DATABASE:-ai_grade_db}'
    )
    conn.close()
    print('MySQL 连接成功')
except Exception as e:
    print(f'MySQL 连接失败: {e}')
    exit(1)
" 2>/dev/null; do
    echo "  尚不可用，2秒后重试..."
    sleep 2
done

echo "等待 Redis 就绪 (${REDIS_URL:-redis://redis:6379/0})..."

until python -c "
import redis
try:
    r = redis.from_url('${REDIS_URL:-redis://redis:6379/0}')
    r.ping()
    print('Redis 连接成功')
except Exception as e:
    print(f'Redis 连接失败: {e}')
    exit(1)
" 2>/dev/null; do
    echo "  尚不可用，2秒后重试..."
    sleep 2
done

echo "所有依赖服务已就绪，启动应用..."
exec "$@"
