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
        ("antispam", "INTEGER NOT NULL DEFAULT 1"),
        ("antilink", "INTEGER NOT NULL DEFAULT 0"),
        ("antiraid", "INTEGER NOT NULL DEFAULT 1"),
        ("log_channel", "INTEGER"),
        ("welcome_text", "TEXT"),
        ("welcome_enabled", "INTEGER NOT NULL DEFAULT 0")
    ]

    for col_name, col_type in columns:
        try:
            # Tenta selecionar a coluna para ver se ela existe
            cursor.execute(f"SELECT {col_name} FROM settings LIMIT 1")
            print(f"Coluna {col_name} já existe.")
        except sqlite3.OperationalError:
            # Se der erro, a coluna não existe, então adicionamos
            try:
                cursor.execute(f"ALTER TABLE settings ADD COLUMN {col_name} {col_type}")
                print(f"Coluna {col_name} adicionada com sucesso.")
            except Exception as e:
                print(f"Erro ao adicionar {col_name}: {e}")

    conn.commit()
    conn.close()
    print("Migração concluída!")

if __name__ == "__main__":
    migrate()
