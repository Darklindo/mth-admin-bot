import sqlite3
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"

def migrate():
    print("Iniciando Migração V4.2 (Correção de Colunas)...")
    conn = sqlite3.connect(DB_PATH)
    
    # Tabela de Chats
    conn.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        chat_type TEXT,
        active INTEGER DEFAULT 1,
        created_at INTEGER
    )""")

    # Tabela de Usuários
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT
    )""")

    # Tabela de Configurações
    conn.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        chat_id INTEGER PRIMARY KEY,
        antispam INTEGER DEFAULT 1,
        antilink INTEGER DEFAULT 0,
        captcha_enabled INTEGER DEFAULT 0,
        protect_porn INTEGER DEFAULT 0
    )""")

    # --- PUNIÇÕES LOCAIS ---
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

    # --- PUNIÇÕES GLOBAIS ---
    conn.execute("""
    CREATE TABLE IF NOT EXISTS global_blacklist (
        user_id INTEGER PRIMARY KEY,
        type TEXT, -- 'ban' ou 'black'
        reason TEXT,
        created_at INTEGER
    )""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS shadow_ban (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        created_at INTEGER
    )""")

    # Whitelist de Links
    conn.execute("""
    CREATE TABLE IF NOT EXISTS link_whitelist (
        chat_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (chat_id, user_id)
    )""")

    # Usuários Autorizados (Userbot)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS authorized_users (
        user_id INTEGER PRIMARY KEY,
        created_at INTEGER
    )""")

    # Logs de mensagens deletadas e ações de admin
    conn.execute("""
    CREATE TABLE IF NOT EXISTS deleted_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        content TEXT,
        reason TEXT,
        created_at INTEGER
    )""")

    # --- VERIFICAÇÃO DE COLUNAS FALTANTES (ESSENCIAL) ---
    cursor = conn.cursor()
    
    # Adicionar admin_id na tabela deleted_logs
    cursor.execute("PRAGMA table_info(deleted_logs)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'admin_id' not in columns:
        print("Adicionando coluna 'admin_id' em 'deleted_logs'...")
        try:
            cursor.execute("ALTER TABLE deleted_logs ADD COLUMN admin_id INTEGER")
        except Exception as e:
            print(f"Erro ao adicionar admin_id: {e}")

    # Adicionar protect_porn na tabela settings
    cursor.execute("PRAGMA table_info(settings)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'protect_porn' not in columns:
        print("Adicionando coluna 'protect_porn' em 'settings'...")
        try:
            cursor.execute("ALTER TABLE settings ADD COLUMN protect_porn INTEGER DEFAULT 0")
        except Exception as e:
            print(f"Erro ao adicionar protect_porn: {e}")

    # Índices para performance
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_local_blacklist_chat ON local_blacklist(chat_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_local_banperm_chat ON local_banperm(chat_id)")

    conn.commit()
    conn.close()
    print("Migração V4.2 concluída com sucesso!")

if __name__ == "__main__":
    migrate()
