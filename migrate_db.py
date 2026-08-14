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
    print("Iniciando Migração V7.4 (namespace exclusivo .jt e inicialização compatível com Python 3.14)...")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', chat_type TEXT NOT NULL DEFAULT 'unknown', active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT);
            CREATE TABLE IF NOT EXISTS settings (chat_id INTEGER PRIMARY KEY, antispam INTEGER NOT NULL DEFAULT 1, antilink INTEGER NOT NULL DEFAULT 0, captcha_enabled INTEGER NOT NULL DEFAULT 0, protect_porn INTEGER NOT NULL DEFAULT 0, antiblack INTEGER NOT NULL DEFAULT 0, quarantine_enabled INTEGER NOT NULL DEFAULT 0, protect_pinned INTEGER NOT NULL DEFAULT 1, locked INTEGER NOT NULL DEFAULT 0, lock_snapshot TEXT, warn_threshold INTEGER NOT NULL DEFAULT 3, warn_action TEXT NOT NULL DEFAULT 'mute', warn_duration INTEGER NOT NULL DEFAULT 600, spam_window INTEGER NOT NULL DEFAULT 10, spam_limit INTEGER NOT NULL DEFAULT 6, duplicate_limit INTEGER NOT NULL DEFAULT 3, link_limit INTEGER NOT NULL DEFAULT 3, media_limit INTEGER NOT NULL DEFAULT 5, quarantine_duration INTEGER NOT NULL DEFAULT 600, spam_score_threshold INTEGER NOT NULL DEFAULT 4, quarantine_score_threshold INTEGER NOT NULL DEFAULT 6);
            CREATE TABLE IF NOT EXISTS local_banperm (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER, previous_permissions TEXT, PRIMARY KEY (chat_id, user_id));
            CREATE TABLE IF NOT EXISTS local_blacklist (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, reason TEXT, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER, PRIMARY KEY (chat_id, user_id));
            CREATE TABLE IF NOT EXISTS global_blacklist (user_id INTEGER PRIMARY KEY, type TEXT NOT NULL DEFAULT 'black', reason TEXT, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS global_ban_snapshots (user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, previous_permissions TEXT, created_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, chat_id));
            CREATE TABLE IF NOT EXISTS shadow_ban (user_id INTEGER PRIMARY KEY, reason TEXT, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS link_whitelist (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY (chat_id, user_id));
            CREATE TABLE IF NOT EXISTS authorized_users (user_id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER);
            CREATE TABLE IF NOT EXISTS deleted_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, user_id INTEGER, content TEXT, reason TEXT, created_at INTEGER NOT NULL DEFAULT 0, admin_id INTEGER);
            CREATE TABLE IF NOT EXISTS detected_spies (user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, detected_at INTEGER NOT NULL DEFAULT 0, signals TEXT NOT NULL DEFAULT '', confidence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, chat_id));
            CREATE TABLE IF NOT EXISTS warnings (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0, first_at INTEGER NOT NULL DEFAULT 0, last_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chat_id, user_id));
            CREATE TABLE IF NOT EXISTS temporary_punishments (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, action TEXT NOT NULL, expires_at INTEGER NOT NULL, reason TEXT, created_at INTEGER NOT NULL DEFAULT 0, admin_id INTEGER, previous_permissions TEXT);
            CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
            """
        )
        columns_to_add = (
            ("chats", "title", "TEXT NOT NULL DEFAULT ''"),
            ("chats", "chat_type", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("chats", "active", "INTEGER NOT NULL DEFAULT 1"),
            ("chats", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "username", "TEXT"),
            ("users", "first_name", "TEXT"),
            ("settings", "antispam", "INTEGER NOT NULL DEFAULT 1"),
            ("settings", "antilink", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "captcha_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "protect_porn", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "antiblack", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "quarantine_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "protect_pinned", "INTEGER NOT NULL DEFAULT 1"),
            ("settings", "locked", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "lock_snapshot", "TEXT"),
            ("settings", "warn_threshold", "INTEGER NOT NULL DEFAULT 3"),
            ("settings", "warn_action", "TEXT NOT NULL DEFAULT 'mute'"),
            ("settings", "warn_duration", "INTEGER NOT NULL DEFAULT 600"),
            ("settings", "spam_window", "INTEGER NOT NULL DEFAULT 10"),
            ("settings", "spam_limit", "INTEGER NOT NULL DEFAULT 6"),
            ("settings", "duplicate_limit", "INTEGER NOT NULL DEFAULT 3"),
            ("settings", "link_limit", "INTEGER NOT NULL DEFAULT 3"),
            ("settings", "media_limit", "INTEGER NOT NULL DEFAULT 5"),
            ("settings", "quarantine_duration", "INTEGER NOT NULL DEFAULT 600"),
            ("settings", "spam_score_threshold", "INTEGER NOT NULL DEFAULT 4"),
            ("settings", "quarantine_score_threshold", "INTEGER NOT NULL DEFAULT 6"),
            ("local_banperm", "reason", "TEXT"),
            ("local_banperm", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("local_banperm", "expires_at", "INTEGER"),
            ("local_banperm", "previous_permissions", "TEXT"),
            ("local_blacklist", "reason", "TEXT"),
            ("local_blacklist", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("local_blacklist", "expires_at", "INTEGER"),
            ("global_blacklist", "type", "TEXT NOT NULL DEFAULT 'black'"),
            ("global_blacklist", "reason", "TEXT"),
            ("global_blacklist", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("global_blacklist", "expires_at", "INTEGER"),
            ("shadow_ban", "reason", "TEXT"),
            ("shadow_ban", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("shadow_ban", "expires_at", "INTEGER"),
            ("authorized_users", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("authorized_users", "expires_at", "INTEGER"),
            ("deleted_logs", "chat_id", "INTEGER"),
            ("deleted_logs", "user_id", "INTEGER"),
            ("deleted_logs", "content", "TEXT"),
            ("deleted_logs", "reason", "TEXT"),
            ("deleted_logs", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("deleted_logs", "admin_id", "INTEGER"),
            ("detected_spies", "chat_id", "INTEGER NOT NULL DEFAULT 0"),
            ("detected_spies", "detected_at", "INTEGER NOT NULL DEFAULT 0"),
            ("detected_spies", "signals", "TEXT NOT NULL DEFAULT ''"),
            ("detected_spies", "confidence", "INTEGER NOT NULL DEFAULT 0"),
            ("warnings", "first_at", "INTEGER NOT NULL DEFAULT 0"),
            ("warnings", "last_at", "INTEGER NOT NULL DEFAULT 0"),
            ("temporary_punishments", "chat_id", "INTEGER NOT NULL DEFAULT 0"),
            ("temporary_punishments", "user_id", "INTEGER NOT NULL DEFAULT 0"),
            ("temporary_punishments", "action", "TEXT NOT NULL DEFAULT 'mute'"),
            ("temporary_punishments", "expires_at", "INTEGER NOT NULL DEFAULT 0"),
            ("temporary_punishments", "reason", "TEXT"),
            ("temporary_punishments", "created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("temporary_punishments", "admin_id", "INTEGER"),
            ("temporary_punishments", "previous_permissions", "TEXT"),
        )
        for table, column, definition in columns_to_add:
            ensure_column(conn, table, column, definition)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_blacklist_chat_user ON local_blacklist(chat_id, user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_banperm_chat_user ON local_banperm(chat_id, user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_global_ban_snapshots_user ON global_ban_snapshots(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deleted_logs_created ON deleted_logs(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_temp_punishments_expiry ON temporary_punishments(expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_warnings_chat_last ON warnings(chat_id, last_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_authorized_users_expiry ON authorized_users(expires_at)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(detected_spies)")}
        primary_keys = [row[1] for row in conn.execute("PRAGMA table_info(detected_spies)") if row[5]]
        if "chat_id" in columns and primary_keys == ["user_id"]:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS detected_spies_v2 (user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, detected_at INTEGER NOT NULL DEFAULT 0, signals TEXT NOT NULL DEFAULT '', confidence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, chat_id));
                INSERT OR REPLACE INTO detected_spies_v2(user_id, chat_id, detected_at, signals, confidence) SELECT user_id, COALESCE(chat_id, 0), detected_at, COALESCE(signals, ''), COALESCE(confidence, 0) FROM detected_spies;
                DROP TABLE detected_spies;
                ALTER TABLE detected_spies_v2 RENAME TO detected_spies;
                """
            )
        conn.commit()
        print("Migração V7.4 concluída com sucesso.")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
