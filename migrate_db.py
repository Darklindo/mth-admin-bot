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

    print("Iniciando migração V5.0 (Supremo)...")

    # 1. Tabela Shadow Ban
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shadow_ban (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        print("Tabela 'shadow_ban' verificada/criada.")
    except Exception as e:
        print(f"Erro ao criar shadow_ban: {e}")

    # 2. Tabela Captcha Pending (para controlar quem precisa clicar no botão)
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS captcha_pending (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                expiry INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
        """)
        print("Tabela 'captcha_pending' verificada/criada.")
    except Exception as e:
        print(f"Erro ao criar captcha_pending: {e}")

    # 3. Adicionar coluna captcha_enabled em settings
    try:
        cursor.execute("SELECT captcha_enabled FROM settings LIMIT 1")
    except sqlite3.OperationalError:
        try:
            cursor.execute("ALTER TABLE settings ADD COLUMN captcha_enabled INTEGER NOT NULL DEFAULT 0")
            print("Coluna 'captcha_enabled' adicionada a 'settings'.")
        except: pass

    # 4. Verificar colunas antigas (garantir integridade)
    columns_to_check = [
        ("chats", "active", "INTEGER NOT NULL DEFAULT 1"),
        ("settings", "antispam", "INTEGER NOT NULL DEFAULT 1"),
        ("settings", "antilink", "INTEGER NOT NULL DEFAULT 0"),
        ("settings", "antiraid", "INTEGER NOT NULL DEFAULT 1"),
        ("settings", "night_mode_auto", "INTEGER NOT NULL DEFAULT 0")
    ]

    for table, col, col_type in columns_to_check:
        try:
            cursor.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                print(f"Coluna {col} restaurada em {table}.")
            except: pass

    conn.commit()
    conn.close()
    print("Migração V5.0 concluída com sucesso!")

if __name__ == "__main__":
    migrate()
