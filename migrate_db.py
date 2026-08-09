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

    # Adicionar novas colunas à tabela settings se não existirem
    columns = [
        ("antilink", "INTEGER NOT NULL DEFAULT 0"),
        ("antiraid", "INTEGER NOT NULL DEFAULT 1"),
        ("log_channel", "INTEGER"),
        ("welcome_text", "TEXT"),
        ("welcome_enabled", "INTEGER NOT NULL DEFAULT 0")
    ]

    for col_name, col_type in columns:
        try:
            cursor.execute(f"ALTER TABLE settings ADD COLUMN {col_name} {col_type}")
            print(f"Coluna {col_name} adicionada.")
        except sqlite3.OperationalError:
            print(f"Coluna {col_name} já existe.")

    conn.commit()
    conn.close()
    print("Migração concluída!")

if __name__ == "__main__":
    migrate()
