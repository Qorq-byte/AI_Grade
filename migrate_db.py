"""数据库迁移脚本 - 为 prompt_templates / knowledge_base / report_module_sequences 新增字段和表"""
from backend.database import engine
from sqlalchemy import text


MIGRATIONS = [
    # prompt_templates 新增细粒度字段
    ("prompt_templates.indicator_level", "ALTER TABLE prompt_templates ADD COLUMN indicator_level INT DEFAULT NULL"),
    ("prompt_templates.parent_indicator", "ALTER TABLE prompt_templates ADD COLUMN parent_indicator VARCHAR(200) DEFAULT NULL"),
    ("prompt_templates.dimension", "ALTER TABLE prompt_templates ADD COLUMN dimension VARCHAR(50) DEFAULT NULL"),

    # knowledge_base 新增 parent_indicator 字段
    ("knowledge_base.parent_indicator", "ALTER TABLE knowledge_base ADD COLUMN parent_indicator VARCHAR(200) DEFAULT NULL"),

    # 创建 report_module_sequences 表
    ("table:report_module_sequences", """
    CREATE TABLE IF NOT EXISTS report_module_sequences (
        id INT AUTO_INCREMENT PRIMARY KEY,
        step_number INT NOT NULL,
        module_key VARCHAR(100) NOT NULL,
        module_name VARCHAR(200) NOT NULL,
        sort_order INT DEFAULT 0,
        is_visible BOOLEAN DEFAULT TRUE,
        client_editable BOOLEAN DEFAULT TRUE,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_step_number (step_number)
    )
    """),
]


def run_migrations():
    """执行所有迁移"""
    with engine.connect() as conn:
        for name, sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"[OK] {name}")
            except Exception as e:
                # 列/表已存在则跳过
                err_msg = str(e).lower()
                if "duplicate column" in err_msg or "already exists" in err_msg or "duplicate key" in err_msg:
                    print(f"[SKIP] {name}: already exists")
                else:
                    print(f"[ERROR] {name}: {e}")
        print("\nAll migrations completed!")


if __name__ == "__main__":
    run_migrations()