import sqlite3
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

def migrate():
    print("Iniciando Migração V5.2 (AntiSpy Persistente)...")
    conn = sqlite3.connect(DB_PATH)
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        chat_type TEXT,
        active INTEGER DEFAULT 1,
        created_at INTEGER
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        chat_id INTEGER PRIMARY KEY,
        antispam INTEGER DEFAULT 1,
        antilink INTEGER DEFAULT 0,
        captcha_enabled INTEGER DEFAULT 0,
        protect_porn INTEGER DEFAULT 0,
        antiblack INTEGER DEFAULT 0
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS local_banperm (
        chat_id INTEGER,
        user_id INTEGER,
        reason TEXT,
        created_at INTEGER,
        PRIMARY KEY (chat_id, user_id)
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS local_blacklist (
        chat_id INTEGER,
        user_id INTEGER,
        reason TEXT,
        created_at INTEGER,
        PRIMARY KEY (chat_id, user_id)
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS global_blacklist (
        user_id INTEGER PRIMARY KEY,
        type TEXT,
        reason TEXT,
        created_at INTEGER
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS shadow_ban (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        created_at INTEGER
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS link_whitelist (
        chat_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (chat_id, user_id)
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS authorized_users (
        user_id INTEGER PRIMARY KEY,
        created_at INTEGER
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS deleted_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        content TEXT,
        reason TEXT,
        created_at INTEGER,
        admin_id INTEGER
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS detected_spies (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER,
        detected_at INTEGER
    )""")

    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(deleted_logs)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'admin_id' not in columns:
        try:
            cursor.execute("ALTER TABLE deleted_logs ADD COLUMN admin_id INTEGER")
        except Exception:
            pass

    cursor.execute("PRAGMA table_info(settings)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'antiblack' not in columns:
        try:
            cursor.execute("ALTER TABLE settings ADD COLUMN antiblack INTEGER DEFAULT 0")
        except Exception:
            pass

    conn.commit()
    conn.close()
    print("Migração V5.2 concluída com sucesso!")

if __name__ == "__main__":
    migrate()
