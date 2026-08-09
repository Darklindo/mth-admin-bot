import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "bot.db"

def migrate():
    if not DB_PATH.exists():
        print("Banco de dados não encontrado.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("Iniciando migração V3.1 (Nuclear)...")

    # 1. Tabela Global Blacklist
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS global_blacklist (
                user_id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        print("Tabela 'global_blacklist' verificada/criada.")
    except Exception as e:
        print(f"Erro ao criar global_blacklist: {e}")

    # 2. Coluna 'active' na tabela chats
    try:
        cursor.execute("SELECT active FROM chats LIMIT 1")
    except sqlite3.OperationalError:
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            print("Coluna 'active' adicionada à tabela 'chats'.")
        except Exception as e:
            print(f"Erro ao alterar tabela chats: {e}")

    # 3. Colunas de settings
    columns = [
        ("night_mode_auto", "INTEGER NOT NULL DEFAULT 0"),
        ("night_start", "TEXT DEFAULT '23:00'"),
        ("night_end", "TEXT DEFAULT '07:00'")
    ]

    for col_name, col_type in columns:
        try:
            cursor.execute(f"SELECT {col_name} FROM settings LIMIT 1")
        except sqlite3.OperationalError:
            try:
                cursor.execute(f"ALTER TABLE settings ADD COLUMN {col_name} {col_type}")
                print(f"Coluna {col_name} adicionada a 'settings'.")
            except: pass

    conn.commit()
    conn.close()
    print("Migração V3.1 concluída!")

if __name__ == "__main__":
    migrate()
