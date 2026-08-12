import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"


def ensure_column(conn, table, column, definition):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate():
    print("Iniciando Migração V6.14 (filtros de baixa latência)...")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                chat_type TEXT NOT NULL DEFAULT 'unknown',
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                antispam INTEGER NOT NULL DEFAULT 1,
                antilink INTEGER NOT NULL DEFAULT 0,
                captcha_enabled INTEGER NOT NULL DEFAULT 0,
                protect_porn INTEGER NOT NULL DEFAULT 0,
                antiblack INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS local_banperm (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS local_blacklist (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS global_blacklist (
                user_id INTEGER PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'black',
                reason TEXT,
                created_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS shadow_ban (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS link_whitelist (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS authorized_users (
                user_id INTEGER PRIMARY KEY,
                created_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deleted_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                content TEXT,
                reason TEXT,
                created_at INTEGER NOT NULL DEFAULT 0,
                admin_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS detected_spies (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                detected_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_local_blacklist_chat_user
                ON local_blacklist(chat_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_local_banperm_chat_user
                ON local_banperm(chat_id, user_id);
            CREATE INDEX IF NOT EXISTS idx_deleted_logs_created
                ON deleted_logs(created_at DESC);
            """
        )

        # Complementa tabelas antigas sem apagar nenhum dado existente.
        ensure_column(conn, "chats", "title", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "chats", "chat_type", "TEXT NOT NULL DEFAULT 'unknown'")
        ensure_column(conn, "chats", "active", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "chats", "created_at", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "users", "username", "TEXT")
        ensure_column(conn, "users", "first_name", "TEXT")
        ensure_column(conn, "settings", "antispam", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "settings", "antilink", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "settings", "captcha_enabled", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "settings", "protect_porn", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "settings", "antiblack", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "deleted_logs", "admin_id", "INTEGER")

        conn.commit()
        print("Migração V6.14 concluída com sucesso!")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
