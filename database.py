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
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

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
    conn.close()