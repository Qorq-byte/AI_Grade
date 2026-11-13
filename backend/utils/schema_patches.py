"""轻量 schema 补丁（批次 24）

项目没有引入 Alembic，建表靠 `Base.metadata.create_all()` —— 它只建**不存在的表**，
不会给已存在的表加列。线上 `users` 表已经跑起来了，光在模型里加字段不生效，
查询会直接 `Unknown column`。

这里做一个幂等补丁：先查 information_schema 再决定是否 ALTER，
重复执行无副作用（不要依赖 `ADD COLUMN IF NOT EXISTS`，MySQL 8 不支持这个语法）。
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import Engine


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
        ),
        {"t": table, "c": column},
    ).scalar()
    return bool(row)


def ensure_column(conn, table: str, column: str, ddl: str) -> bool:
    """列不存在则 ALTER TABLE 加上。返回是否真的执行了 ALTER。"""
    if _column_exists(conn, table, column):
        return False
    conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN {ddl}"))
    logger.info(f"schema 补丁：{table}.{column} 已新增")
    return True


#: 每项 (表, 列, 列定义)。列定义里的 DEFAULT 不能省 —— 老行要有确定值。
COLUMN_PATCHES = [
    ("users", "must_change_password", "`must_change_password` TINYINT(1) NOT NULL DEFAULT 0"),
    # 改密后让旧 JWT 立即失效（批次 25）。老行默认 0，正好等于老 token 里缺省的 ver
    ("users", "token_version", "`token_version` INT NOT NULL DEFAULT 0"),
]


def apply_schema_patches(engine: Engine) -> list[str]:
    """执行所有列补丁，返回本次实际新增的 "表.列" 列表。"""
    applied: list[str] = []
    with engine.begin() as conn:
        for table, column, ddl in COLUMN_PATCHES:
            try:
                if ensure_column(conn, table, column, ddl):
                    applied.append(f"{table}.{column}")
            except Exception as exc:
                # 单个补丁失败不该拦住启动；但必须留痕，否则后面会报 Unknown column 更难查
                logger.error(f"schema 补丁失败 {table}.{column}: {exc}")
    if applied:
        logger.info(f"schema 补丁完成：{', '.join(applied)}")
    return applied
