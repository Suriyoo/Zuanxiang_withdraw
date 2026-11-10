import os
import sqlite3

DB_NAME = os.getenv("DB_NAME", "database.db")


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                diamonds INTEGER NOT NULL DEFAULT 0 CHECK(diamonds >= 0),
                is_admin INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                diamonds INTEGER NOT NULL,
                claimed_at TEXT NOT NULL,
                external_id TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # 兼容旧数据库：为已有的提取记录表补充 external_id 字段。
        # 采用可重复执行的检查，避免每次启动都重复 ALTER TABLE。
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(claim_records)").fetchall()
        }
        if "external_id" not in columns:
            conn.execute(
                "ALTER TABLE claim_records "
                "ADD COLUMN external_id TEXT NOT NULL DEFAULT ''"
            )

        # 初始化管理员账号
        # 只有 admin 会在数据库中不存在时自动创建
        conn.execute(
            """
            INSERT OR IGNORE INTO users
            (username, diamonds, is_admin)
            VALUES (?, ?, 1)
            """,
            ("admin", 0),
        )

        conn.commit()
    finally:
        conn.close()
