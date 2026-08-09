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

    print("Iniciando Migração V1.3.5 (Relatórios Detalhados)...")

    # Criar índices para buscas instantâneas
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_global_blacklist_user ON global_blacklist(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shadow_ban_user ON shadow_ban(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_link_whitelist_chat_user ON link_whitelist(chat_id, user_id)")
    except Exception as e:
        print(f"Erro ao criar índices: {e}")

    # Garantir que a coluna 'type' existe na global_blacklist
    try:
        cursor.execute("SELECT type FROM global_blacklist LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE global_blacklist ADD COLUMN type TEXT DEFAULT 'ban'")

    # Adicionar coluna 'reason' se não existir
    try:
        cursor.execute("SELECT reason FROM global_blacklist LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE global_blacklist ADD COLUMN reason TEXT")
        print("Coluna 'reason' adicionada à global_blacklist.")

    try:
        cursor.execute("SELECT reason FROM shadow_ban LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE shadow_ban ADD COLUMN reason TEXT")
        print("Coluna 'reason' adicionada à shadow_ban.")

    conn.commit()
    conn.close()
    print("Migração V1.3.5 concluída!")

if __name__ == "__main__":
    migrate()
