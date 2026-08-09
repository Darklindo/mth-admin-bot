import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "bot.db"

def migrate():
    if not DB_PATH.exists():
        print("Banco de dados não encontrado. O bot criará um novo ao iniciar.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    print("Iniciando migração...")

    # Colunas necessárias para o Modo Noturno e outras funções
    columns = [
        ("antispam", "INTEGER NOT NULL DEFAULT 1"),
        ("antilink", "INTEGER NOT NULL DEFAULT 0"),
        ("antiraid", "INTEGER NOT NULL DEFAULT 1"),
        ("log_channel", "INTEGER"),
        ("welcome_text", "TEXT"),
        ("welcome_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("night_mode_auto", "INTEGER NOT NULL DEFAULT 0"),
        ("night_start", "TEXT DEFAULT '23:00'"),
        ("night_end", "TEXT DEFAULT '07:00'")
    ]

    for col_name, col_type in columns:
        try:
            cursor.execute(f"SELECT {col_name} FROM settings LIMIT 1")
            print(f"Coluna {col_name} já existe.")
        except sqlite3.OperationalError:
            try:
                cursor.execute(f"ALTER TABLE settings ADD COLUMN {col_name} {col_type}")
                print(f"Coluna {col_name} adicionada com sucesso.")
            except Exception as e:
                print(f"Erro ao adicionar {col_name}: {e}")

    # Criar tabela de whitelist de links se não existir
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS link_whitelist (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            )
        """)
        print("Tabela link_whitelist verificada.")
    except Exception as e:
        print(f"Erro ao criar tabela link_whitelist: {e}")

    conn.commit()
    conn.close()
    print("Migração concluída!")

if __name__ == "__main__":
    migrate()
