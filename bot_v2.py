import logging
import os
import sqlite3
import time
import asyncio
import re
import hashlib
import json
import unicodedata
import logging
from collections import defaultdict, deque
import threading
from pathlib import Path
from datetime import datetime, timezone
from html import escape

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.tl.types import User
from telethon.errors import RPCError, FloodWaitError, ChatAdminRequiredError, UserAdminInvalidError, UserNotParticipantError, MessageNotModifiedError

# --- CONFIGURAÇÕES INICIAIS ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente no .env: {name}")
    return value


def _env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _setting_int(settings, key, default, minimum, maximum):
    """Lê uma configuração inteira com limites, sem deixar valor inválido derrubar filtros."""
    try:
        value = int((settings or {}).get(key, default))
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(value, maximum))


try:
    API_ID = int(_required_env("API_ID"))
    API_HASH = _required_env("API_HASH")
    OWNER_ID = int(_required_env("OWNER_ID"))
except ValueError as exc:
    raise RuntimeError("API_ID e OWNER_ID devem ser números inteiros no .env") from exc

SECOND_OWNER_ID = int(os.getenv("SECOND_OWNER_ID", "6466326477"))
THIRD_OWNER_ID = int(os.getenv("THIRD_OWNER_ID", "7916427095"))

MIN_PURGE_LIMIT = 5
MAX_PURGE_LIMIT = 100
MAX_HISTORY_SCAN = 1000
MAX_DURATION_SECONDS = 365 * 24 * 60 * 60
MIN_DURATION_SECONDS = 10
# O Telegram trata until_date menor que 30 segundos como banimento permanente.
MIN_TELEGRAM_TEMP_DURATION_SECONDS = 30
EXPIRATION_CHECK_INTERVAL = _env_int("EXPIRATION_CHECK_INTERVAL", 30, 10, 300)
SPAM_STATE_MAX_USERS = _env_int("SPAM_STATE_MAX_USERS", 10000, 100, 100000)
SPAM_ACTION_COOLDOWN = _env_int("SPAM_ACTION_COOLDOWN", 30, 5, 300)
ALLBAN_CONCURRENCY = _env_int("ALLBAN_CONCURRENCY", 3, 1, 8)
BROADCAST_CONCURRENCY = _env_int("BROADCAST_CONCURRENCY", 3, 1, 8)
PURGEALL_MIN_LIMIT = 1
PURGEALL_MAX_LIMIT = 1000
PURGEALL_MAX_SCAN = 1200
PURGEALL_BATCH_SIZE = 50
DEFAULT_DELETE_AFTER = 5
# A primeira mensagem de cada chat é apagada imediatamente. Mensagens que
# chegam enquanto o RPC está em andamento são agrupadas em lotes posteriores.
SECURITY_DELETE_BATCH_SIZE = _env_int("SECURITY_DELETE_BATCH_SIZE", 100, 1, 100)
SECURITY_MAX_PENDING_PER_CHAT = _env_int("SECURITY_MAX_PENDING_PER_CHAT", 10000, 100, 50000)
AUDIT_FLUSH_DELAY = _env_int("AUDIT_FLUSH_DELAY_MS", 25, 1, 1000) / 1000
AUDIT_BATCH_SIZE = _env_int("AUDIT_BATCH_SIZE", 100, 1, 500)
TELEGRAM_TIMEOUT = _env_int("TELEGRAM_TIMEOUT", 10, 5, 30)
TELEGRAM_REQUEST_RETRIES = _env_int("TELEGRAM_REQUEST_RETRIES", 3, 1, 10)
TELEGRAM_CONNECTION_RETRIES = _env_int("TELEGRAM_CONNECTION_RETRIES", 5, 1, 15)
# Não deixe o Telethon suspender um comando por dezenas de segundos. Esperas
# curtas continuam automáticas; FloodWaits longos retornam ao handler para
# tratamento rápido e mensagem controlada.
FLOOD_SLEEP_THRESHOLD = _env_int("FLOOD_SLEEP_THRESHOLD", 5, 0, 60)
STARTED_AT = time.time()
VERSION = "V7.6"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
TELETHON_LOG_LEVEL = os.getenv("TELETHON_LOG_LEVEL", "WARNING").upper()

DB_PATH = DATA_DIR / "bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
# O Telethon continua registrando WARNING/ERROR, mas não despeja no painel
# cada sincronização interna de diferenças como "Got difference".
logging.getLogger("telethon").setLevel(
    getattr(logging, TELETHON_LOG_LEVEL, logging.WARNING)
)
logger = logging.getLogger("jtzin-telethon")

# --- CACHE EM MEMÓRIA ---
class Cache:
    def __init__(self):
        self.global_blacklist = set()
        self.global_blacklist_types = {}
        self.local_blacklist = defaultdict(set)
        self.local_banperm = defaultdict(set)
        self.shadow_ban = set()
        self.link_whitelist = defaultdict(set)
        self.authorized_users = set()
        self.authorized_expirations = {}
        self.antiblack_chats = set()
        self.locked_chats = set()
        self.settings = {}
        # Evita leituras e gravações SQLite nos filtros de alta frequência.
        self.settings_loaded = set()
        self.maintenance_enabled = False
        self.maintenance_loaded = False
        # Identidade aquecida no startup para que .status não faça RPC no caminho comum.
        self.me = None
        self.me_loaded = False

    def load_all(self, db_conn):
        try:
            # Permite recarregar o cache sem manter punições removidas em memória.
            self.global_blacklist.clear()
            self.global_blacklist_types.clear()
            self.local_blacklist.clear()
            self.local_banperm.clear()
            self.shadow_ban.clear()
            self.link_whitelist.clear()
            self.authorized_users.clear()
            self.authorized_expirations.clear()
            self.antiblack_chats.clear()
            self.locked_chats.clear()
            self.settings.clear()
            self.settings_loaded.clear()
            self.maintenance_enabled = False
            self.maintenance_loaded = False
            self.me = None
            self.me_loaded = False

            now = int(time.time())
            cursor = db_conn.execute(
                "SELECT user_id, type FROM global_blacklist "
                "WHERE expires_at IS NULL OR expires_at>?",
                (now,),
            )
            global_rows = cursor.fetchall()
            self.global_blacklist = {int(row[0]) for row in global_rows}
            self.global_blacklist_types = {
                int(row[0]): str(row[1] or "black").lower() for row in global_rows
            }
            
            cursor = db_conn.execute(
                "SELECT chat_id, user_id FROM local_blacklist "
                "WHERE expires_at IS NULL OR expires_at>?",
                (now,),
            )
            for row in cursor.fetchall():
                self.local_blacklist[row[0]].add(row[1])
                
            cursor = db_conn.execute(
                "SELECT chat_id, user_id FROM local_banperm "
                "WHERE expires_at IS NULL OR expires_at>?",
                (now,),
            )
            for row in cursor.fetchall():
                self.local_banperm[row[0]].add(row[1])

            cursor = db_conn.execute(
                "SELECT user_id FROM shadow_ban WHERE expires_at IS NULL OR expires_at>?",
                (now,),
            )
            self.shadow_ban = {row[0] for row in cursor.fetchall()}
            
            cursor = db_conn.execute("SELECT chat_id, user_id FROM link_whitelist")
            for row in cursor.fetchall():
                self.link_whitelist[row[0]].add(row[1])

            try:
                cursor = db_conn.execute(
                    "SELECT user_id, expires_at FROM authorized_users "
                    "WHERE expires_at IS NULL OR expires_at>?",
                    (now,),
                )
                authorized_rows = cursor.fetchall()
            except sqlite3.OperationalError:
                authorized_rows = db_conn.execute("SELECT user_id FROM authorized_users").fetchall()
            self.authorized_users = {int(row[0]) for row in authorized_rows}
            self.authorized_expirations = {
                int(row[0]): (int(row[1]) if len(row) > 1 and row[1] is not None else None)
                for row in authorized_rows
            }
            
            try:
                cursor = db_conn.execute("SELECT chat_id, antiblack FROM settings WHERE antiblack=1")
                for row in cursor.fetchall():
                    self.antiblack_chats.add(row[0])
            except sqlite3.OperationalError:
                pass

            try:
                cursor = db_conn.execute("SELECT * FROM settings")
                for row in cursor.fetchall():
                    chat_id = int(row["chat_id"])
                    self.settings[chat_id] = dict(row)
                    self.settings_loaded.add(chat_id)
            except (sqlite3.OperationalError, TypeError, KeyError):
                # Compatibilidade defensiva com bancos de versões anteriores.
                pass

            try:
                cursor = db_conn.execute("SELECT chat_id FROM settings WHERE locked=1")
                self.locked_chats = {int(row[0]) for row in cursor.fetchall()}
            except sqlite3.OperationalError:
                # Compatibilidade defensiva com bancos de versões anteriores.
                pass

            try:
                row = db_conn.execute("SELECT value FROM bot_state WHERE key='maintenance'").fetchone()
                self.maintenance_enabled = bool(row and str(row[0]) == "1")
                self.maintenance_loaded = True
            except sqlite3.OperationalError:
                self.maintenance_enabled = False
                self.maintenance_loaded = True

            logger.info("Cache carregado com sucesso (%s - filtros de baixa latência).", VERSION)
        except Exception as e:
            logger.error(f"Erro ao carregar cache: {e}")

cache = Cache()


async def get_cached_me():
    """Retorna a identidade local; consulta o Telegram somente no primeiro uso."""
    if cache.me_loaded:
        return cache.me
    try:
        me = await client.get_me()
        cache.me = me
        cache.me_loaded = True
        return me
    except Exception:
        # Permite uma nova tentativa quando a conexão estiver temporariamente indisponível.
        cache.me_loaded = False
        return None

# --- BANCO DE DADOS ---
class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db_lock = threading.RLock()
        self._connect()

    def _connect(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA cache_size=-8192")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def execute(self, query, params=(), commit=False):
        try:
            with self._db_lock:
                cursor = self.conn.execute(query, params)
                if commit:
                    self.conn.commit()
                return cursor
        except Exception as e:
            logger.error(f"DB Error: {e} | Query: {query}")
            try:
                with self._db_lock:
                    self.conn.rollback()
            except Exception as rollback_exc:
                logger.debug("Falha ao desfazer transação após erro SQLite: %s", rollback_exc)
            return None

    def register_chat(self, chat_id, title, chat_type):
        self.execute(
            "INSERT INTO chats(chat_id,title,chat_type,active,created_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type",
            (int(chat_id), title or "", str(chat_type), 1, int(time.time())),
            commit=True
        )

    def set_antiblack(self, chat_id, state: int):
        cursor = self.execute("INSERT INTO settings(chat_id, antiblack) VALUES(?, ?) ON CONFLICT(chat_id) DO UPDATE SET antiblack=excluded.antiblack", (int(chat_id), state), commit=True)
        if cursor is not None:
            if state == 1:
                cache.antiblack_chats.add(int(chat_id))
            else:
                cache.antiblack_chats.discard(int(chat_id))
        return cursor is not None

    def add_authorized(self, user_id, expires_at=None):
        user_id = int(user_id)
        expires_at = int(expires_at) if expires_at is not None else None
        cursor = self.execute(
            "INSERT INTO authorized_users(user_id, created_at, expires_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at",
            (user_id, int(time.time()), expires_at),
            commit=True,
        )
        if cursor is not None:
            cache.authorized_users.add(user_id)
            cache.authorized_expirations[user_id] = expires_at
        return cursor is not None

    def get_authorized_record(self, user_id):
        cursor = self.execute(
            "SELECT user_id, created_at, expires_at FROM authorized_users WHERE user_id=?",
            (int(user_id),),
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row else {}

    def remove_authorized(self, user_id):
        user_id = int(user_id)
        cursor = self.execute("DELETE FROM authorized_users WHERE user_id=?", (user_id,), commit=True)
        removed = cursor is not None and cursor.rowcount > 0
        if removed:
            cache.authorized_users.discard(user_id)
            cache.authorized_expirations.pop(user_id, None)
        return removed

    def expire_authorized(self, now=None):
        now = int(now or time.time())
        rows = self.fetchall(
            "SELECT user_id FROM authorized_users WHERE expires_at IS NOT NULL AND expires_at<=?",
            (now,),
        )
        if rows:
            deleted = self.execute(
                "DELETE FROM authorized_users WHERE expires_at IS NOT NULL AND expires_at<=?",
                (now,),
                commit=True,
            )
            if deleted is not None:
                for row in rows:
                    user_id = int(row["user_id"])
                    cache.authorized_users.discard(user_id)
                    cache.authorized_expirations.pop(user_id, None)
        return rows

    def get_all_authorized(self):
        res = self.execute("SELECT user_id, created_at, expires_at FROM authorized_users ORDER BY created_at DESC")
        if res is None:
            res = self.execute("SELECT user_id, created_at FROM authorized_users ORDER BY created_at DESC")
        if res:
            return [dict(r) for r in res.fetchall()]
        return []

    def add_local_banperm(self, chat_id, user_id, reason=None, expires_at=None, previous_permissions=None):
        chat_id, user_id = int(chat_id), int(user_id)
        now = int(time.time())
        cursor = self.execute(
            "UPDATE local_banperm SET reason=?, created_at=?, expires_at=? WHERE chat_id=? AND user_id=?",
            (reason, now, expires_at, chat_id, user_id),
            commit=True,
        )
        if cursor is None:
            return False
        if cursor.rowcount == 0:
            cursor = self.execute(
                "INSERT INTO local_banperm(chat_id, user_id, reason, created_at, expires_at, previous_permissions) VALUES(?,?,?,?,?,?)",
                (chat_id, user_id, reason, now, expires_at, previous_permissions),
                commit=True,
            )
            if cursor is None:
                return False
        cache.local_banperm[chat_id].add(user_id)
        return True

    def get_local_banperm_record(self, chat_id, user_id):
        cursor = self.execute(
            "SELECT previous_permissions FROM local_banperm WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row else {}

    def get_local_banperm_state(self, chat_id, user_id):
        """Retorna True/False/None: existe, ausente ou falha de leitura."""
        record = self.get_local_banperm_record(chat_id, user_id)
        if record is None:
            return None
        return bool(record)

    def get_local_banperm_snapshot(self, chat_id, user_id):
        record = self.get_local_banperm_record(chat_id, user_id)
        if not record:
            return None
        return record.get("previous_permissions")

    def remove_local_banperm(self, chat_id, user_id):
        cursor = self.execute("DELETE FROM local_banperm WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        removed = cursor is not None and cursor.rowcount > 0
        if removed:
            cache.local_banperm[int(chat_id)].discard(int(user_id))
        return removed

    def add_local_blacklist(self, chat_id, user_id, reason=None, expires_at=None):
        cursor = self.execute("INSERT OR REPLACE INTO local_blacklist(chat_id, user_id, reason, created_at, expires_at) VALUES(?,?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time()), expires_at), commit=True)
        if cursor is not None:
            cache.local_blacklist[int(chat_id)].add(int(user_id))
        return cursor is not None

    def get_local_blacklist_record(self, chat_id, user_id):
        cursor = self.execute(
            "SELECT chat_id, user_id, reason, created_at, expires_at FROM local_blacklist WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row else {}

    def remove_local_blacklist(self, chat_id, user_id):
        cursor = self.execute("DELETE FROM local_blacklist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        removed = cursor is not None and cursor.rowcount > 0
        if removed:
            cache.local_blacklist[int(chat_id)].discard(int(user_id))
        return removed

    def get_global_blacklist_record(self, user_id):
        cursor = self.execute(
            "SELECT user_id, type, reason, created_at, expires_at FROM global_blacklist WHERE user_id=?",
            (int(user_id),),
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def add_global_blacklist(self, user_id, type_name="ban", reason=None, expires_at=None):
        cursor = self.execute("INSERT OR REPLACE INTO global_blacklist(user_id, type, reason, created_at, expires_at) VALUES(?,?,?,?,?)", (int(user_id), type_name, reason, int(time.time()), expires_at), commit=True)
        if cursor is not None:
            user_id = int(user_id)
            cache.global_blacklist.add(user_id)
            cache.global_blacklist_types[user_id] = str(type_name or "black").lower()
        return cursor is not None

    def restore_global_blacklist_record(self, record):
        if not record:
            return False
        cursor = self.execute(
            "INSERT OR REPLACE INTO global_blacklist(user_id, type, reason, created_at, expires_at) VALUES(?,?,?,?,?)",
            (int(record["user_id"]), record["type"], record.get("reason"), int(record.get("created_at") or 0), record.get("expires_at")),
            commit=True,
        )
        if cursor is not None:
            user_id = int(record["user_id"])
            cache.global_blacklist.add(user_id)
            cache.global_blacklist_types[user_id] = str(record.get("type") or "black").lower()
        return cursor is not None

    def remove_global_blacklist(self, user_id):
        cursor = self.execute("DELETE FROM global_blacklist WHERE user_id=?", (int(user_id),), commit=True)
        removed = cursor is not None and cursor.rowcount > 0
        if removed:
            user_id = int(user_id)
            cache.global_blacklist.discard(user_id)
            cache.global_blacklist_types.pop(user_id, None)
        return removed

    def add_global_ban_snapshot(self, user_id, chat_id, previous_permissions=None):
        cursor = self.execute(
            "INSERT OR IGNORE INTO global_ban_snapshots(user_id, chat_id, previous_permissions, created_at) VALUES(?,?,?,?)",
            (int(user_id), int(chat_id), previous_permissions, int(time.time())),
            commit=True,
        )
        return cursor is not None

    def get_global_ban_snapshots(self, user_id):
        cursor = self.execute(
            "SELECT chat_id, previous_permissions FROM global_ban_snapshots WHERE user_id=?",
            (int(user_id),),
        )
        if cursor is None:
            return None
        return [dict(row) for row in cursor.fetchall()]

    def clear_global_ban_snapshots(self, user_id):
        cursor = self.execute(
            "DELETE FROM global_ban_snapshots WHERE user_id=?",
            (int(user_id),),
            commit=True,
        )
        return -1 if cursor is None else int(cursor.rowcount)

    def add_shadow_ban(self, user_id, reason=None, expires_at=None):
        cursor = self.execute("INSERT OR REPLACE INTO shadow_ban(user_id, reason, created_at, expires_at) VALUES(?,?,?,?)", (int(user_id), reason, int(time.time()), expires_at), commit=True)
        if cursor is not None:
            cache.shadow_ban.add(int(user_id))
        return cursor is not None

    def get_shadow_ban_record(self, user_id):
        cursor = self.execute(
            "SELECT user_id, reason, created_at, expires_at FROM shadow_ban WHERE user_id=?",
            (int(user_id),),
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row else {}

    def remove_shadow_ban(self, user_id):
        cursor = self.execute("DELETE FROM shadow_ban WHERE user_id=?", (int(user_id),), commit=True)
        removed = cursor is not None and cursor.rowcount > 0
        if removed:
            cache.shadow_ban.discard(int(user_id))
        return removed

    def add_link_authorized(self, chat_id, user_id):
        chat_id, user_id = int(chat_id), int(user_id)
        cursor = self.execute(
            "INSERT OR IGNORE INTO link_whitelist(chat_id, user_id) VALUES(?, ?)",
            (chat_id, user_id), commit=True,
        )
        if cursor is not None:
            cache.link_whitelist[chat_id].add(user_id)
            return True
        return False

    def remove_link_authorized(self, chat_id, user_id):
        chat_id, user_id = int(chat_id), int(user_id)
        cursor = self.execute(
            "DELETE FROM link_whitelist WHERE chat_id=? AND user_id=?",
            (chat_id, user_id), commit=True,
        )
        if cursor is not None and cursor.rowcount > 0:
            cache.link_whitelist[chat_id].discard(user_id)
            return True
        return False

    def is_link_authorized(self, chat_id, user_id):
        return int(user_id) in cache.link_whitelist.get(int(chat_id), set())

    def set_setting(self, chat_id, key, value):
        allowed = {
            "antispam", "antilink", "quarantine_enabled", "protect_pinned", "warn_threshold",
            "warn_action", "warn_duration", "spam_window", "spam_limit",
            "duplicate_limit", "link_limit", "media_limit", "quarantine_duration", "spam_score_threshold", "quarantine_score_threshold",
        }
        if key not in allowed:
            return False
        cursor = self.execute(
            f"INSERT INTO settings(chat_id, {key}) VALUES(?, ?) "
            f"ON CONFLICT(chat_id) DO UPDATE SET {key}=excluded.{key}",
            (int(chat_id), value), commit=True,
        )
        if cursor is not None:
            chat_id = int(chat_id)
            cache.settings.setdefault(chat_id, {})[key] = value
            cache.settings_loaded.add(chat_id)
            return True
        return False

    def get_settings(self, chat_id):
        chat_id = int(chat_id)
        if chat_id in cache.settings_loaded:
            return dict(cache.settings.get(chat_id, {}))
        row = self.fetchone("SELECT * FROM settings WHERE chat_id=?", (chat_id,))
        if row is None:
            self.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,), commit=True)
            row = self.fetchone("SELECT * FROM settings WHERE chat_id=?", (chat_id,))
        result = dict(row) if row else {}
        cache.settings[chat_id] = result
        cache.settings_loaded.add(chat_id)
        return dict(result)

    def get_chat_lock(self, chat_id):
        row = self.fetchone("SELECT locked, lock_snapshot FROM settings WHERE chat_id=?", (int(chat_id),))
        if row is None:
            self.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (int(chat_id),), commit=True)
            row = self.fetchone("SELECT locked, lock_snapshot FROM settings WHERE chat_id=?", (int(chat_id),))
        return dict(row) if row is not None else None

    def set_chat_lock(self, chat_id, snapshot):
        cursor = self.execute(
            "INSERT INTO settings(chat_id, locked, lock_snapshot) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET locked=excluded.locked, lock_snapshot=excluded.lock_snapshot",
            (int(chat_id), 1, snapshot),
            commit=True,
        )
        if cursor is not None:
            cache.locked_chats.add(int(chat_id))
            cache.settings.setdefault(int(chat_id), {}).update({"locked": 1, "lock_snapshot": snapshot})
            return True
        return False

    def clear_chat_lock(self, chat_id):
        cursor = self.execute(
            "UPDATE settings SET locked=0, lock_snapshot=NULL WHERE chat_id=?",
            (int(chat_id),),
            commit=True,
        )
        if cursor is not None:
            cache.locked_chats.discard(int(chat_id))
            cache.settings.setdefault(int(chat_id), {}).update({"locked": 0, "lock_snapshot": None})
            return True
        return False

    def add_warning(self, chat_id, user_id, now=None):
        now = int(now or time.time())
        settings = self.get_settings(chat_id)
        window = max(60, _setting_int(settings, "spam_window", 10, 5, 120) * 6)
        row = self.fetchone("SELECT count, first_at, last_at FROM warnings WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        if not row or now - int(row["first_at"] or now) > window:
            count, first_at = 1, now
        else:
            count, first_at = int(row["count"]) + 1, int(row["first_at"])
        cursor = self.execute(
            "INSERT INTO warnings(chat_id,user_id,count,first_at,last_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count, first_at=excluded.first_at, last_at=excluded.last_at",
            (int(chat_id), int(user_id), count, first_at, now), commit=True,
        )
        return count if cursor is not None else None

    def get_warning(self, chat_id, user_id):
        cursor = self.execute("SELECT count, first_at, last_at FROM warnings WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row else {"count": 0, "first_at": 0, "last_at": 0}

    def remove_warning(self, chat_id, user_id):
        cursor = self.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        if cursor is None:
            return -1
        row = cursor.fetchone()
        current = int(row["count"]) if row else 0
        if current <= 0:
            return 0
        if current == 1:
            cursor = self.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
            return 0 if cursor is not None else -1
        cursor = self.execute("UPDATE warnings SET count=count-1 WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        return current - 1 if cursor is not None else -1

    def clear_warnings(self, chat_id, user_id):
        cursor = self.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
        if cursor is None:
            return -1
        row = cursor.fetchone()
        removed = int(row["count"]) if row else 0
        cursor = self.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        return removed if cursor is not None else -1

    def add_temporary_punishment(self, chat_id, user_id, action, expires_at, reason=None, admin_id=None, previous_permissions=None):
        chat_id, user_id, action = int(chat_id), int(user_id), str(action)
        now = int(time.time())
        cursor = self.execute(
            "UPDATE temporary_punishments SET expires_at=?, reason=?, created_at=?, admin_id=? WHERE chat_id=? AND user_id=? AND action=?",
            (int(expires_at), reason, now, admin_id, chat_id, user_id, action),
            commit=True,
        )
        if cursor is None:
            return False
        if cursor.rowcount > 0:
            return True
        cursor = self.execute(
            "INSERT INTO temporary_punishments(chat_id,user_id,action,expires_at,reason,created_at,admin_id,previous_permissions) VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, user_id, action, int(expires_at), reason, now, admin_id, previous_permissions),
            commit=True,
        )
        return cursor is not None

    def get_temporary_punishment(self, chat_id, user_id, action):
        cursor = self.execute(
            "SELECT id, chat_id, user_id, action, expires_at, reason, created_at, admin_id, previous_permissions "
            "FROM temporary_punishments WHERE chat_id=? AND user_id=? AND action=?",
            (int(chat_id), int(user_id), str(action)),
        )
        if cursor is None:
            return None
        row = cursor.fetchone()
        return dict(row) if row else {}

    def get_expired_punishments(self, now=None):
        return self.fetchall("SELECT * FROM temporary_punishments WHERE expires_at<=? ORDER BY expires_at LIMIT 200", (int(now or time.time()),))

    def remove_temporary_punishment(self, punishment_id):
        cursor = self.execute("DELETE FROM temporary_punishments WHERE id=?", (int(punishment_id),), commit=True)
        return cursor is not None and cursor.rowcount > 0

    def clear_temporary_punishments(self, chat_id, user_id, actions):
        actions = tuple(str(action) for action in actions)
        if not actions:
            return 0
        placeholders = ",".join("?" for _ in actions)
        params = (int(chat_id), int(user_id), *actions)
        cursor = self.execute(
            f"DELETE FROM temporary_punishments WHERE chat_id=? AND user_id=? AND action IN ({placeholders})",
            params,
            commit=True,
        )
        return int(cursor.rowcount) if cursor is not None else -1

    def get_expired_local_banperm(self, now=None):
        now = int(now or time.time())
        return self.fetchall(
            "SELECT chat_id,user_id FROM local_banperm "
            "WHERE expires_at IS NOT NULL AND expires_at<=?",
            (now,),
        )

    def get_expired_global_blacklist(self, now=None):
        now = int(now or time.time())
        return self.fetchall(
            "SELECT user_id, type FROM global_blacklist "
            "WHERE expires_at IS NOT NULL AND expires_at<=?",
            (now,),
        )

    def expire_local_blacklist(self, now=None):
        now = int(now or time.time())
        rows = self.fetchall("SELECT chat_id,user_id FROM local_blacklist WHERE expires_at IS NOT NULL AND expires_at<=?", (now,))
        if rows:
            deleted = self.execute("DELETE FROM local_blacklist WHERE expires_at IS NOT NULL AND expires_at<=?", (now,), commit=True)
            if deleted is not None:
                for row in rows:
                    cache.local_blacklist[int(row["chat_id"])].discard(int(row["user_id"]))
        return rows

    def expire_local_banperm(self, now=None):
        now = int(now or time.time())
        rows = self.get_expired_local_banperm(now)
        if rows:
            deleted = self.execute("DELETE FROM local_banperm WHERE expires_at IS NOT NULL AND expires_at<=?", (now,), commit=True)
            if deleted is not None:
                for row in rows:
                    cache.local_banperm[int(row["chat_id"])].discard(int(row["user_id"]))
        return rows

    def expire_global_blacklist(self, now=None):
        now = int(now or time.time())
        rows = self.get_expired_global_blacklist(now)
        if rows:
            deleted = self.execute("DELETE FROM global_blacklist WHERE expires_at IS NOT NULL AND expires_at<=?", (now,), commit=True)
            if deleted is not None:
                for row in rows:
                    user_id = int(row["user_id"])
                    cache.global_blacklist.discard(user_id)
                    cache.global_blacklist_types.pop(user_id, None)
        return rows

    def expire_shadow(self, now=None):
        now = int(now or time.time())
        rows = self.fetchall("SELECT user_id FROM shadow_ban WHERE expires_at IS NOT NULL AND expires_at<=?", (now,))
        if rows:
            deleted = self.execute("DELETE FROM shadow_ban WHERE expires_at IS NOT NULL AND expires_at<=?", (now,), commit=True)
            if deleted is not None:
                for row in rows:
                    cache.shadow_ban.discard(int(row["user_id"]))
        return rows

    def get_active_maintenance(self):
        if cache.maintenance_loaded:
            return bool(cache.maintenance_enabled)
        row = self.fetchone("SELECT value FROM bot_state WHERE key='maintenance'")
        cache.maintenance_enabled = bool(row and str(row["value"]) == "1")
        cache.maintenance_loaded = True
        return cache.maintenance_enabled

    def set_maintenance(self, enabled):
        success = self.execute(
            "INSERT INTO bot_state(key,value) VALUES('maintenance',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if enabled else "0",), commit=True,
        ) is not None
        if success:
            cache.maintenance_enabled = bool(enabled)
            cache.maintenance_loaded = True
        return success

    def fetchone(self, query, params=()):
        cursor = self.execute(query, params)
        return cursor.fetchone() if cursor is not None else None

    def fetchall(self, query, params=()):
        cursor = self.execute(query, params)
        return cursor.fetchall() if cursor is not None else []

    def resolve_username(self, username):
        username = (username or "").lower().lstrip("@")
        if not username:
            return None
        row = self.fetchone("SELECT user_id FROM users WHERE username=? LIMIT 1", (username,))
        return int(row["user_id"]) if row else None

    def get_user_info(self, user_id):
        row = self.fetchone("SELECT username, first_name FROM users WHERE user_id=?", (int(user_id),))
        if row:
            display = f"@{row['username']}" if row["username"] else (row["first_name"] or str(user_id))
            return escape(str(display))
        return str(user_id)

    def get_user_info_many(self, user_ids):
        """Resolve vários usuários com uma única consulta, preservando a ordem."""
        ordered_ids = []
        seen = set()
        for value in user_ids or ():
            try:
                user_id = int(value)
            except (TypeError, ValueError):
                continue
            if user_id not in seen:
                seen.add(user_id)
                ordered_ids.append(user_id)
        if not ordered_ids:
            return {}
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self.fetchall(
            f"SELECT user_id, username, first_name FROM users WHERE user_id IN ({placeholders})",
            tuple(ordered_ids),
        )
        resolved = {}
        for row in rows:
            user_id = int(row["user_id"])
            display = f"@{row['username']}" if row["username"] else (row["first_name"] or str(user_id))
            resolved[user_id] = escape(str(display))
        for user_id in ordered_ids:
            resolved.setdefault(user_id, str(user_id))
        return resolved

    def get_all_banned_list_detailed(self):
        shadow = self.fetchall("SELECT user_id, reason, created_at FROM shadow_ban ORDER BY created_at DESC")
        glob = self.fetchall("SELECT user_id, type, reason, created_at FROM global_blacklist ORDER BY created_at DESC")
        return [dict(r) for r in shadow], [dict(r) for r in glob]

    def all_chats_detailed(self):
        rows = self.fetchall("SELECT chat_id, title, chat_type, active FROM chats")
        return [dict(r) for r in rows]

    def get_diagnostic_counts(self):
        tables = {
            "chats": "chats",
            "users": "users",
            "authorized": "authorized_users",
            "global_blacklist": "global_blacklist",
            "local_blacklist": "local_blacklist",
            "local_banperm": "local_banperm",
            "shadow": "shadow_ban",
            "deleted_logs": "deleted_logs",
            "spies": "detected_spies",
        }
        counts = {}
        for key, table in tables.items():
            row = self.fetchone(f"SELECT COUNT(*) AS total FROM {table}")
            counts[key] = int(row["total"]) if row is not None else 0
        return counts

    def get_db_size_bytes(self):
        try:
            return int(self.path.stat().st_size)
        except (OSError, TypeError, ValueError):
            return 0

    def remember_user(self, user_id, username, first_name):
        if not user_id: return
        username = (username or "").lower().lstrip("@") or None
        self.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (int(user_id), username, first_name or ""),
            commit=True
        )

    def add_deleted_logs_batch(self, records):
        """Persiste vários eventos de auditoria com apenas uma transação."""
        if not records:
            return 0
        values = [
            (
                int(chat_id),
                int(user_id),
                admin_id,
                content or "[Mídia / Ação]",
                reason,
                int(created_at or time.time()),
            )
            for chat_id, user_id, admin_id, content, reason, created_at in records
        ]
        try:
            with self._db_lock:
                self.conn.executemany(
                    "INSERT INTO deleted_logs(chat_id, user_id, admin_id, content, reason, created_at) VALUES(?,?,?,?,?,?)",
                    values,
                )
                self.conn.commit()
            return len(values)
        except sqlite3.OperationalError as exc:
            if "admin_id" not in str(exc):
                logger.error(f"DB Error ao registrar lote de logs: {exc}")
                return 0
            try:
                with self._db_lock:
                    self.conn.rollback()
                    self.conn.executemany(
                        "INSERT INTO deleted_logs(chat_id, user_id, content, reason, created_at) VALUES(?,?,?,?,?)",
                        [
                            (chat_id, user_id, content, reason, created_at)
                            for chat_id, user_id, _admin_id, content, reason, created_at in values
                        ],
                    )
                    self.conn.commit()
                return len(values)
            except sqlite3.Error as fallback_exc:
                logger.error(f"DB Error no fallback de lote: {fallback_exc}")
                return 0
        except sqlite3.Error as exc:
            logger.error(f"DB Error ao registrar lote de logs: {exc}")
            try:
                with self._db_lock:
                    self.conn.rollback()
            except sqlite3.Error:
                pass
            return 0

    def add_deleted_log(self, chat_id, user_id, content, reason, admin_id=None):
        record = (chat_id, user_id, admin_id, content, reason, int(time.time()))
        return self.add_deleted_logs_batch([record]) == 1

    def get_latest_logs(self, limit=10):
        safe_limit = max(1, min(int(limit), 100))
        return [dict(r) for r in self.fetchall("SELECT * FROM deleted_logs ORDER BY created_at DESC LIMIT ?", (safe_limit,))]

    def add_detected_spy(self, user_id, chat_id, signals="", confidence=0):
        cursor = self.execute(
            "INSERT OR REPLACE INTO detected_spies(user_id, chat_id, detected_at, signals, confidence) VALUES(?,?,?,?,?)",
            (int(user_id), int(chat_id), int(time.time()), str(signals), int(confidence)),
            commit=True
        )
        return cursor is not None

    def get_all_spies(self):
        return [dict(r) for r in self.fetchall("SELECT * FROM detected_spies ORDER BY detected_at DESC")]

    def remove_spy(self, user_id):
        cursor = self.execute("DELETE FROM detected_spies WHERE user_id = ?", (int(user_id),), commit=True)
        return cursor is not None and cursor.rowcount > 0

    def get_warnings_report(self, chat_id=None):
        if chat_id is None:
            return [dict(row) for row in self.fetchall("SELECT * FROM warnings ORDER BY last_at DESC")]
        return [dict(row) for row in self.fetchall("SELECT * FROM warnings WHERE chat_id=? ORDER BY last_at DESC", (int(chat_id),))]

try:
    from migrate_db import migrate as migrate_database
    migrate_database()
except Exception as exc:
    raise RuntimeError(f"Falha na migração automática do banco: {exc}") from exc

db = Database(DB_PATH)

# --- CLIENTE TELETHON ---
client = TelegramClient(
    "jtzin_session",
    API_ID,
    API_HASH,
    timeout=TELEGRAM_TIMEOUT,
    request_retries=TELEGRAM_REQUEST_RETRIES,
    connection_retries=TELEGRAM_CONNECTION_RETRIES,
    retry_delay=1,
    auto_reconnect=True,
    sequential_updates=False,
    flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD,
    entity_cache_limit=10000,
    device_model="Jtzin Userbot",
    app_version=VERSION,
)


class AuditBuffer:
    """Agrupa logs e grava-os sem bloquear o event loop do Telethon."""

    def __init__(self, database):
        self.database = database
        self.records = deque()
        self.flush_task = None
        self.flush_lock = asyncio.Lock()
        self.enqueued = 0
        self.persisted = 0
        self.failed = 0

    def enqueue(self, chat_id, user_id, content, reason, admin_id=None):
        record = (chat_id, user_id, admin_id, content, reason, int(time.time()))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.failed += 1
            self.database.add_deleted_log(
                chat_id, user_id, content, reason, admin_id=admin_id
            )
            return
        self.records.append(record)
        self.enqueued += 1
        if self.flush_task is None or self.flush_task.done():
            self.flush_task = schedule_background(self._flush_after_delay(), "audit-flush")

    async def _flush_after_delay(self):
        try:
            await asyncio.sleep(AUDIT_FLUSH_DELAY)
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed += 1
            logger.error("Falha no buffer de auditoria: %s", exc)

    async def flush(self):
        async with self.flush_lock:
            while self.records:
                batch = []
                while self.records and len(batch) < AUDIT_BATCH_SIZE:
                    batch.append(self.records.popleft())
                try:
                    persisted = await asyncio.to_thread(
                        self.database.add_deleted_logs_batch, batch
                    )
                    self.persisted += persisted
                    if persisted < len(batch):
                        failed_records = batch[persisted:]
                        self.failed += len(failed_records)
                        for record in reversed(failed_records):
                            self.records.appendleft(record)
                        logger.error("Lote de auditoria parcialmente persistido; %s registros permaneceram pendentes.", len(failed_records))
                        break
                except Exception as exc:
                    self.failed += len(batch)
                    # Não descartar registros quando o SQLite falhar
                    # temporariamente; eles permanecem pendentes para o
                    # próximo flush ou para a rotina de encerramento.
                    for record in reversed(batch):
                        self.records.appendleft(record)
                    logger.error("Falha ao persistir lote de auditoria: %s", exc)
                    break

    def pending_count(self):
        return len(self.records)


class SecurityDeleteQueue:
    """Apaga a primeira mensagem imediatamente e agrupa as seguintes por chat."""

    def __init__(self, telegram_client):
        self.telegram_client = telegram_client
        self.pending = defaultdict(deque)
        self.running_chats = set()
        self.immediate = 0
        self.batched = 0
        self.deleted = 0
        self.failed = 0
        self.overflow = 0
        self.last_delete_ms = 0.0
        self.max_delete_ms = 0.0

    async def _delete_rpc(self, chat_id, items):
        if not items:
            return
        entity = items[0][0] or chat_id
        message_ids = [message_id for _entity, message_id in items]
        started = time.perf_counter()
        try:
            await self.telegram_client.delete_messages(entity, message_ids, revoke=True)
            self.deleted += len(message_ids)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
            try:
                await self.telegram_client.delete_messages(entity, message_ids, revoke=True)
                self.deleted += len(message_ids)
            except Exception as retry_exc:
                self.failed += len(message_ids)
                logger.error("Falha ao apagar lote após FloodWait: %s", retry_exc)
        except Exception as exc:
            self.failed += len(message_ids)
            logger.debug("Falha no lote de exclusão no chat %s: %s", chat_id, exc)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.last_delete_ms = elapsed_ms
            self.max_delete_ms = max(self.max_delete_ms, elapsed_ms)

    async def _drain_chat(self, chat_id):
        try:
            while self.pending[chat_id]:
                batch = []
                while self.pending[chat_id] and len(batch) < SECURITY_DELETE_BATCH_SIZE:
                    batch.append(self.pending[chat_id].popleft())
                if batch:
                    self.batched += len(batch)
                    await self._delete_rpc(chat_id, batch)
                await asyncio.sleep(0)
        finally:
            self.pending.pop(chat_id, None)
            self.running_chats.discard(chat_id)

    async def submit(self, chat_id, entity, message_id):
        item = (entity, int(message_id))
        if chat_id in self.running_chats:
            if len(self.pending[chat_id]) >= SECURITY_MAX_PENDING_PER_CHAT:
                self.overflow += 1
                await self._delete_rpc(chat_id, [item])
            else:
                self.pending[chat_id].append(item)
            return

        self.running_chats.add(chat_id)
        self.immediate += 1
        await self._delete_rpc(chat_id, [item])

        if self.pending.get(chat_id):
            # Mantém o chat marcado como ocupado até o dreno terminar, para
            # preservar a ordem e evitar duas requisições simultâneas.
            schedule_background(self._drain_chat(chat_id), "security-delete-drain")
        else:
            self.running_chats.discard(chat_id)

    def snapshot(self):
        return {
            "pending": sum(len(items) for items in self.pending.values()),
            "immediate": self.immediate,
            "batched": self.batched,
            "deleted": self.deleted,
            "failed": self.failed,
            "overflow": self.overflow,
            "last_delete_ms": self.last_delete_ms,
            "max_delete_ms": self.max_delete_ms,
        }


class CommandMetrics:
    """Métricas leves em memória; nunca bloqueiam comandos nem acessam SQLite."""

    def __init__(self, max_commands=128):
        self._active = {}
        self._stats = {}
        self.max_commands = max(16, int(max_commands))

    @staticmethod
    def _name(event):
        parts = str(getattr(event, "raw_text", "") or "").strip().split(maxsplit=1)
        raw = parts[0] if parts else ""
        if raw.startswith(".") and len(raw) <= 48:
            return raw.lower()
        return "<other>"

    def start(self, event):
        key = id(event)
        if key not in self._active:
            self._active[key] = (self._name(event), time.perf_counter())

    def finish(self, event, success=True):
        item = self._active.pop(id(event), None)
        if item is None:
            return
        name, started = item
        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        row = self._stats.setdefault(name, {"count": 0, "failed": 0, "last_ms": 0.0, "max_ms": 0.0, "total_ms": 0.0})
        row["count"] += 1
        row["failed"] += 0 if success else 1
        row["last_ms"] = elapsed_ms
        row["max_ms"] = max(row["max_ms"], elapsed_ms)
        row["total_ms"] += elapsed_ms
        if len(self._stats) > self.max_commands:
            oldest = next(iter(self._stats))
            if oldest != name:
                self._stats.pop(oldest, None)

    def snapshot(self):
        stats = {name: dict(values) for name, values in self._stats.items()}
        total = sum(row["count"] for row in stats.values())
        failures = sum(row["failed"] for row in stats.values())
        for row in stats.values():
            row["avg_ms"] = row["total_ms"] / row["count"] if row["count"] else 0.0
        return {"total": total, "failed": failures, "active": len(self._active), "commands": stats}


command_metrics = CommandMetrics()


class BackgroundTaskSupervisor:
    """Supervisiona tarefas de fundo sem bloquear handlers ou criar exceções silenciosas."""

    def __init__(self):
        self.tasks = set()
        self.started = 0
        self.completed = 0
        self.failed = 0
        self.cancelled = 0
        self._lock = threading.Lock()

    def create(self, coroutine, label):
        try:
            task = asyncio.create_task(coroutine, name=f"jtzin:{label}")
        except RuntimeError:
            close = getattr(coroutine, "close", None)
            if close:
                close()
            raise
        self.tasks.add(task)
        with self._lock:
            self.started += 1

        def _done(completed_task):
            self.tasks.discard(completed_task)
            if completed_task.cancelled():
                with self._lock:
                    self.cancelled += 1
                return
            try:
                completed_task.result()
            except Exception:
                with self._lock:
                    self.failed += 1
                logger.exception("Falha na tarefa de fundo '%s'", label)
            else:
                with self._lock:
                    self.completed += 1

        task.add_done_callback(_done)
        return task

    async def cancel_all(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    def snapshot(self):
        with self._lock:
            return {
                "active": len(self.tasks),
                "started": self.started,
                "completed": self.completed,
                "failed": self.failed,
                "cancelled": self.cancelled,
            }


background_supervisor = BackgroundTaskSupervisor()


def schedule_background(coroutine, label):
    """Agenda tarefa supervisionada; o chamador continua sem esperar por ela."""
    return background_supervisor.create(coroutine, label)


# Inicializados após a criação do cliente para permitir testes offline e substituição controlada.
audit_buffer = AuditBuffer(db)
security_delete_queue = SecurityDeleteQueue(client)


def queue_audit_log(chat_id, user_id, content, reason, admin_id=None):
    try:
        audit_buffer.enqueue(chat_id, user_id, content, reason, admin_id=admin_id)
    except Exception as exc:
        # Auditoria nunca pode interromper exclusões ou comandos de segurança.
        logger.error("Falha ao enfileirar auditoria: %s", exc)


def get_performance_snapshot():
    queue_stats = security_delete_queue.snapshot()
    return {
        **queue_stats,
        "audit_pending": audit_buffer.pending_count(),
        "audit_enqueued": audit_buffer.enqueued,
        "audit_persisted": audit_buffer.persisted,
        "audit_failed": audit_buffer.failed,
        # Alias mantido para evitar quebra em instalações com o nome antigo.
        "delete_failed": queue_stats.get("failed", 0),
        "command_metrics": command_metrics.snapshot(),
        "background_tasks": background_supervisor.snapshot(),
    }


def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID, THIRD_OWNER_ID]

def is_authorized(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    user_id = int(user_id or 0)
    if user_id not in cache.authorized_users:
        return False
    expires_at = cache.authorized_expirations.get(user_id)
    return expires_at is None or expires_at > int(time.time())


ADMIN_CACHE_TTL = _env_int("ADMIN_CACHE_TTL", 180, 10, 900)
_admin_status_cache = {}
_admin_status_inflight = {}


def is_immune(user_id: int) -> bool:
    """Somente os proprietários ficam imunes às punições do Userbot."""
    try:
        normalized_id = int(user_id or 0)
    except (TypeError, ValueError):
        return False
    return normalized_id != 0 and is_owner(normalized_id)


async def _refresh_chat_admin_status(chat_id, user_id):
    """Atualiza o cargo uma vez por chave e compartilha o resultado entre eventos."""
    key = (int(chat_id), int(user_id))
    try:
        permissions = await client.get_permissions(chat_id, user_id)
        allowed = bool(
            getattr(permissions, "is_admin", False)
            or getattr(permissions, "is_creator", False)
            or type(getattr(permissions, "participant", None)).__name__ in {
                "ChannelParticipantAdmin", "ChannelParticipantCreator",
            }
        )
    except (RPCError, ValueError, TypeError):
        allowed = False
    except Exception as exc:
        logger.debug("Falha ao verificar cargo no chat %s: %s", chat_id, exc)
        allowed = False
    _admin_status_cache[key] = (allowed, time.monotonic())
    if len(_admin_status_cache) > 20000:
        oldest = min(_admin_status_cache, key=lambda item: _admin_status_cache[item][1])
        _admin_status_cache.pop(oldest, None)
    return allowed


def _schedule_admin_status_refresh(chat_id, user_id):
    """Agenda uma única consulta de cargo sem bloquear filtros de mensagens."""
    key = (int(chat_id), int(user_id))
    task = _admin_status_inflight.get(key)
    if task is not None and not task.done():
        return task
    task = schedule_background(
        _refresh_chat_admin_status(chat_id, user_id),
        "admin-status-refresh",
    )
    _admin_status_inflight[key] = task

    def _forget(_completed):
        if _admin_status_inflight.get(key) is task:
            _admin_status_inflight.pop(key, None)

    task.add_done_callback(_forget)
    return task


async def is_chat_admin(chat_id, user_id, use_cache=True, wait_for_rpc=True):
    """Consulta o cargo com cache; filtros podem optar por não esperar o RPC."""
    if not chat_id or not user_id:
        return False
    user_id = int(user_id)
    if is_owner(user_id):
        return True
    key = (int(chat_id), user_id)
    now = time.monotonic()
    cached = _admin_status_cache.get(key)
    if use_cache and cached and now - cached[1] < ADMIN_CACHE_TTL:
        return cached[0]
    task = _admin_status_inflight.get(key)
    if not wait_for_rpc:
        _schedule_admin_status_refresh(chat_id, user_id)
        # Estado desconhecido não deve bloquear o dispatcher. O Telegram já
        # aplica as permissões do grupo; o resultado será usado nos próximos eventos.
        return None
    if task is None or task.done():
        task = _schedule_admin_status_refresh(chat_id, user_id)
    try:
        return await asyncio.shield(task)
    except Exception as exc:
        logger.debug("Falha ao aguardar estado administrativo de %s/%s: %s", chat_id, user_id, exc)
        return False


async def can_manage_chat(event):
    if is_owner(event.sender_id):
        return True
    return await is_chat_admin(event.chat_id, event.sender_id)


async def require_chat_admin(event, action):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, f"❌ O comando para {action} só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return False
    if not await can_manage_chat(event):
        await reply_or_edit(event, f"❌ Somente administradores deste grupo podem {action}.", delete_after=DEFAULT_DELETE_AFTER)
        return False
    return True


_CHAT_RIGHT_FIELDS = (
    "view_messages", "send_messages", "send_media", "send_stickers", "send_gifs",
    "send_games", "send_inline", "embed_links", "send_polls", "change_info",
    "invite_users", "pin_messages", "manage_topics", "send_photos", "send_videos",
    "send_roundvideos", "send_audios", "send_voices", "send_docs", "send_plain",
    "edit_rank", "send_reactions",
)


def serialize_chat_default_rights(rights):
    """Converte ChatBannedRights em JSON sem depender de objetos TL serializáveis."""
    if rights is None:
        return json.dumps({"rights": None}, separators=(",", ":"))
    payload = {}
    for field in _CHAT_RIGHT_FIELDS:
        value = getattr(rights, field, None)
        payload[field] = bool(value) if value is not None else None
    until_date = getattr(rights, "until_date", None)
    payload["until_date"] = int(until_date.timestamp()) if isinstance(until_date, datetime) else None
    return json.dumps({"rights": payload}, separators=(",", ":"), sort_keys=True)


def deserialize_chat_default_rights(snapshot):
    """Reconstrói permissões padrão; snapshots inválidos interrompem o rollback."""
    if not snapshot:
        raise ValueError("snapshot de permissões ausente")
    payload = json.loads(snapshot)
    values = payload.get("rights")
    if values is None:
        return types.ChatBannedRights(until_date=None)
    if not isinstance(values, dict):
        raise ValueError("snapshot de permissões inválido")
    until_date = values.get("until_date")
    if until_date is not None:
        until_date = datetime.fromtimestamp(int(until_date))
    kwargs = {
        field: values[field]
        for field in _CHAT_RIGHT_FIELDS
        if field in values and values[field] is not None
    }
    return types.ChatBannedRights(until_date=until_date, **kwargs)


async def capture_chat_default_rights(chat_id):
    """Obtém as permissões padrão atuais do grupo/canal para permitir unlock sem perda de configuração."""
    entity = await client.get_entity(chat_id)
    if isinstance(entity, types.Channel):
        result = await client(functions.channels.GetFullChannelRequest(channel=entity))
    elif isinstance(entity, types.Chat):
        result = await client(functions.messages.GetFullChatRequest(chat_id=entity))
    else:
        raise ValueError("o destino não é um grupo ou canal compatível")
    full_chat = getattr(result, "full_chat", None)
    return serialize_chat_default_rights(getattr(full_chat, "default_banned_rights", None))


async def apply_chat_default_rights(chat_id, rights):
    await client(functions.messages.EditChatDefaultBannedRightsRequest(
        peer=chat_id,
        banned_rights=rights,
    ))


def locked_chat_rights():
    """Bloqueia texto, mídia, links, stickers, GIFs, arquivos e reações de membros."""
    return types.ChatBannedRights(
        until_date=None,
        send_messages=True,
        send_media=True,
        send_stickers=True,
        send_gifs=True,
        send_games=True,
        send_inline=True,
        embed_links=True,
        send_polls=True,
        send_photos=True,
        send_videos=True,
        send_roundvideos=True,
        send_audios=True,
        send_voices=True,
        send_docs=True,
        send_plain=True,
        send_reactions=True,
    )


async def restore_global_ban(user_id):
    """Restaura somente os chats atingidos; usa fallback para registros antigos."""
    snapshots = await asyncio.to_thread(db.get_global_ban_snapshots, user_id)
    if snapshots is None:
        return 0, 1
    if snapshots:
        attempted = 0
        failed = 0
        for row in snapshots:
            chat_id = int(row.get("chat_id") or 0)
            # chat_id=0 é um marcador criado pelo allban atual quando não
            # houve nenhum chat aplicável; nunca é uma entidade Telegram.
            if chat_id == 0:
                continue
            attempted += 1
            try:
                await restore_permission_snapshot(
                    chat_id, user_id, row.get("previous_permissions"), "ban"
                )
            except Exception as exc:
                failed += 1
                logger.debug(
                    "Falha ao restaurar ban global de %s no chat %s: %s",
                    user_id,
                    row["chat_id"],
                    exc,
                )
        return attempted, failed

    attempted = 0
    failed = 0
    rows = await asyncio.to_thread(db.all_chats_detailed)
    for chat in rows:
        chat_type = str(chat.get("chat_type") or "").lower()
        if not chat.get("active") or chat_type not in {"group", "supergroup", "channel", "chat"}:
            continue
        attempted += 1
        try:
            await client.edit_permissions(
                chat["chat_id"], user_id, view_messages=True, send_messages=True
            )
        except Exception as exc:
            failed += 1
            logger.debug(
                "Falha ao restaurar ban global legado de %s no chat %s: %s",
                user_id,
                chat["chat_id"],
                exc,
            )
    return attempted, failed


PERMISSION_SNAPSHOT_FIELDS = (
    "view_messages", "send_messages", "send_media", "send_stickers",
    "send_gifs", "send_games", "send_inline", "embed_links",
    "send_polls", "change_info", "invite_users", "pin_messages",
)
PERMISSION_EDIT_FIELDS = {
    "view_messages", "send_messages", "send_media", "send_stickers",
    "send_gifs", "send_games", "send_inline", "send_polls",
    "change_info", "invite_users", "pin_messages",
}


PERMISSION_SNAPSHOT_VERSION = 2


async def capture_permission_snapshot(chat_id, user_id):
    """Captura permissões permitidas antes de uma restrição temporária.

    O Telethon expõe ``banned_rights`` como direitos revogados, enquanto
    ``client.edit_permissions`` recebe permissões permitidas. O snapshot V2
    guarda a forma permitida para que a restauração não inverta o estado.
    """
    try:
        permissions = await client.get_permissions(chat_id, user_id)
        participant = getattr(permissions, "participant", permissions)
        banned_rights = getattr(participant, "banned_rights", None)
        if banned_rights is None:
            # Compatibilidade com fakes/versões antigas que expõem o campo
            # diretamente no objeto retornado por get_permissions.
            banned_rights = getattr(permissions, "banned_rights", None)
        if banned_rights is None:
            return "{}"
        allowed = {
            field: not bool(getattr(banned_rights, field, False))
            for field in PERMISSION_SNAPSHOT_FIELDS
            if hasattr(banned_rights, field)
        }
        snapshot = {"version": PERMISSION_SNAPSHOT_VERSION, "permissions": allowed}
        until_date = getattr(banned_rights, "until_date", None) or getattr(permissions, "until_date", None)
        if until_date:
            if isinstance(until_date, datetime):
                until_timestamp = int(until_date.timestamp())
            else:
                until_timestamp = int(until_date)
            if until_timestamp > int(time.time()):
                snapshot["until_date"] = until_timestamp
        return json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
    except Exception as exc:
        logger.debug("Não foi possível capturar permissões de %s/%s: %s", chat_id, user_id, exc)
        return None


async def restore_permission_snapshot(chat_id, user_id, snapshot=None, action=None):
    """Restaura o snapshot V2 e converte snapshots legados com segurança."""
    kwargs = {}
    if snapshot:
        try:
            data = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
            if isinstance(data, dict):
                version = int(data.get("version", 1))
                source = data.get("permissions") if version >= PERMISSION_SNAPSHOT_VERSION else data
                if not isinstance(source, dict):
                    raise ValueError("permissões ausentes no snapshot")
                raw_until_date = data.get("until_date")
                if raw_until_date:
                    try:
                        until_timestamp = int(raw_until_date)
                        if until_timestamp > int(time.time()):
                            kwargs["until_date"] = telegram_datetime(until_timestamp)
                    except (TypeError, ValueError, OverflowError, OSError):
                        logger.warning("Prazo inválido no snapshot de permissões para %s/%s", chat_id, user_id)
                for field in PERMISSION_SNAPSHOT_FIELDS:
                    if field not in source or source[field] is None:
                        continue
                    edit_field = "embed_link_previews" if field == "embed_links" else field
                    if edit_field not in PERMISSION_EDIT_FIELDS and edit_field != "embed_link_previews":
                        continue
                    # V1 armazenava direitos banidos; V2 armazena permissões
                    # permitidas. A conversão mantém bancos antigos seguros.
                    kwargs[edit_field] = bool(source[field]) if version >= PERMISSION_SNAPSHOT_VERSION else not bool(source[field])
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Snapshot de permissões inválido para %s/%s", chat_id, user_id)
    if not kwargs:
        action = str(action or "").lower()
        if action in {"ban", "banperm"}:
            kwargs = {"view_messages": True, "send_messages": True}
        else:
            kwargs = {"send_messages": True, "send_media": True}
    await client.edit_permissions(chat_id, user_id, **kwargs)


LINK_PATTERN = re.compile(
    r"(?i)(?<![\w@])(?:"
    r"https?://|hxxps?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://|"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24}|xn--[a-z0-9-]{2,59})"
    r")[^\s<>()]+"
)

_OBFUSCATED_DOT_PATTERN = re.compile(
    r"(?i)\s*(?:\[\s*\.\s*\]|\(\s*(?:dot|ponto|\.)\s*\)|"
    r"\[\s*(?:dot|ponto)\s*\]|\{\s*(?:dot|ponto)\s*\})\s*"
)
_ZERO_WIDTH_PATTERN = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_LINK_ENTITY_NAMES = {"MessageEntityUrl", "MessageEntityTextUrl"}


def normalize_link_text(text):
    """Normaliza obfuscações comuns sem remover espaços de texto normal."""
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = _ZERO_WIDTH_PATTERN.sub("", normalized)
    normalized = re.sub(r"(?i)\bhxxps?://", "https://", normalized)
    normalized = _OBFUSCATED_DOT_PATTERN.sub(".", normalized)
    return normalized


def count_message_links(message, text=None):
    """Conta links visíveis e entidades de URL sem contar a mesma URL duas vezes."""
    raw_text = text if text is not None else getattr(message, "raw_text", "") or ""
    normalized = normalize_link_text(raw_text)
    visible_count = len(LINK_PATTERN.findall(normalized))
    entity_count = sum(
        1 for entity in getattr(message, "entities", None) or ()
        if type(entity).__name__ in _LINK_ENTITY_NAMES
    )
    return max(visible_count, entity_count)


def message_contains_link(message, text=None):
    """Detecta URLs modernas, obfuscadas e links ocultos em entidades do Telegram."""
    return count_message_links(message, text) > 0


async def get_target_from_event(event):
    """Resolve alvo por reply, ID ou username sem confundir o autor do comando."""
    try:
        reply = await event.get_reply_message()
        if reply is not None and getattr(reply, "id", None) != getattr(event, "id", None):
            sender_id = getattr(reply, "sender_id", None)
            if sender_id:
                return int(sender_id)

            # Algumas mensagens de mídia/serviço não expõem sender_id diretamente.
            # Tenta a entidade real antes de considerar o alvo inválido.
            try:
                sender = await reply.get_sender()
                entity_id = getattr(sender, "id", None)
                if entity_id:
                    return int(entity_id)
            except Exception as sender_exc:
                logger.debug("Não foi possível resolver o remetente da resposta: %s", sender_exc)

            forward = getattr(reply, "forward", None)
            forward_sender_id = getattr(forward, "sender_id", None)
            if forward_sender_id:
                return int(forward_sender_id)

        args = (event.raw_text or "").split()
        if len(args) > 1:
            raw = args[1].strip()
            if raw.startswith("@"):
                try:
                    user = await client.get_entity(raw)
                    if isinstance(user, User):
                        await asyncio.to_thread(db.remember_user, user.id, user.username, user.first_name)
                    return int(user.id)
                except (ValueError, RPCError):
                    resolved = await asyncio.to_thread(db.resolve_username, raw)
                    return int(resolved) if resolved else None
            if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
                return int(raw)
    except (TypeError, ValueError, RPCError) as exc:
        logger.debug("Erro ao extrair alvo: %s", exc)
    except Exception as exc:
        logger.exception("Erro inesperado ao extrair alvo: %s", exc)
    return None


async def reject_moderation_target(event, target_id):
    """Informa claramente se o alvo não foi resolvido ou é realmente imune."""
    if not target_id:
        await reply_or_edit(
            event,
            "❌ Não encontrei o alvo. Responda à mensagem do usuário ou informe um ID/@username válido.",
            delete_after=DEFAULT_DELETE_AFTER,
        )
        return True
    if is_immune(target_id):
        await reply_or_edit(
            event,
            "❌ Este usuário é protegido e não pode ser punido pelo Userbot.",
            delete_after=DEFAULT_DELETE_AFTER,
        )
        return True
    return False


async def reject_fast_moderation_target(event, status_message, target_id, label="resultado da moderação"):
    """Aplica a validação de alvo sem abandonar a mensagem de status rápido."""
    if not target_id:
        await finish_fast_response(
            event,
            status_message,
            "❌ Não encontrei o alvo. Responda à mensagem do usuário ou informe um ID/@username válido.",
            label=label,
        )
        return True
    if is_immune(target_id):
        await finish_fast_response(
            event,
            status_message,
            "❌ Este usuário é protegido e não pode ser punido pelo Userbot.",
            label=label,
        )
        return True
    return False


async def get_authorization_target_and_expiry(event):
    """Resolve alvo e duração sem tratar a duração como se fosse um ID."""
    args = event.raw_text.split()[1:]
    duration = None
    duration_index = None
    for index, token in enumerate(args[:2]):
        parsed = parse_duration_token(token)
        if parsed is not None:
            duration = parsed
            duration_index = index
            break

    target_token = next((token for index, token in enumerate(args) if index != duration_index), None)
    try:
        reply = await event.get_reply_message()
        if reply and reply.sender_id:
            return int(reply.sender_id), duration

        if not target_token:
            return None, duration
        raw = target_token.strip()
        if raw.startswith("@"):
            try:
                user = await client.get_entity(raw)
                if isinstance(user, User):
                    await asyncio.to_thread(db.remember_user, user.id, user.username, user.first_name)
                return int(user.id), duration
            except (ValueError, RPCError):
                    return await asyncio.to_thread(db.resolve_username, raw), duration
        if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
            return int(raw), duration
    except Exception as exc:
        logger.error("Erro ao extrair alvo e duração da autorização: %s", exc)
    return None, duration


def format_timestamp(value, fmt="%d/%m/%Y %H:%M"):
    try:
        timestamp = int(value or 0)
        return datetime.fromtimestamp(timestamp).strftime(fmt) if timestamp > 0 else "-"
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


def format_duration(seconds):
    total = max(0, int(seconds or 0))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}min")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_session_state():
    try:
        filename = getattr(client.session, "filename", None)
        if filename and Path(str(filename)).exists():
            return "✅ arquivo de sessão presente"
    except (OSError, TypeError, ValueError):
        pass
    return "⚠️ arquivo de sessão não localizado"


def get_cache_counts():
    return {
        "global_blacklist": len(cache.global_blacklist),
        "local_blacklist": sum(len(users) for users in cache.local_blacklist.values()),
        "local_banperm": sum(len(users) for users in cache.local_banperm.values()),
        "shadow": len(cache.shadow_ban),
        "authorized": len(cache.authorized_users),
        "antiblack_chats": len(cache.antiblack_chats),
        "locked_chats": len(cache.locked_chats),
    }


def format_bytes(value):
    size = float(max(0, value or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


async def get_chat_permission_health(chat_id):
    if chat_id is None:
        return "⚪ não aplicável fora de chats"
    try:
        permissions = await client.get_permissions(chat_id, "me")
        required = {
            "apagar mensagens": "delete_messages",
            "banir/restringir": "ban_users",
            "gerenciar informações": "change_info",
        }
        missing = [label for label, attribute in required.items() if not getattr(permissions, attribute, False)]
        if not missing:
            return "✅ permissões principais disponíveis"
        return "⚠️ ausentes: " + ", ".join(missing)
    except (RPCError, ValueError) as exc:
        logger.debug("Não foi possível verificar permissões no chat %s: %s", chat_id, exc)
        return "⚠️ não foi possível consultar as permissões"
    except Exception as exc:
        logger.debug("Falha inesperada ao verificar permissões no chat %s: %s", chat_id, exc)
        return "⚠️ não foi possível consultar as permissões"


def parse_duration_token(token):
    token = str(token or "").strip().lower()
    if token in {"perm", "permanent", "permanente"}:
        return None
    match = re.fullmatch(r"(\d+)(s|m|h|d|w)", token)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    seconds = amount * multiplier
    if seconds < MIN_DURATION_SECONDS or seconds > MAX_DURATION_SECONDS:
        return None
    return seconds


def parse_moderation_options(event, allow_purge=False):
    args = (event.raw_text or "").split()
    start = 1 if event.is_reply else 2
    duration = None
    purge_limit = None
    reason_tokens = []
    index = start
    while index < len(args):
        token = args[index]
        lower = token.lower()
        if allow_purge and lower == "--purge":
            if index + 1 < len(args) and args[index + 1].isdigit():
                purge_limit = max(MIN_PURGE_LIMIT, min(int(args[index + 1]), MAX_PURGE_LIMIT))
                index += 2
                continue
        if allow_purge and lower.startswith("--purge=") and lower[8:].isdigit():
            purge_limit = max(MIN_PURGE_LIMIT, min(int(lower[8:]), MAX_PURGE_LIMIT))
            index += 1
            continue
        if allow_purge and lower == "--include-pinned":
            index += 1
            continue
        parsed_duration = parse_duration_token(token)
        if parsed_duration is not None or lower in {"perm", "permanent", "permanente"}:
            duration = parsed_duration
        else:
            reason_tokens.append(token)
        index += 1
    return duration, purge_limit, (" ".join(reason_tokens).strip() or None)


def get_reason_from_event(event):
    return parse_moderation_options(event, allow_purge=True)[2]


def telegram_datetime(timestamp):
    """Retorna datetime UTC para a serialização de prazos do Telethon."""
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)


def duration_label(seconds):
    if seconds is None:
        return "permanente"
    seconds = int(seconds)
    for suffix, divisor in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= divisor and seconds % divisor == 0:
            return f"{seconds // divisor}{suffix}"
    return f"{seconds}s"


def include_pinned_requested(event):
    return "--include-pinned" in (event.raw_text or "").lower().split()


async def purge_target_messages(chat_id, target_id, limit, include_pinned=False):
    message_ids = []
    # A proteção de mensagens fixadas pode fazer a primeira janela conter
    # menos itens úteis; ampliar a busca evita relatar falsamente que não há
    # mensagens antigas do alvo.
    scan_limit = MAX_HISTORY_SCAN
    async for msg in client.iter_messages(chat_id, limit=scan_limit, from_user=target_id):
        if getattr(msg, "pinned", False) and not include_pinned:
            continue
        message_ids.append(msg.id)
        if len(message_ids) >= int(limit):
            break
    return await delete_message_ids_safely(chat_id, message_ids)


async def temporary_expiry_loop():
    while True:
        try:
            await asyncio.sleep(EXPIRATION_CHECK_INTERVAL)
            now = int(time.time())
            await asyncio.to_thread(db.expire_authorized, now)
            await asyncio.to_thread(db.expire_local_blacklist, now)
            expired_local_bans = await asyncio.to_thread(db.get_expired_local_banperm, now)
            expired_global = await asyncio.to_thread(db.get_expired_global_blacklist, now)
            await asyncio.to_thread(db.expire_shadow, now)
            for row in expired_local_bans:
                try:
                    record = await asyncio.to_thread(db.get_local_banperm_record, row["chat_id"], row["user_id"])
                    if record is None:
                        logger.error("Falha ao ler snapshot do banperm expirado; será tentado novamente: %s/%s", row["chat_id"], row["user_id"])
                        continue
                    snapshot = record.get("previous_permissions")
                    await restore_permission_snapshot(row["chat_id"], row["user_id"], snapshot, "banperm")
                except Exception as exc:
                    logger.debug("Falha ao restaurar banperm expirado; será tentado novamente: %s", exc)
                else:
                    if not await asyncio.to_thread(db.remove_local_banperm, row["chat_id"], row["user_id"]):
                        logger.error("Banperm expirado restaurado, mas não removido do banco: %s/%s", row["chat_id"], row["user_id"])
            for row in expired_global:
                row_type = str(row["type"] or "").lower()
                if row_type != "ban":
                    await asyncio.to_thread(db.remove_global_blacklist, row["user_id"])
                    continue
                _attempted, failures = await restore_global_ban(row["user_id"])
                # Mantém a punição global quando algum chat ainda não foi
                # restaurado, permitindo nova tentativa no próximo ciclo.
                if failures == 0:
                    if await asyncio.to_thread(db.remove_global_blacklist, row["user_id"]):
                        if await asyncio.to_thread(db.clear_global_ban_snapshots, row["user_id"]) < 0:
                            logger.error("Blacklist global removida, mas snapshots não puderam ser limpos: %s", row["user_id"])
                    else:
                        logger.error("Ban global restaurado, mas o registro não pôde ser removido: %s", row["user_id"])
            for row in await asyncio.to_thread(db.get_expired_punishments, now):
                restored = False
                try:
                    action = str(row["action"] or "").lower()
                    if action in {"ban", "banperm", "mute", "quarantine"}:
                        await restore_permission_snapshot(
                            row["chat_id"], row["user_id"], row["previous_permissions"], action
                        )
                    else:
                        logger.error("Ação de punição temporária desconhecida; mantendo registro %s: %r", row["id"], row["action"])
                        continue
                    restored = True
                except Exception as exc:
                    logger.debug("Falha ao expirar punição %s; será tentado novamente: %s", row["id"], exc)
                if restored and not await asyncio.to_thread(db.remove_temporary_punishment, row["id"]):
                    logger.error("Punição %s restaurada, mas não removida do banco; será reprocessada", row["id"])
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Erro no ciclo de expiração: %s", exc)


async def maintenance_guard(event):
    if not cache.maintenance_enabled:
        return False
    if is_owner(event.sender_id):
        return False
    await delete_command_safely(event)
    return True


def parse_purge_limit(event, default=50):
    args = (event.raw_text or "").split()[1:]
    values = [int(arg) for arg in args if arg.isdigit()]
    if not values:
        return default, None
    value = values[0]
    if value < MIN_PURGE_LIMIT or value > MAX_PURGE_LIMIT:
        return None, f"❌ A quantidade deve estar entre {MIN_PURGE_LIMIT} e {MAX_PURGE_LIMIT}."
    return value, None


def parse_purgeall_limit(event, default=100):
    """Valida o limite do purgeall e evita uma limpeza ilimitada acidental."""
    args = (event.raw_text or "").split()[1:]
    if not args:
        return default, None
    try:
        value = int(args[0])
    except (TypeError, ValueError):
        return None, f"❌ Use <code>.jtpurgeall {PURGEALL_MIN_LIMIT}-{PURGEALL_MAX_LIMIT}</code>."
    if value < PURGEALL_MIN_LIMIT or value > PURGEALL_MAX_LIMIT:
        return None, f"❌ A quantidade deve estar entre {PURGEALL_MIN_LIMIT} e {PURGEALL_MAX_LIMIT}."
    return value, None


async def delete_message_safely(message, label="mensagem"):
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except Exception as exc:
        logger.debug(f"Não foi possível apagar {label}: {exc}")
        return False


async def delete_command_safely(event):
    return await delete_message_safely(event, "mensagem de comando")


async def resolve_message_for_delete(event):
    """Resolve a mensagem respondida ou um ID explícito sem aceitar alvos ambíguos."""
    try:
        if getattr(event, "is_reply", False):
            message = await event.get_reply_message()
            return message if message is not None else None
        args = (event.raw_text or "").split()
        if len(args) < 2 or not args[1].isdigit():
            return None
        message = await client.get_messages(event.chat_id, ids=int(args[1]))
        if isinstance(message, (list, tuple)):
            return message[0] if message else None
        return message
    except (RPCError, ValueError, TypeError) as exc:
        logger.debug("Falha ao resolver mensagem para exclusão: %s", exc)
        return None
    except Exception as exc:
        logger.debug("Falha inesperada ao resolver mensagem para exclusão: %s", exc)
        return None


async def delete_message_ids_safely(chat_id, message_ids, batch_size=100):
    """Apaga mensagens em lotes e usa fallback individual quando necessário."""
    ids = [int(message_id) for message_id in message_ids if message_id]
    deleted = 0
    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        try:
            await client.delete_messages(chat_id, batch)
            deleted += len(batch)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
            try:
                await client.delete_messages(chat_id, batch)
                deleted += len(batch)
            except Exception as retry_exc:
                logger.error(f"Falha ao apagar lote após FloodWait: {retry_exc}")
        except Exception as batch_exc:
            logger.debug(f"Falha no lote de exclusão; usando fallback: {batch_exc}")
            for message_id in batch:
                try:
                    await client.delete_messages(chat_id, message_id)
                    deleted += 1
                except Exception as item_exc:
                    logger.debug(f"Não foi possível apagar mensagem {message_id}: {item_exc}")
    return deleted


async def log_deleted_in_background(chat_id, user_id, content, reason, admin_id=None):
    """Compatibilidade para chamadas antigas, encaminhando ao buffer."""
    try:
        queue_audit_log(chat_id, user_id, content, reason, admin_id=admin_id)
    except Exception as exc:
        logger.error(f"Falha ao enfileirar log assíncrono: {exc}")


async def apply_security_restriction(chat_id, user_id):
    """Aplica a restrição secundária sem atrasar a exclusão da mensagem."""
    try:
        await client.edit_permissions(chat_id, user_id, view_messages=False)
    except UserAdminInvalidError:
        pass
    except Exception as permission_exc:
        logger.debug(f"Não foi possível aplicar restrição adicional: {permission_exc}")


async def delete_security_message(event, chat_id, user_id, content_text, reason):
    """Exclui pela fila híbrida; auditoria e restrições seguem em segundo plano."""
    try:
        # input_chat evita resolução adicional da entidade no primeiro RPC.
        delete_entity = getattr(event.message, "input_chat", None) or chat_id
        await security_delete_queue.submit(chat_id, delete_entity, event.id)
    except Exception as delete_exc:
        logger.error(f"Erro ao encaminhar mensagem para exclusão: {delete_exc}")
    finally:
        queue_audit_log(chat_id, user_id, content_text, reason)

    if reason in ("Global Ban", "Local BanPerm"):
        schedule_background(apply_security_restriction(chat_id, user_id), "security-restriction")


_response_cleanup_tasks = set()


async def _cleanup_response_later(message, event, delay, label):
    try:
        await asyncio.sleep(max(0, float(delay)))
        same_message = (
            message is not None
            and getattr(message, "id", None) == getattr(event, "id", None)
        )
        if message is not None and message is not event and not same_message:
            await delete_message_safely(message, label)
        await delete_command_safely(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Falha na limpeza assíncrona de resposta: %s", exc)


def schedule_response_cleanup(message, event, delay, label="resposta automática"):
    """Agenda a exclusão sem bloquear o handler por DEFAULT_DELETE_AFTER segundos."""
    if not delay:
        return
    task = schedule_background(_cleanup_response_later(message, event, delay, label), "response-cleanup")
    _response_cleanup_tasks.add(task)
    task.add_done_callback(_response_cleanup_tasks.discard)


async def _edit_response_now(message, text):
    if message is None:
        return False
    try:
        await message.edit(text, parse_mode="html")
        return True
    except MessageNotModifiedError:
        return True
    except Exception as exc:
        logger.warning("Resposta HTML falhou; tentando texto simples: %s", exc)
        try:
            await message.edit(text, parse_mode=None)
            return True
        except MessageNotModifiedError:
            return True
        except Exception as fallback_exc:
            logger.error("Erro ao editar resposta: %s", fallback_exc)
            return False


async def reply_or_edit(event, text, delete_after=DEFAULT_DELETE_AFTER):
    """Responde sem bloquear o handler durante o período de autoexclusão."""
    command_metrics.start(event)
    msg = None
    try:
        if event.out:
            msg = await event.edit(text, parse_mode="html")
        else:
            msg = await event.reply(text, parse_mode="html")
    except MessageNotModifiedError:
        # O comando já contém exatamente o texto solicitado; ainda assim,
        # ele deve seguir o ciclo normal de autoexclusão.
        msg = event
    except Exception as exc:
        logger.warning(f"Resposta HTML falhou; tentando texto simples: {exc}")
        try:
            if event.out:
                msg = await event.edit(text, parse_mode=None)
            else:
                msg = await event.reply(text, parse_mode=None)
        except MessageNotModifiedError:
            msg = event
        except Exception as fallback_exc:
            logger.error(f"Erro ao enviar/editar resposta: {fallback_exc}")

    command_metrics.finish(event, success=msg is not None)
    if msg is not None:
        schedule_response_cleanup(msg, event, delete_after)
    elif delete_after:
        schedule_response_cleanup(None, event, delete_after)
    return msg


async def begin_fast_response(event, text, label="status de moderação"):
    """Mostra o processamento imediatamente, editando o comando quando possível."""
    command_metrics.start(event)
    try:
        if event.out:
            return await event.edit(text, parse_mode="html")
        return await event.respond(text, parse_mode="html")
    except MessageNotModifiedError:
        return event
    except Exception as exc:
        logger.warning("Falha ao mostrar %s: %s", label, exc)
        try:
            if event.out:
                return await event.edit(text, parse_mode=None)
            return await event.respond(text, parse_mode=None)
        except Exception as fallback_exc:
            logger.error("Falha no fallback de %s: %s", label, fallback_exc)
            return None


async def finish_fast_response(event, status_message, text, delete_after=DEFAULT_DELETE_AFTER, label="resposta de moderação"):
    """Edita a confirmação e agenda sua exclusão sem segurar o loop assíncrono."""
    if status_message is None:
        return await reply_or_edit(event, text, delete_after=delete_after)
    edited = await _edit_response_now(status_message, text)
    command_metrics.finish(event, success=edited)
    if not edited:
        # Evita deixar o usuário sem resultado quando a edição da mensagem de
        # status falhar; a resposta alternativa também será autoexcluída.
        await reply_or_edit(event, text, delete_after=delete_after)
        schedule_response_cleanup(status_message, event, delete_after, label)
        return None
    schedule_response_cleanup(status_message, event, delete_after, label)
    return status_message


async def send_status_safely(event, text, label="mensagem de status"):
    try:
        return await event.respond(text)
    except Exception as exc:
        logger.warning("Falha ao enviar %s: %s", label, exc)
        await delete_command_safely(event)
        return None


async def edit_and_delete_safely(message, text, delete_after=DEFAULT_DELETE_AFTER, label="mensagem de status"):
    if message is None:
        return False
    edited = False
    try:
        await message.edit(text, parse_mode="html")
        edited = True
    except MessageNotModifiedError:
        edited = True
    except Exception as exc:
        logger.warning("Falha ao editar %s: %s", label, exc)
    # Não retenha o handler por vários segundos: a resposta e o comando
    # serão removidos pelo agendador compartilhado em segundo plano.
    schedule_response_cleanup(message, message, delete_after, label)
    return edited


async def send_broadcast_payload(chat_id, reply, text=None):
    if reply is None:
        await client.send_message(chat_id, text or "")
    elif reply.media:
        caption = text if text is not None else (reply.raw_text or None)
        await client.send_file(chat_id, reply.media, caption=caption)
    else:
        await client.send_message(chat_id, text if text is not None else (reply.raw_text or ""))

# --- REGISTRO DE CHATS E USUÁRIOS ---
registered_chat_ids = set()
registered_user_ids = set()
_registering_chat_ids = set()
_registering_user_ids = set()


def _event_entity_without_rpc(event, attribute):
    """Obtém a entidade anexada ao update sem chamar get_entity/get_dialogs."""
    try:
        return getattr(event, attribute, None)
    except Exception:
        return None


async def register_chat_and_user(event):
    chat_id = event.chat_id
    if not chat_id:
        return
    try:
        if chat_id not in registered_chat_ids:
            entity = _event_entity_without_rpc(event, "chat")
            if event.is_group:
                chat_type = "group"
            elif event.is_channel:
                chat_type = "channel"
            else:
                chat_type = "private"
            title = (
                getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or getattr(event, "chat_title", None)
                or ""
            )
            # O registro é deliberadamente local e não resolve entidades pela rede.
            db.register_chat(chat_id, title, chat_type)
            registered_chat_ids.add(chat_id)

        sender_id = event.sender_id
        if sender_id and sender_id not in registered_user_ids:
            # NewMessage normalmente já carrega o remetente no update. Se não
            # estiver disponível, persiste apenas o ID; nunca faz get_sender()
            # automaticamente, pois essa resolução pode provocar GetDialogsRequest.
            sender = _event_entity_without_rpc(event, "sender")
            registered_user_ids.add(sender_id)
            if isinstance(sender, User):
                db.remember_user(sender.id, sender.username, sender.first_name)
            else:
                db.remember_user(sender_id, None, None)
    except Exception as exc:
        logger.debug("Falha não crítica ao registrar chat/usuário: %s", exc)
    finally:
        _registering_chat_ids.discard(int(chat_id))
        if event.sender_id:
            _registering_user_ids.discard(int(event.sender_id))


@client.on(events.NewMessage)
async def chat_registry(event):
    if not event.chat_id or (event.raw_text or "").startswith("."):
        return
    sender_id = event.sender_id
    if sender_id and not is_immune(sender_id):
        chat_id = event.chat_id
        if (
            sender_id in cache.global_blacklist
            or sender_id in cache.shadow_ban
            or sender_id in cache.local_blacklist.get(chat_id, ())
            or sender_id in cache.local_banperm.get(chat_id, ())
        ):
            return
    # Registro é secundário: nunca deve bloquear o filtro de exclusão. A tarefa
    # só é criada quando realmente há algo novo para registrar.
    needs_chat = event.chat_id not in registered_chat_ids and event.chat_id not in _registering_chat_ids
    needs_user = bool(sender_id and sender_id not in registered_user_ids and sender_id not in _registering_user_ids)
    if not needs_chat and not needs_user:
        return
    if needs_chat:
        _registering_chat_ids.add(int(event.chat_id))
    if needs_user:
        _registering_user_ids.add(int(sender_id))
    schedule_background(register_chat_and_user(event), "chat-registry")


# --- SISTEMA ANTIBLACK (AUTO-REPOSTE FÊNIX) ---
recent_sent_messages = {}
MAX_RECENT_SENT_MESSAGES = 5000

@client.on(events.NewMessage(outgoing=True))
async def antiblack_tracker(event):
    if not event.is_group and not event.is_channel:
        return
    chat_id = event.chat_id
    if chat_id not in cache.antiblack_chats or (event.raw_text or "").startswith("."):
        return
    recent_sent_messages[event.id] = {
        "chat_id": chat_id,
        "message": event.message,
        "time": time.time(),
    }
    cutoff = time.time() - 10
    for msg_id, data in list(recent_sent_messages.items()):
        if data["time"] < cutoff or len(recent_sent_messages) > MAX_RECENT_SENT_MESSAGES:
            recent_sent_messages.pop(msg_id, None)

@client.on(events.MessageDeleted())
async def antiblack_resender(event):
    for deleted_id in event.deleted_ids:
        data = recent_sent_messages.pop(deleted_id, None)
        if not data or data["chat_id"] != event.chat_id or time.time() - data["time"] >= 10:
            continue
        try:
            message = data["message"]
            if message.media:
                await client.send_file(event.chat_id, message.media, caption=message.text or None)
            elif message.text:
                await client.send_message(event.chat_id, message.text)
        except Exception as exc:
            logger.error(f"Erro no auto-reposte antiblack: {exc}")

# --- FILTRO DE SEGURANÇA GLOBAL & SHADOW BAN ---
@client.on(events.NewMessage(incoming=True))
async def global_security_filter(event):
    if not event.is_group and not event.is_channel:
        return

    user_id = event.sender_id
    if not user_id or is_immune(user_id):
        return

    chat_id = event.chat_id
    reason = None
    
    if user_id in cache.global_blacklist:
        reason = "Global Ban" if cache.global_blacklist_types.get(user_id) == "ban" else "Global Blacklist"
    elif user_id in cache.shadow_ban: reason = "Shadow Ban"
    elif user_id in cache.local_blacklist.get(chat_id, ()): reason = "Local Blacklist"
    elif user_id in cache.local_banperm.get(chat_id, ()): reason = "Local BanPerm"

    if reason:
        content_text = event.text or "[Mídia / Sticker / GIF]"
        # O RPC de exclusão começa neste mesmo handler: não há uma tarefa
        # intermediária aguardando a próxima rodada do event loop.
        await delete_security_message(event, chat_id, user_id, content_text, reason)
        raise events.StopPropagation

@client.on(events.NewMessage(incoming=True))
async def chat_lock_filter(event):
    """Camada redundante: o Telegram bloqueia no servidor; este filtro cobre eventos residuais."""
    if not event.chat_id or not (event.is_group or event.is_channel):
        return
    if int(event.chat_id) not in cache.locked_chats:
        return
    user_id = event.sender_id
    if not user_id or is_immune(user_id):
        return
    # O lock restringe mensagens comuns, mas não deve atrasar ou bloquear
    # comandos de uma conta já autorizada pelo Userbot.
    if (event.raw_text or "").startswith(".") and is_authorized(user_id):
        return
    admin_state = await is_chat_admin(event.chat_id, user_id, wait_for_rpc=False)
    if admin_state is not False:
        return
    try:
        await delete_security_message(
            event, event.chat_id, user_id,
            event.text or "[mensagem bloqueada durante o lock]",
            "Chat Lock",
        )
    except Exception as exc:
        logger.debug("Falha no filtro redundante de lock: %s", exc)
    raise events.StopPropagation


async def get_settings_async(chat_id):
    """Obtém settings do cache; a primeira criação é deslocada para uma thread."""
    chat_id = int(chat_id)
    if chat_id in cache.settings_loaded:
        return dict(cache.settings.get(chat_id, {}))
    try:
        return await asyncio.to_thread(db.get_settings, chat_id)
    except Exception as exc:
        logger.debug("Falha ao carregar settings do chat %s: %s", chat_id, exc)
        return {}


def _record_is_active(record, now=None):
    """Retorna True somente para um registro presente e ainda vigente."""
    if not record:
        return False
    expires_at = record.get("expires_at") if isinstance(record, dict) else None
    if expires_at in (None, ""):
        return True
    try:
        return int(expires_at) > int(now or time.time())
    except (TypeError, ValueError, OverflowError):
        return False


def _active_state_suffix(record):
    """Formata o prazo de um estado ativo para respostas idempotentes."""
    if not record or record.get("expires_at") in (None, ""):
        return "permanentemente"
    try:
        return f"até <b>{format_timestamp(int(record['expires_at']))}</b>"
    except (TypeError, ValueError, OverflowError):
        return "com prazo registrado"


async def get_active_temporary_punishment(chat_id, user_id, action):
    record = await asyncio.to_thread(db.get_temporary_punishment, chat_id, user_id, action)
    if record is None:
        return None, True
    if not _record_is_active(record):
        return {}, False
    return record, False


async def get_telegram_restriction_state(chat_id, user_id):
    """Lê restrições atuais apenas sob demanda de um comando de moderação."""
    if not callable(getattr(client, "get_permissions", None)):
        # Clientes falsos dos testes não precisam bloquear o caminho de aplicação;
        # o cliente Telethon real sempre expõe este método.
        return {"mute": False, "ban": False}, False
    try:
        permissions = await client.get_permissions(chat_id, user_id)
        participant = getattr(permissions, "participant", permissions)
        banned_rights = getattr(participant, "banned_rights", None)
        if banned_rights is None:
            banned_rights = getattr(permissions, "banned_rights", None)
        if banned_rights is None:
            return {"mute": False, "ban": False}, False
        return {
            "mute": bool(getattr(banned_rights, "send_messages", False)),
            "ban": bool(getattr(banned_rights, "view_messages", False)),
        }, False
    except (RPCError, ValueError, TypeError):
        return {"mute": False, "ban": False}, False
    except Exception as exc:
        logger.debug("Falha ao consultar restrições de %s/%s: %s", chat_id, user_id, exc)
        return {}, True


def _snapshot_allows(snapshot, field, default=True):
    """Retorna a permissão permitida registrada no snapshot, sem lançar exceção."""
    if not snapshot:
        return default
    try:
        data = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        if not isinstance(data, dict):
            return default
        version = int(data.get("version", 1))
        source = data.get("permissions") if version >= PERMISSION_SNAPSHOT_VERSION else data
        if not isinstance(source, dict) or field not in source:
            return default
        value = bool(source[field])
        return value if version >= PERMISSION_SNAPSHOT_VERSION else not value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


@client.on(events.NewMessage(incoming=True))
async def antilink_filter(event):
    """Remove links de não administradores sem consultar permissões repetidamente."""
    if not event.chat_id or not (event.is_group or event.is_channel):
        return
    # Comandos não são links de usuário e não devem disparar leitura de
    # settings, whitelist ou consulta RPC de administrador.
    if (event.raw_text or "").startswith("."):
        return
    user_id = event.sender_id
    if not user_id or is_immune(user_id):
        return
    settings = await get_settings_async(event.chat_id)
    if not _setting_int(settings, "antilink", 0, 0, 1):
        return
    if not message_contains_link(event.message, event.raw_text or ""):
        return
    if user_id in cache.link_whitelist.get(int(event.chat_id), set()):
        return
    admin_state = await is_chat_admin(event.chat_id, user_id, wait_for_rpc=False)
    if admin_state is not False:
        return
    try:
        await delete_security_message(
            event, event.chat_id, user_id,
            event.text or "[link oculto ou mídia com URL]",
            "AntiLink",
        )
    except Exception as exc:
        logger.debug("Falha no filtro antilink: %s", exc)
    raise events.StopPropagation


spam_state = {}


async def apply_warning_action(chat_id, user_id, action, duration, reason, admin_id):
    expires_at = int(time.time()) + int(duration)
    action = str(action or "mute").lower()
    snapshot = await capture_permission_snapshot(chat_id, user_id)
    if action == "ban":
        await client.edit_permissions(chat_id, user_id, view_messages=False)
    elif action == "quarantine":
        await client.edit_permissions(chat_id, user_id, send_messages=False, send_media=False)
    else:
        await client.edit_permissions(chat_id, user_id, send_messages=False)
        action = "mute"
    if await asyncio.to_thread(
        db.add_temporary_punishment,
        chat_id, user_id, action, expires_at, reason, admin_id,
        previous_permissions=snapshot,
    ):
        return True
    try:
        await restore_permission_snapshot(chat_id, user_id, snapshot, action)
    except Exception as restore_exc:
        logger.error("Falha ao desfazer punição automática não persistida em %s/%s: %s", chat_id, user_id, restore_exc)
    raise RuntimeError("não foi possível persistir o prazo da punição automática")


_antispam_tasks = set()


def _schedule_antispam_task(coroutine):
    task = schedule_background(coroutine, "antispam-action")
    _antispam_tasks.add(task)
    task.add_done_callback(_antispam_tasks.discard)
    return task


async def process_antispam_warning(chat_id, user_id, settings, reason):
    """Persiste advertência e aplica a consequência sem bloquear o dispatcher."""
    try:
        count = await asyncio.to_thread(db.add_warning, chat_id, user_id)
        if count is None:
            logger.error("Não foi possível persistir advertência automática em %s/%s", chat_id, user_id)
            return
        threshold = _setting_int(settings, "warn_threshold", 3, 1, 20)
        if count < threshold:
            return
        action = str((settings or {}).get("warn_action", "mute")).lower()
        if action not in {"mute", "ban"}:
            action = "mute"
        duration = _setting_int(settings, "warn_duration", 600, 60, MAX_DURATION_SECONDS)
        try:
            await apply_warning_action(chat_id, user_id, action, duration, reason, OWNER_ID)
            await asyncio.to_thread(db.clear_warnings, chat_id, user_id)
        except Exception as exc:
            logger.debug("Falha ao aplicar ação após advertências: %s", exc)
    except Exception as exc:
        logger.debug("Falha ao persistir advertência antispam: %s", exc)


async def process_antispam_quarantine(chat_id, user_id, duration, reason):
    """Aplica a quarentena após a mensagem já ter sido encaminhada para exclusão."""
    try:
        await apply_warning_action(chat_id, user_id, "quarantine", duration, reason, OWNER_ID)
    except Exception as exc:
        logger.debug("Falha ao aplicar quarentena antispam em %s/%s: %s", chat_id, user_id, exc)


def schedule_antispam_warning(chat_id, user_id, settings, reason):
    return _schedule_antispam_task(process_antispam_warning(chat_id, user_id, settings, reason))


def schedule_antispam_quarantine(chat_id, user_id, duration, reason):
    return _schedule_antispam_task(process_antispam_quarantine(chat_id, user_id, duration, reason))


@client.on(events.NewMessage(incoming=True))
async def antispam_filter(event):
    """Detecta padrões combinados com limites defensivos e relógio monotônico."""
    if not event.chat_id or not (event.is_group or event.is_channel) or (event.raw_text or "").startswith("."):
        return
    user_id = event.sender_id
    if not user_id or is_immune(user_id):
        return
    # Administradores não entram no antispam: o antilink também possui a
    # mesma exceção e não deve haver punição automática de moderadores.
    admin_state = await is_chat_admin(event.chat_id, user_id, wait_for_rpc=False)
    if admin_state is not False:
        return
    settings = await get_settings_async(event.chat_id)
    if not _setting_int(settings, "antispam", 1, 0, 1):
        return

    # time.monotonic() evita que ajustes de relógio do Android distorçam a janela.
    now = time.monotonic()
    key = (int(event.chat_id), int(user_id))
    state = spam_state.setdefault(
        key,
        {
            "times": deque(),
            "fingerprints": deque(),
            "links": deque(),
            "media": deque(),
            "last_action_at": 0.0,
            "last_seen": now,
        },
    )
    state["last_seen"] = now
    if len(spam_state) > SPAM_STATE_MAX_USERS:
        oldest_key, _ = min(
            spam_state.items(),
            key=lambda item: float(item[1].get("last_seen", 0.0)),
        )
        if oldest_key != key:
            spam_state.pop(oldest_key, None)

    window = _setting_int(settings, "spam_window", 10, 5, 120)
    cutoff = now - window
    for name in ("times", "fingerprints", "links", "media"):
        while state[name] and state[name][0][0] < cutoff:
            state[name].popleft()

    text = (event.raw_text or "").strip()
    fingerprint = hashlib.sha1(
        re.sub(r"\s+", " ", text.casefold()).encode("utf-8", "ignore")
    ).hexdigest() if text else ""
    link_count = count_message_links(event.message, text)
    has_media = bool(getattr(event.message, "media", None))
    state["times"].append((now, event.id))
    if fingerprint:
        state["fingerprints"].append((now, fingerprint))
    for _ in range(min(link_count, 20)):
        state["links"].append((now, True))
    if has_media:
        state["media"].append((now, True))

    frequency_limit = _setting_int(settings, "spam_limit", 6, 2, 100)
    duplicate_limit = _setting_int(settings, "duplicate_limit", 3, 2, 20)
    link_limit = _setting_int(settings, "link_limit", 3, 2, 20)
    media_limit = _setting_int(settings, "media_limit", 5, 2, 50)
    same_text = sum(1 for _, value in state["fingerprints"] if fingerprint and value == fingerprint)
    frequency_count = len(state["times"])
    link_count_window = len(state["links"])
    media_count_window = len(state["media"])

    signals = []
    score = 0
    if frequency_count > frequency_limit:
        excess = frequency_count - frequency_limit
        score += min(4, 2 + excess // max(1, frequency_limit // 2))
        signals.append(f"frequência ({frequency_count}/{frequency_limit})")
    if same_text >= duplicate_limit:
        score += min(4, 2 + max(0, same_text - duplicate_limit))
        signals.append(f"duplicação ({same_text})")
    if link_count_window >= link_limit:
        score += min(4, 2 + max(0, link_count_window - link_limit))
        signals.append(f"links repetidos ({link_count_window})")
    if media_count_window >= media_limit:
        score += min(4, 2 + max(0, media_count_window - media_limit))
        signals.append(f"mídia em rajada ({media_count_window})")

    score_threshold = _setting_int(settings, "spam_score_threshold", 4, 2, 12)
    quarantine_threshold = max(
        score_threshold,
        _setting_int(settings, "quarantine_score_threshold", 6, 2, 16),
    )
    if not signals or score < score_threshold:
        return

    # Uma única mensagem nunca é suficiente. Exigimos dois sinais independentes
    # ou uma rajada claramente anormal antes de excluir e advertir.
    strong_pattern = (
        len(signals) >= 2
        or frequency_count >= frequency_limit * 2
        or same_text >= duplicate_limit + 2
        or media_count_window >= media_limit * 2
    )
    if not strong_pattern:
        return
    reason = f"Antispam ({score} pontos): " + ", ".join(signals)
    if now - float(state.get("last_action_at", 0.0)) < SPAM_ACTION_COOLDOWN:
        return

    if _setting_int(settings, "quarantine_enabled", 0, 0, 1):
        if score < quarantine_threshold:
            logger.debug("Sinal antispam abaixo da quarentena em %s/%s: %s", event.chat_id, user_id, reason)
            return
        duration = _setting_int(settings, "quarantine_duration", 600, 60, MAX_DURATION_SECONDS)
        try:
            await delete_security_message(event, event.chat_id, user_id, event.text or "[mídia]", reason)
            schedule_antispam_quarantine(event.chat_id, user_id, duration, reason)
            state["last_action_at"] = now
            for name in ("times", "fingerprints", "links", "media"):
                state[name].clear()
        except Exception as exc:
            logger.debug("Falha ao encaminhar a quarentena antispam: %s", exc)
        return

    try:
        await delete_security_message(event, event.chat_id, user_id, event.text or "[mídia]", reason)
        # A exclusão é aguardada para preservar a prioridade de segurança; a
        # gravação SQLite e a eventual punição seguem fora do hot path.
        schedule_antispam_warning(event.chat_id, user_id, settings, reason)
        state["last_action_at"] = now
        for name in ("times", "fingerprints", "links", "media"):
            state[name].clear()
    except Exception as exc:
        logger.debug("Falha no filtro antispam: %s", exc)


# --- COMANDOS ---

@client.on(events.NewMessage(incoming=True, pattern=r'^\.jt'))
async def maintenance_filter(event):
    if not cache.maintenance_enabled or is_owner(event.sender_id):
        return
    command = (event.raw_text or "").split(maxsplit=1)[0].lower()
    if command in {".maintenance", ".status", ".health", ".latency"}:
        return
    await delete_command_safely(event)
    raise events.StopPropagation


@client.on(events.NewMessage(pattern=r'^\.maintenance(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_maintenance(event):
    args = (event.raw_text or "").split()
    if len(args) < 2 or args[1].lower() not in {"on", "off", "1", "0"}:
        await reply_or_edit(event, "Use <code>.maintenance on</code> ou <code>.maintenance off</code>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    enabled = args[1].lower() in {"on", "1"}
    if bool(cache.maintenance_enabled) == enabled:
        await reply_or_edit(event, f"ℹ️ O modo manutenção já está <b>{'ATIVADO' if enabled else 'DESATIVADO'}</b>; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.set_maintenance, enabled):
        await reply_or_edit(event, "❌ Não foi possível atualizar o modo manutenção no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    await reply_or_edit(event, f"🛠️ Modo manutenção <b>{'ATIVADO' if enabled else 'DESATIVADO'}</b>.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.lock(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_lock(event):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, "❌ O lock só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await can_manage_chat(event):
        await reply_or_edit(event, "❌ Somente administradores deste grupo podem usar o lock.", delete_after=DEFAULT_DELETE_AFTER)
        return
    state = await asyncio.to_thread(db.get_chat_lock, event.chat_id)
    if state is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o estado do lock no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if int(state.get("locked", 0)):
        await reply_or_edit(event, "ℹ️ Este grupo já está bloqueado. Apenas administradores podem enviar mensagens.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        snapshot = await capture_chat_default_rights(event.chat_id)
        await apply_chat_default_rights(event.chat_id, locked_chat_rights())
        if not await asyncio.to_thread(db.set_chat_lock, event.chat_id, snapshot):
            try:
                await apply_chat_default_rights(event.chat_id, deserialize_chat_default_rights(snapshot))
            except Exception as rollback_exc:
                logger.error("Falha ao desfazer lock não persistido em %s: %s", event.chat_id, rollback_exc)
            await reply_or_edit(event, "❌ O grupo foi bloqueado no Telegram, mas o estado não pôde ser salvo; a ação foi revertida quando possível.", delete_after=DEFAULT_DELETE_AFTER)
            return
        queue_audit_log(event.chat_id, event.sender_id, "Ação: Lock", "Mensagens restritas para membros", admin_id=event.sender_id)
        await reply_or_edit(event, "🔒 <b>Grupo bloqueado.</b> Somente administradores poderão enviar mensagens.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Não tenho permissão de administrador para alterar as permissões padrão deste grupo.", delete_after=DEFAULT_DELETE_AFTER)
    except RPCError as exc:
        logger.warning("Falha RPC ao bloquear o chat %s: %s", event.chat_id, exc)
        await reply_or_edit(event, "❌ O Telegram recusou o lock. Confirme que sou administrador com permissão para restringir membros.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as exc:
        logger.error("Erro inesperado no .lock para %s: %s", event.chat_id, exc)
        await reply_or_edit(event, "❌ Não foi possível bloquear o grupo com segurança.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.unlock(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unlock(event):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, "❌ O unlock só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await can_manage_chat(event):
        await reply_or_edit(event, "❌ Somente administradores deste grupo podem usar o unlock.", delete_after=DEFAULT_DELETE_AFTER)
        return
    state = await asyncio.to_thread(db.get_chat_lock, event.chat_id)
    if state is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o estado do lock no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not int(state.get("locked", 0)):
        await reply_or_edit(event, "ℹ️ Este grupo já está desbloqueado.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        previous_rights = deserialize_chat_default_rights(state.get("lock_snapshot"))
        await apply_chat_default_rights(event.chat_id, previous_rights)
        if not await asyncio.to_thread(db.clear_chat_lock, event.chat_id):
            try:
                await apply_chat_default_rights(event.chat_id, locked_chat_rights())
            except Exception as rollback_exc:
                logger.critical("Falha ao restaurar lock após erro de banco em %s: %s", event.chat_id, rollback_exc)
            await reply_or_edit(event, "⚠️ As permissões foram restauradas, mas o estado não pôde ser limpo; o lock foi mantido quando possível.", delete_after=DEFAULT_DELETE_AFTER)
            return
        queue_audit_log(event.chat_id, event.sender_id, "Ação: Unlock", "Permissões padrão restauradas", admin_id=event.sender_id)
        await reply_or_edit(event, "🔓 <b>Grupo desbloqueado.</b> As permissões anteriores foram restauradas.", delete_after=DEFAULT_DELETE_AFTER)
    except ValueError:
        await reply_or_edit(event, "❌ O snapshot anterior do lock está inválido. O unlock foi interrompido para não remover restrições existentes.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Não tenho permissão de administrador para restaurar as permissões padrão deste grupo.", delete_after=DEFAULT_DELETE_AFTER)
    except RPCError as exc:
        logger.warning("Falha RPC ao desbloquear o chat %s: %s", event.chat_id, exc)
        await reply_or_edit(event, "❌ O Telegram recusou o unlock. Confirme minhas permissões de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as exc:
        logger.error("Erro inesperado no .unlock para %s: %s", event.chat_id, exc)
        await reply_or_edit(event, "❌ Não foi possível desbloquear o grupo com segurança.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.quarantine(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_quarantine(event):
    if not await require_chat_admin(event, "alterar a quarentena"):
        return
    args = (event.raw_text or "").split()
    if len(args) < 2 or args[1].lower() not in {"on", "off", "1", "0"}:
        settings = await get_settings_async(event.chat_id)
        status = bool(_setting_int(settings, "quarantine_enabled", 0, 0, 1))
        await reply_or_edit(event, f"Quarentena: <b>{'ATIVADA' if status else 'DESATIVADA'}</b>. Use <code>.quarantine on|off</code>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    enabled = args[1].lower() in {"on", "1"}
    current = bool(_setting_int(await get_settings_async(event.chat_id), "quarantine_enabled", 0, 0, 1))
    if current == enabled:
        await reply_or_edit(event, f"ℹ️ A quarentena já está <b>{'ATIVADA' if enabled else 'DESATIVADA'}</b> neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.set_setting, event.chat_id, "quarantine_enabled", int(enabled)):
        await reply_or_edit(event, "❌ Não foi possível atualizar a quarentena no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    await reply_or_edit(event, f"🛡️ Quarentena antispam <b>{'ATIVADA' if enabled else 'DESATIVADA'}</b> neste chat.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.antispam(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_antispam_new(event):
    if not await require_chat_admin(event, "alterar o antispam"):
        return
    args = (event.raw_text or "").split()
    if len(args) < 2 or args[1].lower() not in {"on", "off", "1", "0"}:
        settings = await get_settings_async(event.chat_id)
        status = bool(_setting_int(settings, "antispam", 1, 0, 1))
        await reply_or_edit(event, f"Antispam: <b>{'ATIVADO' if status else 'DESATIVADO'}</b>. Use <code>.antispam on|off</code>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    enabled = args[1].lower() in {"on", "1"}
    current = bool(_setting_int(await get_settings_async(event.chat_id), "antispam", 1, 0, 1))
    if current == enabled:
        await reply_or_edit(event, f"ℹ️ O antispam já está <b>{'ATIVADO' if enabled else 'DESATIVADO'}</b> neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.set_setting, event.chat_id, "antispam", int(enabled)):
        await reply_or_edit(event, "❌ Não foi possível atualizar o antispam no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    await reply_or_edit(event, f"🛡️ Antispam <b>{'ATIVADO' if enabled else 'DESATIVADO'}</b> neste chat.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.pinned(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_pinned(event):
    if not await require_chat_admin(event, "alterar a proteção de mensagens fixadas"):
        return
    args = (event.raw_text or "").split()
    if len(args) < 2 or args[1].lower() not in {"on", "off", "1", "0"}:
        settings = await get_settings_async(event.chat_id)
        status = bool(_setting_int(settings, "protect_pinned", 1, 0, 1))
        await reply_or_edit(event, f"Proteção de fixadas: <b>{'ATIVADA' if status else 'DESATIVADA'}</b>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    enabled = args[1].lower() in {"on", "1"}
    current = bool(_setting_int(await get_settings_async(event.chat_id), "protect_pinned", 1, 0, 1))
    if current == enabled:
        await reply_or_edit(event, f"ℹ️ A proteção de mensagens fixadas já está <b>{'ATIVADA' if enabled else 'DESATIVADA'}</b>; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.set_setting, event.chat_id, "protect_pinned", int(enabled)):
        await reply_or_edit(event, "❌ Não foi possível atualizar a proteção de fixadas no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    await reply_or_edit(event, f"📌 Proteção de mensagens fixadas <b>{'ATIVADA' if enabled else 'DESATIVADA'}</b>.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.antilink(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_antilink(event):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, "❌ O antilink só pode ser configurado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await can_manage_chat(event):
        await reply_or_edit(event, "❌ Somente administradores deste grupo podem alterar o antilink.", delete_after=DEFAULT_DELETE_AFTER)
        return
    args = (event.raw_text or "").split()
    if len(args) < 2 or args[1].lower() not in {"on", "off", "1", "0"}:
        settings = await get_settings_async(event.chat_id)
        status = bool(_setting_int(settings, "antilink", 0, 0, 1))
        await reply_or_edit(
            event,
            f"🔗 Antilink: <b>{'ATIVADO' if status else 'DESATIVADO'}</b>. Use <code>.antilink on|off</code>.",
            delete_after=DEFAULT_DELETE_AFTER,
        )
        return
    enabled = args[1].lower() in {"on", "1"}
    current = bool(_setting_int(await get_settings_async(event.chat_id), "antilink", 0, 0, 1))
    if current == enabled:
        await reply_or_edit(event, f"ℹ️ O antilink já está <b>{'ATIVADO' if enabled else 'DESATIVADO'}</b> neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.set_setting, event.chat_id, "antilink", int(enabled)):
        await reply_or_edit(event, "❌ Não foi possível atualizar o antilink no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    await reply_or_edit(
        event,
        f"🔗 Antilink <b>{'ATIVADO' if enabled else 'DESATIVADO'}</b>. Links ficam permitidos para administradores e usuários autorizados.",
        delete_after=DEFAULT_DELETE_AFTER,
    )


@client.on(events.NewMessage(pattern=r'^\.autorizarlink(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_autorizarlink(event):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, "❌ A autorização de links só pode ser configurada em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await can_manage_chat(event):
        await reply_or_edit(event, "❌ Somente administradores deste grupo podem autorizar links.", delete_after=DEFAULT_DELETE_AFTER)
        return
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Informe um usuário válido por resposta, ID ou username.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if target_id in cache.link_whitelist.get(int(event.chat_id), set()):
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        await reply_or_edit(event, f"ℹ️ {user_info} (<code>{target_id}</code>) já está autorizado a enviar links neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.add_link_authorized, event.chat_id, target_id):
        await reply_or_edit(event, "❌ Não foi possível atualizar a autorização de links no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    queue_audit_log(event.chat_id, target_id, "Ação: AutorizarLink", "Usuário autorizado a enviar links", admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(
        event,
        f"✅ <b>{user_info}</b> (<code>{target_id}</code>) poderá enviar links neste chat.",
        delete_after=DEFAULT_DELETE_AFTER,
    )


@client.on(events.NewMessage(pattern=r'^\.desautorizarlink(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_desautorizarlink(event):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, "❌ A autorização de links só pode ser configurada em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await can_manage_chat(event):
        await reply_or_edit(event, "❌ Somente administradores deste grupo podem remover autorizações de links.", delete_after=DEFAULT_DELETE_AFTER)
        return
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Informe um usuário por resposta, ID ou username.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if target_id not in cache.link_whitelist.get(int(event.chat_id), set()):
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        await reply_or_edit(event, f"ℹ️ {user_info} (<code>{target_id}</code>) não possui autorização de links neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.remove_link_authorized, event.chat_id, target_id):
        await reply_or_edit(event, "❌ Não foi possível remover a autorização de links no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    queue_audit_log(event.chat_id, target_id, "Ação: DesautorizarLink", "Autorização de links removida", admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(
        event,
        f"✅ Autorização de links removida de <b>{user_info}</b> (<code>{target_id}</code>).",
        delete_after=DEFAULT_DELETE_AFTER,
    )


@client.on(events.NewMessage(pattern=r'^\.listlinkauth(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listlinkauth(event):
    if not (event.is_group or event.is_channel):
        await reply_or_edit(event, "❌ A whitelist de links só pode ser consultada em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await can_manage_chat(event):
        await reply_or_edit(event, "❌ Somente administradores deste grupo podem consultar a whitelist de links.", delete_after=DEFAULT_DELETE_AFTER)
        return
    users = sorted(cache.link_whitelist.get(int(event.chat_id), set()))
    if not users:
        text = "🔗 Nenhum usuário autorizado a enviar links neste chat."
    else:
        visible_users = users[:100]
        user_info = await asyncio.to_thread(db.get_user_info_many, visible_users)
        rows = [f"• {user_info.get(user_id, str(user_id))} (<code>{user_id}</code>)" for user_id in visible_users]
        suffix = f"\n\nExibindo 100 de {len(users)}." if len(users) > 100 else ""
        text = "🔗 <b>Usuários autorizados a enviar links</b>\n" + "\n".join(rows) + suffix
    await reply_or_edit(event, text, delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.jtdel(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_del(event):
    target = await resolve_message_for_delete(event)
    if target is None or getattr(target, "id", None) == getattr(event, "id", None):
        await reply_or_edit(event, "❌ Responda à mensagem que deseja apagar ou use <code>.jtdel ID</code>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    target_user_id = int(getattr(target, "sender_id", 0) or 0)
    deleted = await delete_message_safely(target, "mensagem selecionada pelo .jtdel")
    if deleted:
        queue_audit_log(event.chat_id, target_user_id, "Ação: Del", "Exclusão manual", admin_id=event.sender_id)
        await delete_command_safely(event)
        return
    await reply_or_edit(event, "❌ Não foi possível apagar a mensagem. Verifique minhas permissões neste chat.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.jtwarn(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_warn(event):
    target_id = await get_target_from_event(event)
    _, _, reason = parse_moderation_options(event)
    if await reject_moderation_target(event, target_id):
        return
    settings = await get_settings_async(event.chat_id)
    count = await asyncio.to_thread(db.add_warning, event.chat_id, target_id)
    if count is None:
        await reply_or_edit(event, "❌ Não foi possível registrar a advertência no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    threshold = _setting_int(settings, "warn_threshold", 3, 1, 20)
    action = str(settings.get("warn_action", "mute")).lower()
    if action not in {"mute", "ban"}:
        action = "mute"
    duration = _setting_int(settings, "warn_duration", 600, 60, MAX_DURATION_SECONDS)
    if count >= threshold:
        try:
            await apply_warning_action(event.chat_id, target_id, action, duration, reason or "Limite de advertências", event.sender_id)
            cleared = await asyncio.to_thread(db.clear_warnings, event.chat_id, target_id)
            if cleared < 0:
                result = f"limite atingido; {action} aplicado por {duration_label(duration)}, mas a limpeza das advertências falhou"
            else:
                result = f"limite atingido; {action} aplicado por {duration_label(duration)}"
        except Exception as exc:
            logger.debug("Falha na ação de advertência: %s", exc)
            result = "limite atingido, mas a ação automática falhou"
    else:
        result = f"{count}/{threshold} advertências"
    queue_audit_log(event.chat_id, target_id, "Ação: Warn", reason or "Advertência", admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(event, f"⚠️ <b>{user_info}</b> (<code>{target_id}</code>): {result}.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.jtdelwarn(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_delwarn(event):
    target = await event.get_reply_message() if getattr(event, "is_reply", False) else None
    target_id = int(getattr(target, "sender_id", 0) or 0) if target is not None else 0
    if target is None or not target_id:
        await reply_or_edit(event, "❌ Responda diretamente à mensagem que deseja apagar e advertir o autor.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if is_immune(target_id):
        await reply_or_edit(event, "❌ A conta protegida não pode ser advertida.", delete_after=DEFAULT_DELETE_AFTER)
        return
    deleted = await delete_message_safely(target, "mensagem selecionada pelo .jtdelwarn")
    if not deleted:
        await reply_or_edit(event, "❌ Não foi possível apagar a mensagem; a advertência não foi aplicada.", delete_after=DEFAULT_DELETE_AFTER)
        return
    reason = " ".join((event.raw_text or "").split()[1:]).strip() or "Mensagem removida por moderação"
    settings = await get_settings_async(event.chat_id)
    count = await asyncio.to_thread(db.add_warning, event.chat_id, target_id)
    if count is None:
        await reply_or_edit(event, "❌ Não foi possível registrar a advertência no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    threshold = _setting_int(settings, "warn_threshold", 3, 1, 20)
    action = str(settings.get("warn_action", "mute")).lower()
    if action not in {"mute", "ban"}:
        action = "mute"
    duration = _setting_int(settings, "warn_duration", 600, 60, MAX_DURATION_SECONDS)
    if count >= threshold:
        try:
            await apply_warning_action(event.chat_id, target_id, action, duration, reason, event.sender_id)
            cleared = await asyncio.to_thread(db.clear_warnings, event.chat_id, target_id)
            if cleared < 0:
                result = f"limite atingido; {action} aplicado por {duration_label(duration)}, mas a limpeza das advertências falhou"
            else:
                result = f"limite atingido; {action} aplicado por {duration_label(duration)}"
        except Exception as exc:
            logger.debug("Falha na ação automática do .jtdelwarn: %s", exc)
            result = "limite atingido, mas a ação automática falhou"
    else:
        result = f"{count}/{threshold} advertências"
    queue_audit_log(event.chat_id, target_id, "Ação: Delwarn", reason, admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(event, f"🗑️⚠️ Mensagem de <b>{user_info}</b> apagada e advertência aplicada: {result}.", delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.unwarn(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unwarn(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Responda à mensagem do usuário ou informe o ID/username após <code>.unwarn</code>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if is_immune(target_id):
        await reply_or_edit(event, "❌ A conta protegida não pode ser alterada por este comando.", delete_after=DEFAULT_DELETE_AFTER)
        return
    before = await asyncio.to_thread(db.get_warning, event.chat_id, target_id)
    if before is None:
        await reply_or_edit(event, "❌ Não foi possível consultar as advertências no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    if int(before.get("count", 0)) <= 0:
        result = f"ℹ️ {user_info} (<code>{target_id}</code>) não possui advertências ativas neste chat."
    else:
        remaining = await asyncio.to_thread(db.remove_warning, event.chat_id, target_id)
        if remaining < 0:
            await reply_or_edit(event, "❌ Não foi possível remover a advertência do banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
            return
        result = f"✅ Uma advertência foi removida de {user_info} (<code>{target_id}</code>). Restantes: <code>{remaining}</code>."
        queue_audit_log(event.chat_id, target_id, "Ação: Unwarn", "Remoção de advertência", admin_id=event.sender_id)
    await reply_or_edit(event, result, delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.clearwarns(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_clearwarns(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Responda à mensagem do usuário ou informe o ID/username após <code>.clearwarns</code>.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if is_immune(target_id):
        await reply_or_edit(event, "❌ A conta protegida não pode ser alterada por este comando.", delete_after=DEFAULT_DELETE_AFTER)
        return
    removed = await asyncio.to_thread(db.clear_warnings, event.chat_id, target_id)
    if removed < 0:
        await reply_or_edit(event, "❌ Não foi possível limpar as advertências do banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    if removed:
        text = f"✅ Todas as advertências de {user_info} (<code>{target_id}</code>) foram removidas. Total: <code>{removed}</code>."
    else:
        text = f"ℹ️ {user_info} (<code>{target_id}</code>) não possui advertências ativas neste chat."
    queue_audit_log(event.chat_id, target_id, "Ação: Clearwarns", "Limpeza de advertências", admin_id=event.sender_id)
    await reply_or_edit(event, text, delete_after=DEFAULT_DELETE_AFTER)


@client.on(events.NewMessage(pattern=r'^\.warns(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_warns(event):
    rows = await asyncio.to_thread(db.get_warnings_report, event.chat_id)
    if not rows:
        await reply_or_edit(event, "📭 Nenhuma advertência ativa neste chat.", delete_after=DEFAULT_DELETE_AFTER)
        return
    visible_rows = rows[:30]
    info_map = await asyncio.to_thread(db.get_user_info_many, [row["user_id"] for row in visible_rows])
    text = "⚠️ <b>ADVERTÊNCIAS ATIVAS</b>\n\n"
    for row in visible_rows:
        user_id = int(row["user_id"])
        text += f"• {info_map.get(user_id, str(user_id))} (<code>{user_id}</code>): {row['count']}\n"
    await reply_or_edit(event, text, delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.start(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_start(event):
    text = f"🛡️ <b>Jtzin Userbot {VERSION} (Status e Health)</b>\n\nEquipe Diamond — Operacional."
    await reply_or_edit(event, text, delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.antiblack(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_antiblack(event):
    if not await require_chat_admin(event, "alterar o Modo Fênix"):
        return
    args = event.raw_text.split()
    if len(args) < 2:
        status = "ATIVADO 🛡️" if event.chat_id in cache.antiblack_chats else "DESATIVADO ❌"
        await reply_or_edit(event, f"ℹ️ Anti-Black neste chat está: <b>{status}</b>\nUse <code>.antiblack on</code> ou <code>.antiblack off</code>", delete_after=5)
        return
    
    action = args[1].lower()
    if action in ['on', 'ativar', '1']:
        if event.chat_id in cache.antiblack_chats:
            await reply_or_edit(event, "ℹ️ O Anti-Black já está ativado neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
            return
        if not await asyncio.to_thread(db.set_antiblack, event.chat_id, 1):
            await reply_or_edit(event, "❌ Não foi possível ativar o Anti-Black no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
            return
        await reply_or_edit(event, "🛡️ <b>Anti-Black ATIVADO!</b> Se algum bot rival apagar suas mensagens, o Userbot irá repostá-las instantaneamente.", delete_after=DEFAULT_DELETE_AFTER)
    elif action in ['off', 'desativar', '0']:
        if event.chat_id not in cache.antiblack_chats:
            await reply_or_edit(event, "ℹ️ O Anti-Black já está desativado neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
            return
        if not await asyncio.to_thread(db.set_antiblack, event.chat_id, 0):
            await reply_or_edit(event, "❌ Não foi possível desativar o Anti-Black no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
            return
        await reply_or_edit(event, "❌ <b>Anti-Black DESATIVADO.</b>", delete_after=DEFAULT_DELETE_AFTER)
    else:
        await reply_or_edit(event, "❌ Use <code>.antiblack on</code> ou <code>.antiblack off</code>", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.kick(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_kick(event):
    status_message = await begin_fast_response(
        event,
        "⏳ Localizando o alvo e preparando a expulsão...",
        label="status do kick",
    )
    try:
        target_id = await get_target_from_event(event)
    except Exception as exc:
        logger.exception("Erro ao interpretar o comando .kick")
        await finish_fast_response(
            event,
            status_message,
            f"❌ Não foi possível interpretar o comando: {exc}",
            label="erro ao interpretar o kick",
        )
        return
    if await reject_fast_moderation_target(event, status_message, target_id, "resultado do kick"):
        return
    try:
        get_permissions = getattr(client, "get_permissions", None)
        if callable(get_permissions):
            try:
                await get_permissions(event.chat_id, target_id)
            except UserNotParticipantError:
                user_info = await asyncio.to_thread(db.get_user_info, target_id)
                await finish_fast_response(
                    event,
                    status_message,
                    f"ℹ️ {user_info} (<code>{target_id}</code>) não está neste chat; nenhuma alteração foi necessária.",
                    label="resultado idempotente do kick",
                )
                return
        await _edit_response_now(status_message, "⏳ Aplicando a expulsão...")
        await client.kick_participant(event.chat_id, target_id)
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        queue_audit_log(event.chat_id, target_id, "Ação: Kick", "Moderação", admin_id=event.sender_id)
        await finish_fast_response(
            event,
            status_message,
            f"👢 {user_info} (<code>{target_id}</code>) foi expulso.",
            label="resultado do kick",
        )
    except ChatAdminRequiredError:
        await finish_fast_response(event, status_message, "❌ Erro: Não tenho permissão de administrador.", label="erro do kick")
    except UserAdminInvalidError:
        await finish_fast_response(event, status_message, "❌ Erro: Não é possível expulsar outro administrador (hierarquia).", label="erro do kick")
    except Exception as exc:
        logger.exception("Erro ao aplicar .kick")
        await finish_fast_response(event, status_message, f"❌ Erro ao expulsar: {exc}", label="erro do kick")

@client.on(events.NewMessage(pattern=r'^\.jtban(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_ban(event):
    # O status é mostrado antes da resolução de reply/username, que também pode
    # exigir um RPC. Assim o usuário recebe retorno visual imediatamente.
    status_message = await begin_fast_response(
        event,
        "⏳ Localizando o alvo e preparando o ban temporário...",
        label="status do ban",
    )
    try:
        target_id = await get_target_from_event(event)
        duration, purge_limit, reason = parse_moderation_options(event, allow_purge=True)
    except Exception as exc:
        logger.exception("Erro ao interpretar o comando .jtban")
        await finish_fast_response(
            event,
            status_message,
            f"❌ Não foi possível interpretar o comando: {exc}",
            label="erro ao interpretar o ban",
        )
        return
    if not target_id:
        await finish_fast_response(
            event,
            status_message,
            "❌ Não encontrei o alvo. Responda à mensagem do usuário ou informe um ID/@username válido.",
            label="resultado do ban",
        )
        return
    if is_immune(target_id):
        await finish_fast_response(
            event,
            status_message,
            "❌ Este usuário é protegido e não pode ser punido pelo Userbot.",
            label="resultado do ban",
        )
        return
    # .jtban é temporário por definição. O comando permanente é .banperm;
    # nunca permitir que a ausência de duração caia silenciosamente no ban eterno.
    if duration is None:
        await finish_fast_response(
            event,
            status_message,
            "❌ O <code>.jtban</code> exige uma duração, por exemplo <code>.jtban 30m</code>. Para banir permanentemente, use <code>.banperm</code>.",
            label="resultado do ban",
        )
        return
    if duration < MIN_TELEGRAM_TEMP_DURATION_SECONDS:
        await finish_fast_response(
            event,
            status_message,
            "❌ A duração mínima de um ban temporário é de 30 segundos. Durações menores podem ser tratadas pelo Telegram como permanentes.",
            label="resultado do ban",
        )
        return
    existing_ban, ban_state_error = await get_active_temporary_punishment(event.chat_id, target_id, "ban")
    if ban_state_error:
        await finish_fast_response(event, status_message, "❌ Não foi possível consultar o estado atual do ban com segurança.", label="erro de estado do ban")
        return
    if existing_ban:
        await finish_fast_response(event, status_message, f"ℹ️ Este usuário já está banido {_active_state_suffix(existing_ban)}; nenhuma alteração foi necessária.", label="resultado do ban")
        return
    restriction_state, restriction_error = await get_telegram_restriction_state(event.chat_id, target_id)
    if restriction_error:
        await finish_fast_response(event, status_message, "❌ Não foi possível confirmar as permissões atuais do alvo; o ban não foi reaplicado.", label="erro de estado do ban")
        return
    if restriction_state.get("ban"):
        await finish_fast_response(event, status_message, "ℹ️ Este usuário já está banido no Telegram; nenhuma alteração foi necessária.", label="resultado do ban")
        return

    # A captura do snapshot e o EditBanned RPC podem levar alguns segundos;
    # o status acima já foi mostrado antes dessas operações.
    await _edit_response_now(status_message, "⏳ Aplicando ban temporário e salvando o estado anterior...")
    expires_at = int(time.time()) + duration
    try:
        snapshot = await capture_permission_snapshot(event.chat_id, target_id)
        if snapshot is None:
            await finish_fast_response(
                event,
                status_message,
                "❌ Não foi possível capturar as permissões anteriores; o ban não foi aplicado.",
                label="resultado do ban",
            )
            return
        await client.edit_permissions(
            event.chat_id,
            target_id,
            until_date=telegram_datetime(expires_at),
            view_messages=False,
        )
        if purge_limit:
            await purge_target_messages(event.chat_id, target_id, purge_limit, include_pinned=include_pinned_requested(event))
        if not await asyncio.to_thread(db.add_temporary_punishment, event.chat_id, target_id, "ban", expires_at, reason, event.sender_id, previous_permissions=snapshot):
            try:
                await restore_permission_snapshot(event.chat_id, target_id, snapshot, "ban")
            except Exception as restore_exc:
                logger.error("Falha ao desfazer ban temporário não persistido em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await finish_fast_response(
                event,
                status_message,
                "❌ O banimento foi aplicado no Telegram, mas o prazo não pôde ser registrado; a ação foi revertida quando possível.",
                label="resultado de erro do ban",
            )
            return
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        queue_audit_log(event.chat_id, target_id, "Ação: Ban", "Moderação", admin_id=event.sender_id)
        await finish_fast_response(
            event,
            status_message,
            f"🔨 {user_info} (<code>{target_id}</code>) banido do grupo por <b>{duration_label(duration)}</b>.",
            label="resultado do ban",
        )
    except ChatAdminRequiredError:
        await finish_fast_response(event, status_message, "❌ Erro: Não tenho permissão de administrador.", label="erro do ban")
    except UserAdminInvalidError:
        await finish_fast_response(event, status_message, "❌ Erro: Não é possível banir outro administrador (hierarquia).", label="erro do ban")
    except Exception as e:
        logger.exception("Erro ao aplicar .jtban")
        await finish_fast_response(event, status_message, f"❌ Erro ao banir: {e}", label="erro do ban")

@client.on(events.NewMessage(pattern=r'^\.unban(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unban(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    state = await asyncio.to_thread(db.get_local_banperm_state, event.chat_id, target_id)
    if state is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o ban local no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    record = await asyncio.to_thread(db.get_local_banperm_record, event.chat_id, target_id) if state else {}
    if state and record is None:
        await reply_or_edit(event, "❌ Não foi possível ler o snapshot do ban local no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    snapshot = record.get("previous_permissions") if record else None
    try:
        if not state:
            temporary_ban = await asyncio.to_thread(db.get_temporary_punishment, event.chat_id, target_id, "ban")
            temporary_banperm = await asyncio.to_thread(db.get_temporary_punishment, event.chat_id, target_id, "banperm")
            if not _record_is_active(temporary_ban) and not _record_is_active(temporary_banperm):
                restriction_state, restriction_error = await get_telegram_restriction_state(event.chat_id, target_id)
                if restriction_error:
                    await reply_or_edit(event, "❌ Não foi possível confirmar se o usuário está banido; nenhuma alteração foi feita.", delete_after=DEFAULT_DELETE_AFTER)
                    return
                if not restriction_state.get("ban"):
                    await reply_or_edit(event, "ℹ️ Este usuário não está banido neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
                    return
        await restore_permission_snapshot(event.chat_id, target_id, snapshot, "ban")
        cleared = await asyncio.to_thread(db.clear_temporary_punishments, event.chat_id, target_id, ("ban", "banperm"))
        if cleared < 0:
            try:
                await client.edit_permissions(event.chat_id, target_id, view_messages=False)
            except Exception as restore_exc:
                logger.error("Falha ao manter ban após falha na limpeza temporária em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await reply_or_edit(event, "⚠️ A permissão não foi mantida liberada porque os registros temporários não puderam ser limpos.", delete_after=DEFAULT_DELETE_AFTER)
            return
        if state and not await asyncio.to_thread(db.remove_local_banperm, event.chat_id, target_id):
            try:
                await client.edit_permissions(event.chat_id, target_id, view_messages=False)
            except Exception as restore_exc:
                logger.error("Falha ao manter ban local após erro de banco em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await reply_or_edit(event, "❌ A permissão foi restaurada, mas não foi possível remover o ban local no banco; a ação foi revertida quando possível.", delete_after=DEFAULT_DELETE_AFTER)
            return
        queue_audit_log(event.chat_id, target_id, "Ação: Unban Local", "Reversão", admin_id=event.sender_id)
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) desbanido totalmente.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desbanir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.jtmute(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_mute(event):
    status_message = await begin_fast_response(
        event,
        "⏳ Localizando o alvo e preparando o silêncio...",
        label="status do mute",
    )
    try:
        target_id = await get_target_from_event(event)
        duration, purge_limit, reason = parse_moderation_options(event, allow_purge=True)
    except Exception as exc:
        logger.exception("Erro ao interpretar o comando .jtmute")
        await finish_fast_response(
            event,
            status_message,
            f"❌ Não foi possível interpretar o comando: {exc}",
            label="erro ao interpretar o mute",
        )
        return
    if await reject_fast_moderation_target(event, status_message, target_id, "resultado do mute"):
        return
    if duration is not None and duration < MIN_TELEGRAM_TEMP_DURATION_SECONDS:
        await finish_fast_response(
            event,
            status_message,
            "❌ A duração mínima de um mute temporário é de 30 segundos.",
            label="resultado do mute",
        )
        return
    existing_mute = None
    if duration is not None:
        existing_mute, mute_state_error = await get_active_temporary_punishment(event.chat_id, target_id, "mute")
        if mute_state_error:
            await finish_fast_response(event, status_message, "❌ Não foi possível consultar o estado atual do mute com segurança.", label="erro de estado do mute")
            return
        if existing_mute:
            await finish_fast_response(event, status_message, f"ℹ️ Este usuário já está silenciado {_active_state_suffix(existing_mute)}; nenhuma alteração foi necessária.", label="resultado do mute")
            return
    restriction_state, restriction_error = await get_telegram_restriction_state(event.chat_id, target_id)
    if restriction_error:
        await finish_fast_response(event, status_message, "❌ Não foi possível confirmar as permissões atuais do alvo; o mute não foi reaplicado.", label="erro de estado do mute")
        return
    if restriction_state.get("mute"):
        await finish_fast_response(event, status_message, "ℹ️ Este usuário já está silenciado no Telegram; nenhuma alteração foi necessária.", label="resultado do mute")
        return
    expires_at = int(time.time()) + duration if duration is not None else None
    try:
        await _edit_response_now(status_message, "⏳ Salvando permissões anteriores e aplicando o silêncio...")
        snapshot = await capture_permission_snapshot(event.chat_id, target_id)
        if snapshot is None:
            await finish_fast_response(
                event,
                status_message,
                "❌ Não foi possível capturar as permissões anteriores; o mute não foi aplicado.",
                label="erro de snapshot do mute",
            )
            return
        permission_kwargs = {"send_messages": False}
        if expires_at is not None:
            permission_kwargs["until_date"] = telegram_datetime(expires_at)
        await client.edit_permissions(event.chat_id, target_id, **permission_kwargs)
        if purge_limit:
            await purge_target_messages(event.chat_id, target_id, purge_limit, include_pinned=include_pinned_requested(event))
        if duration is not None and not await asyncio.to_thread(db.add_temporary_punishment, event.chat_id, target_id, "mute", expires_at, reason, event.sender_id, previous_permissions=snapshot):
            try:
                await restore_permission_snapshot(event.chat_id, target_id, snapshot, "mute")
            except Exception as restore_exc:
                logger.error("Falha ao desfazer mute temporário não persistido em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await finish_fast_response(
                event,
                status_message,
                "❌ O mute foi aplicado no Telegram, mas o prazo não pôde ser registrado; a ação foi revertida quando possível.",
                label="erro de registro do mute",
            )
            return
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        queue_audit_log(event.chat_id, target_id, "Ação: Mute", "Moderação", admin_id=event.sender_id)
        suffix = f" por {duration_label(duration)}" if duration is not None else " permanentemente"
        await finish_fast_response(
            event,
            status_message,
            f"🔇 {user_info} (<code>{target_id}</code>) silenciado{suffix}.",
            label="resultado do mute",
        )
    except ChatAdminRequiredError:
        await finish_fast_response(event, status_message, "❌ Erro: Não tenho permissão de administrador.", label="erro do mute")
    except UserAdminInvalidError:
        await finish_fast_response(event, status_message, "❌ Erro: Não é possível silenciar outro administrador (hierarquia).", label="erro do mute")
    except Exception as exc:
        logger.exception("Erro ao aplicar .jtmute")
        await finish_fast_response(event, status_message, f"❌ Erro ao silenciar: {exc}", label="erro do mute")

@client.on(events.NewMessage(pattern=r'^\.unmute(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unmute(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        temporary_mute = await asyncio.to_thread(db.get_temporary_punishment, event.chat_id, target_id, "mute")
        temporary_quarantine = await asyncio.to_thread(db.get_temporary_punishment, event.chat_id, target_id, "quarantine")
        if not _record_is_active(temporary_mute) and not _record_is_active(temporary_quarantine):
            restriction_state, restriction_error = await get_telegram_restriction_state(event.chat_id, target_id)
            if restriction_error:
                await reply_or_edit(event, "❌ Não foi possível confirmar se o usuário está silenciado; nenhuma alteração foi feita.", delete_after=DEFAULT_DELETE_AFTER)
                return
            if not restriction_state.get("mute"):
                await reply_or_edit(event, "ℹ️ Este usuário não está silenciado neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
                return
        await client.edit_permissions(event.chat_id, target_id, send_messages=True, send_media=True)
        cleared = await asyncio.to_thread(db.clear_temporary_punishments, event.chat_id, target_id, ("mute", "quarantine"))
        if cleared < 0:
            await reply_or_edit(event, "⚠️ O usuário foi liberado, mas os registros temporários não puderam ser limpos.", delete_after=DEFAULT_DELETE_AFTER)
            return
        queue_audit_log(event.chat_id, target_id, "Ação: Unmute", "Reversão", admin_id=event.sender_id)
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) pode falar novamente.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desmutar: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.blacklist(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_blacklist(event):
    target_id = await get_target_from_event(event)
    duration, _, reason = parse_moderation_options(event)
    if await reject_moderation_target(event, target_id):
        return
    expires_at = int(time.time()) + duration if duration is not None else None
    previous_blacklist = await asyncio.to_thread(db.get_local_blacklist_record, event.chat_id, target_id)
    if previous_blacklist is None:
        await reply_or_edit(event, "❌ Não foi possível consultar a blacklist local no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if _record_is_active(previous_blacklist):
        await reply_or_edit(event, f"ℹ️ Este usuário já está na blacklist local {_active_state_suffix(previous_blacklist)}; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if previous_blacklist:
        await asyncio.to_thread(db.remove_local_blacklist, event.chat_id, target_id)
    if not await asyncio.to_thread(db.add_local_blacklist, event.chat_id, target_id, reason, expires_at=expires_at):
        await reply_or_edit(event, "❌ Não foi possível registrar a blacklist local no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    queue_audit_log(event.chat_id, target_id, "Ação: Blacklist Local", "Moderação", admin_id=event.sender_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist local ({duration_label(duration)}).", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unblacklist(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unblacklist(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    previous_blacklist = await asyncio.to_thread(db.get_local_blacklist_record, event.chat_id, target_id)
    if previous_blacklist is None:
        await reply_or_edit(event, "❌ Não foi possível consultar a blacklist local no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not _record_is_active(previous_blacklist):
        if previous_blacklist:
            await asyncio.to_thread(db.remove_local_blacklist, event.chat_id, target_id)
        await reply_or_edit(event, "ℹ️ Este usuário não possui blacklist local ativa neste chat; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.remove_local_blacklist, event.chat_id, target_id):
        await reply_or_edit(event, "❌ Não foi possível remover a blacklist local do banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    queue_audit_log(event.chat_id, target_id, "Ação: Unblacklist Local", "Reversão", admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) removido da blacklist local deste chat.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.banperm(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_banperm(event):
    target_id = await get_target_from_event(event)
    duration, purge_limit, reason = parse_moderation_options(event, allow_purge=True)
    if await reject_moderation_target(event, target_id):
        return
    if duration is not None and duration < MIN_TELEGRAM_TEMP_DURATION_SECONDS:
        await reply_or_edit(event, "❌ A duração mínima de um ban temporário é de 30 segundos.", delete_after=DEFAULT_DELETE_AFTER)
        return
    expires_at = int(time.time()) + duration if duration is not None else None
    existing_banperm = await asyncio.to_thread(db.get_local_banperm_record, event.chat_id, target_id)
    if existing_banperm is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o ban local no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if _record_is_active(existing_banperm):
        await reply_or_edit(event, f"ℹ️ Este usuário já possui banimento local {_active_state_suffix(existing_banperm)}; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if existing_banperm:
        await asyncio.to_thread(db.remove_local_banperm, event.chat_id, target_id)
    try:
        snapshot = await capture_permission_snapshot(event.chat_id, target_id)
        if snapshot is None:
            await reply_or_edit(event, "❌ Não foi possível consultar as permissões atuais; o ban não foi reaplicado.", delete_after=DEFAULT_DELETE_AFTER)
            return
        if not _snapshot_allows(snapshot, "view_messages", True):
            await reply_or_edit(event, "ℹ️ Este usuário já está banido no Telegram; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
            return
        permission_kwargs = {"view_messages": False}
        if expires_at is not None:
            permission_kwargs["until_date"] = telegram_datetime(expires_at)
        await client.edit_permissions(event.chat_id, target_id, **permission_kwargs)
        if purge_limit:
            await purge_target_messages(event.chat_id, target_id, purge_limit, include_pinned=include_pinned_requested(event))
        if not await asyncio.to_thread(db.add_local_banperm, event.chat_id, target_id, reason, expires_at=expires_at, previous_permissions=snapshot):
            try:
                await restore_permission_snapshot(event.chat_id, target_id, snapshot, "banperm")
            except Exception as restore_exc:
                logger.error("Falha ao desfazer banperm não persistido em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await reply_or_edit(event, "❌ A punição foi aplicada no Telegram, mas não pôde ser registrada no banco; tente novamente após verificar o SQLite.", delete_after=DEFAULT_DELETE_AFTER)
            return
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        queue_audit_log(event.chat_id, target_id, "Ação: BanPerm", "Moderação", admin_id=event.sender_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) banido por {duration_label(duration)}.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except UserAdminInvalidError:
        await reply_or_edit(event, "❌ Erro: Não é possível banir permanentemente outro administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao banir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unbanperm(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unbanperm(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    state = await asyncio.to_thread(db.get_local_banperm_state, event.chat_id, target_id)
    if state is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o ban permanente no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not state:
        await reply_or_edit(event, "ℹ️ Não há banimento permanente registrado para este usuário neste chat.", delete_after=DEFAULT_DELETE_AFTER)
        return
    record = await asyncio.to_thread(db.get_local_banperm_record, event.chat_id, target_id)
    if record is None:
        await reply_or_edit(event, "❌ Não foi possível ler o snapshot do ban permanente no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not _record_is_active(record):
        await asyncio.to_thread(db.remove_local_banperm, event.chat_id, target_id)
        await reply_or_edit(event, "ℹ️ O ban permanente já estava vencido e foi limpo; nenhuma alteração adicional foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    snapshot = record.get("previous_permissions")
    try:
        await restore_permission_snapshot(event.chat_id, target_id, snapshot, "banperm")
        cleared = await asyncio.to_thread(db.clear_temporary_punishments, event.chat_id, target_id, ("banperm",))
        if cleared < 0:
            try:
                await client.edit_permissions(event.chat_id, target_id, view_messages=False)
            except Exception as restore_exc:
                logger.error("Falha ao manter banperm após falha na limpeza temporária em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await reply_or_edit(event, "⚠️ A permissão não foi mantida liberada porque os registros temporários não puderam ser limpos.", delete_after=DEFAULT_DELETE_AFTER)
            return
        if not await asyncio.to_thread(db.remove_local_banperm, event.chat_id, target_id):
            try:
                await client.edit_permissions(event.chat_id, target_id, view_messages=False)
            except Exception as restore_exc:
                logger.error("Falha ao manter banperm após erro de banco em %s/%s: %s", event.chat_id, target_id, restore_exc)
            await reply_or_edit(event, "❌ A permissão foi restaurada, mas não foi possível atualizar a punição local no banco; a ação foi revertida quando possível.", delete_after=DEFAULT_DELETE_AFTER)
            return
        queue_audit_log(event.chat_id, target_id, "Ação: UnbanPerm Local", "Reversão", admin_id=event.sender_id)
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) perdoado neste chat.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desbanir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.shadow(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_shadow(event):
    target_id = await get_target_from_event(event)
    duration, _, reason = parse_moderation_options(event)
    if await reject_moderation_target(event, target_id):
        return
    expires_at = int(time.time()) + duration if duration is not None else None
    previous_shadow = await asyncio.to_thread(db.get_shadow_ban_record, target_id)
    if previous_shadow is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o Shadow Ban no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if _record_is_active(previous_shadow):
        await reply_or_edit(event, f"ℹ️ Este usuário já está em Shadow Ban {_active_state_suffix(previous_shadow)}; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if previous_shadow:
        await asyncio.to_thread(db.remove_shadow_ban, target_id)
    if not await asyncio.to_thread(db.add_shadow_ban, target_id, reason, expires_at=expires_at):
        await reply_or_edit(event, "❌ Não foi possível registrar o Shadow Ban no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    queue_audit_log(event.chat_id, target_id, "Ação: Shadow Ban", "Moderação", admin_id=event.sender_id)
    await reply_or_edit(event, f"🌑 {user_info} (<code>{target_id}</code>) em Shadow Ban (mensagens serão apagadas globalmente).", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unshadow(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unshadow(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    previous_shadow = await asyncio.to_thread(db.get_shadow_ban_record, target_id)
    if previous_shadow is None:
        await reply_or_edit(event, "❌ Não foi possível consultar o Shadow Ban no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not _record_is_active(previous_shadow):
        if previous_shadow:
            await asyncio.to_thread(db.remove_shadow_ban, target_id)
        await reply_or_edit(event, "ℹ️ Este usuário não possui Shadow Ban ativo; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.remove_shadow_ban, target_id):
        await reply_or_edit(event, "❌ Não foi possível remover o Shadow Ban do banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    queue_audit_log(event.chat_id, target_id, "Ação: Unshadow Global", "Reversão", admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) saiu das sombras.", delete_after=DEFAULT_DELETE_AFTER)

async def _apply_allban_to_chat(chat, target_id, expires_at, purge_limit, include_pinned, semaphore):
    """Aplica allban em um chat com snapshot-before-action e rollback seguro."""
    chat_id = int(chat["chat_id"])
    async with semaphore:
        try:
            snapshot = await capture_permission_snapshot(chat_id, target_id)
            if snapshot is None:
                return False, "snapshot indisponível"

            permission_kwargs = {"view_messages": False}
            if expires_at is not None:
                permission_kwargs["until_date"] = telegram_datetime(expires_at)

            applied = False
            for attempt in range(2):
                try:
                    await client.edit_permissions(chat_id, target_id, **permission_kwargs)
                    applied = True
                    break
                except FloodWaitError as exc:
                    if attempt:
                        raise
                    await asyncio.sleep(exc.seconds)
            if not applied:
                return False, "permissão não aplicada"

            snapshot_saved = await asyncio.to_thread(
                db.add_global_ban_snapshot, target_id, chat_id, snapshot
            )
            if not snapshot_saved:
                try:
                    await restore_permission_snapshot(chat_id, target_id, snapshot, "ban")
                except Exception as restore_exc:
                    logger.error(
                        "Falha ao desfazer allban sem snapshot em %s/%s: %s",
                        chat_id,
                        target_id,
                        restore_exc,
                    )
                return False, "snapshot não persistido; rollback executado"

            purge_note = ""
            if purge_limit:
                try:
                    await purge_target_messages(
                        chat_id,
                        target_id,
                        purge_limit,
                        include_pinned=include_pinned,
                    )
                except Exception as purge_exc:
                    # A punição permanece aplicada; a limpeza é opcional e
                    # não deve transformar um banimento bem-sucedido em falha.
                    purge_note = "limpeza parcial"
                    logger.debug("Falha na limpeza do allban em %s/%s: %s", chat_id, target_id, purge_exc)
            return True, purge_note
        except FloodWaitError as exc:
            return False, f"FloodWait de {exc.seconds}s"
        except (RPCError, ValueError, TypeError) as exc:
            logger.debug("Falha controlada no allban em %s/%s: %s", chat_id, target_id, exc)
            return False, type(exc).__name__
        except Exception as exc:
            logger.debug("Falha inesperada no allban em %s/%s: %s", chat_id, target_id, exc)
            return False, type(exc).__name__


@client.on(events.NewMessage(pattern=r'^\.allban(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_allban(event):
    status = await begin_fast_response(
        event,
        "⏳ Allban: validando alvo e preparando a aplicação global...",
        label="status do allban",
    )
    target_id = await get_target_from_event(event)
    if await reject_fast_moderation_target(event, status, target_id, label="resultado do allban"):
        return

    duration, purge_limit, reason = parse_moderation_options(event, allow_purge=True)
    if duration is not None and duration < MIN_TELEGRAM_TEMP_DURATION_SECONDS:
        await finish_fast_response(
            event,
            status,
            "❌ A duração mínima de um allban temporário é de 30 segundos.",
            label="resultado do allban",
        )
        return

    expires_at = int(time.time()) + duration if duration is not None else None
    previous_global = await asyncio.to_thread(db.get_global_blacklist_record, target_id)
    if previous_global and str(previous_global.get("type") or "").lower() == "ban":
        await finish_fast_response(
            event,
            status,
            "ℹ️ Este usuário já possui um allban ativo. Use <code>.unallblack</code> antes de iniciar outro ciclo global.",
            label="resultado do allban",
        )
        return

    if not await asyncio.to_thread(db.add_global_blacklist, target_id, "ban", reason, expires_at):
        await finish_fast_response(
            event,
            status,
            "❌ Não foi possível registrar o banimento global no banco de dados.",
            label="resultado do allban",
        )
        return

    # Ao converter uma blacklist global ou recuperar de uma execução incompleta,
    # os snapshots anteriores não podem contaminar o novo ciclo de restauração.
    if await asyncio.to_thread(db.clear_global_ban_snapshots, target_id) < 0:
        if previous_global:
            await asyncio.to_thread(db.restore_global_blacklist_record, previous_global)
        else:
            await asyncio.to_thread(db.remove_global_blacklist, target_id)
        await finish_fast_response(
            event,
            status,
            "❌ Não foi possível preparar o controle de restauração global.",
            label="resultado do allban",
        )
        return

    if not await asyncio.to_thread(db.add_global_ban_snapshot, target_id, 0, None):
        if previous_global:
            await asyncio.to_thread(db.restore_global_blacklist_record, previous_global)
        else:
            await asyncio.to_thread(db.remove_global_blacklist, target_id)
        await finish_fast_response(
            event,
            status,
            "❌ Não foi possível inicializar os snapshots do banimento global.",
            label="resultado do allban",
        )
        return

    chats = await asyncio.to_thread(db.all_chats_detailed)
    eligible_chats = [
        chat for chat in chats
        if chat.get("active")
        and str(chat.get("chat_type") or "").lower()
        in {"group", "supergroup", "channel", "chat"}
    ]
    if not eligible_chats:
        user_info = await asyncio.to_thread(db.get_user_info, target_id)
        queue_audit_log(event.chat_id, target_id, "Ação: Allban (0 chats)", "Moderação Global", admin_id=event.sender_id)
        await finish_fast_response(
            event,
            status,
            f"⚠️ {user_info} (<code>{target_id}</code>) foi registrado globalmente, mas não há chats ativos aplicáveis.",
            label="resultado do allban",
        )
        return

    semaphore = asyncio.Semaphore(ALLBAN_CONCURRENCY)
    results = await asyncio.gather(
        *(
            _apply_allban_to_chat(
                chat,
                target_id,
                expires_at,
                purge_limit,
                include_pinned_requested(event),
                semaphore,
            )
            for chat in eligible_chats
        ),
        return_exceptions=False,
    )
    applied = sum(1 for ok, _ in results if ok)
    failed = len(results) - applied
    partial_cleanup = sum(1 for ok, note in results if ok and note)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    queue_audit_log(
        event.chat_id,
        target_id,
        f"Ação: Allban ({applied}/{len(results)} chats)",
        "Moderação Global",
        admin_id=event.sender_id,
    )
    if failed:
        result_text = (
            f"⚠️ {user_info} (<code>{target_id}</code>) aplicado em {applied}/{len(results)} chats. "
            f"Falhas: {failed}. A blacklist global permanece ativa e pode ser reprocessada após a correção das permissões."
        )
    else:
        result_text = f"✅ {user_info} (<code>{target_id}</code>) banido globalmente em {applied} chats."
    if partial_cleanup:
        result_text += f" Limpeza opcional com falhas parciais: {partial_cleanup}."
    await finish_fast_response(event, status, result_text, label="resultado do allban")

@client.on(events.NewMessage(pattern=r'^\.allblack(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_allblack(event):
    target_id = await get_target_from_event(event)
    duration, _, reason = parse_moderation_options(event)
    if await reject_moderation_target(event, target_id):
        return
    expires_at = int(time.time()) + duration if duration is not None else None
    previous_global = await asyncio.to_thread(db.get_global_blacklist_record, target_id)
    if previous_global and _record_is_active(previous_global):
        existing_type = str(previous_global.get("type") or "").lower()
        if existing_type == "black":
            await reply_or_edit(event, f"ℹ️ Este usuário já está em blacklist global {_active_state_suffix(previous_global)}; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        else:
            await reply_or_edit(event, f"ℹ️ Este usuário já possui allban global {_active_state_suffix(previous_global)}. Use <code>.unallblack</code> somente após remover a punição global atual.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.add_global_blacklist, target_id, 'black', reason, expires_at=expires_at):
        await reply_or_edit(event, "❌ Não foi possível registrar a blacklist global no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    queue_audit_log(event.chat_id, target_id, "Ação: Allblack Global", "Moderação Global", admin_id=event.sender_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist global.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unallblack(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_unallblack(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    current_global = await asyncio.to_thread(db.get_global_blacklist_record, target_id)
    if current_global is None:
        await reply_or_edit(event, "ℹ️ Este usuário não possui blacklist ou allban global ativo; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not _record_is_active(current_global):
        await asyncio.to_thread(db.remove_global_blacklist, target_id)
        await reply_or_edit(event, "ℹ️ O registro global já estava vencido e foi limpo; nenhuma alteração adicional foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    was_ban = str(current_global.get("type") or "").lower() == "ban"
    if was_ban:
        attempted, failures = await restore_global_ban(target_id)
        if failures:
            await reply_or_edit(
                event,
                f"⚠️ Não foi possível restaurar as permissões em {failures} de {attempted} chats. A punição global foi mantida para nova tentativa.",
                delete_after=DEFAULT_DELETE_AFTER,
            )
            return
    if not await asyncio.to_thread(db.remove_global_blacklist, target_id):
        await reply_or_edit(event, "❌ Não foi possível atualizar a blacklist global do banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if await asyncio.to_thread(db.clear_global_ban_snapshots, target_id) < 0:
        logger.error("Blacklist global removida, mas snapshots não puderam ser limpos: %s", target_id)
    queue_audit_log(event.chat_id, target_id, "Ação: Unallblack Global", "Reversão Global", admin_id=event.sender_id)
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) removido da blacklist global.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.autorizar(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_autorizar(event):
    target_id, duration = await get_authorization_target_and_expiry(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário por resposta, ID ou username.", delete_after=5)
        return
    if is_immune(target_id):
        await reply_or_edit(event, "❌ Os proprietários já possuem acesso permanente.", delete_after=5)
        return
    expires_at = int(time.time()) + duration if duration is not None else None
    previous_auth = await asyncio.to_thread(db.get_authorized_record, target_id)
    if previous_auth and _record_is_active(previous_auth):
        await reply_or_edit(event, f"ℹ️ Este usuário já está autorizado {_active_state_suffix(previous_auth)}; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.add_authorized, target_id, expires_at=expires_at):
        await reply_or_edit(event, "❌ Não foi possível registrar a autorização no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    if expires_at is None:
        access_text = "permanentemente"
        audit_reason = "Controle; autorização permanente"
    else:
        access_text = f"por <b>{escape(format_duration(duration))}</b> (expira em {format_timestamp(expires_at)})"
        audit_reason = f"Controle; autorização temporária por {format_duration(duration)}"
    queue_audit_log(event.chat_id, target_id, "Ação: Autorizar", audit_reason, admin_id=event.sender_id)
    await reply_or_edit(
        event,
        f"✅ Usuário {user_info} (<code>{target_id}</code>) autorizado {access_text}.",
        delete_after=5,
    )

@client.on(events.NewMessage(pattern=r'^\.desautorizar(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_desautorizar(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=5)
        return
    previous_auth = await asyncio.to_thread(db.get_authorized_record, target_id)
    if previous_auth is None:
        await reply_or_edit(event, "❌ Não foi possível consultar a autorização no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not previous_auth:
        await reply_or_edit(event, "ℹ️ Este usuário não possui autorização ativa; nenhuma alteração foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not _record_is_active(previous_auth):
        await asyncio.to_thread(db.remove_authorized, target_id)
        await reply_or_edit(event, "ℹ️ A autorização já estava vencida e foi limpa; nenhuma alteração adicional foi necessária.", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.remove_authorized, target_id):
        await reply_or_edit(event, "❌ Não foi possível revogar a autorização no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    user_info = await asyncio.to_thread(db.get_user_info, target_id)
    queue_audit_log(event.chat_id, target_id, "Ação: Desautorizar", "Controle", admin_id=event.sender_id)
    await reply_or_edit(event, f"❌ Acesso revogado para {user_info} (<code>{target_id}</code>).", delete_after=5)

@client.on(events.NewMessage(pattern=r'^\.listauth(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listauth(event):
    auths = await asyncio.to_thread(db.get_all_authorized)
    if not auths:
        await reply_or_edit(event, "📭 Nenhum usuário autorizado no momento.", delete_after=10)
        return
    info_map = await asyncio.to_thread(db.get_user_info_many, [row["user_id"] for row in auths])
    text = "👥 <b>LISTA DE USUÁRIOS AUTORIZADOS</b>\n\n"
    for row in auths:
        user_id = int(row["user_id"])
        info = info_map.get(user_id, str(user_id))
        date_str = format_timestamp(row["created_at"])
        expires_at = row.get("expires_at")
        access_text = "permanente" if not expires_at else f"expira em {format_timestamp(expires_at)}"
        text += f"• {info} (<code>{user_id}</code>)\n└ 📅 {date_str} | {access_text}\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.logs(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_logs(event):
    await audit_buffer.flush()
    logs = await asyncio.to_thread(db.get_latest_logs, 10)
    if not logs:
        await reply_or_edit(event, "📭 Nenhum log registrado recentemente.", delete_after=5)
        return
    ids = [log["user_id"] for log in logs]
    ids.extend(log["admin_id"] for log in logs if log.get("admin_id"))
    info_map = await asyncio.to_thread(db.get_user_info_many, ids)
    text = f"📜 <b>LOGS DE ATIVIDADE ({VERSION})</b>\n\n"
    for log in logs:
        user_id = int(log["user_id"])
        user_info = info_map.get(user_id, str(user_id))
        time_str = format_timestamp(log["created_at"], "%H:%M:%S")
        raw_content = str(log["content"] or "[sem conteúdo]")
        content = (raw_content[:30] + "...") if len(raw_content) > 30 else raw_content
        content = escape(content)
        text += f"⏰ <code>{time_str}</code> | 👤 {user_info}\n"
        text += f"🚫 <b>Motivo:</b> {escape(str(log['reason'] or 'não informado'))}\n"
        if log.get("admin_id"):
            admin_id = int(log["admin_id"])
            text += f"👮 <b>Admin:</b> {info_map.get(admin_id, str(admin_id))}\n"
        text += f"💬 <b>Conteúdo:</b> <i>{content}</i>\n"
        text += "------------------\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.listdn(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listdn(event):
    shadow, glob = await asyncio.to_thread(db.get_all_banned_list_detailed)
    all_rows = list(shadow) + list(glob)
    info_map = await asyncio.to_thread(db.get_user_info_many, [row["user_id"] for row in all_rows]) if all_rows else {}
    text = "📋 <b>LISTA DE PUNIÇÕES GLOBAIS</b>\n\n"
    if shadow:
        text += "🌑 <b>Shadow Ban:</b>\n"
        for row in shadow:
            user_id = int(row["user_id"])
            info = info_map.get(user_id, str(user_id))
            reason = f" | Motivo: {escape(str(row['reason']))}" if row["reason"] else ""
            date_str = format_timestamp(row["created_at"])
            text += f"• {info} (<code>{user_id}</code>){reason}\n└ 📅 {date_str}\n"
        text += "\n"
    if glob:
        text += "🌎 <b>Global Blacklist:</b>\n"
        for row in glob:
            user_id = int(row["user_id"])
            info = info_map.get(user_id, str(user_id))
            reason = f" | Motivo: {escape(str(row['reason']))}" if row["reason"] else ""
            date_str = format_timestamp(row["created_at"])
            punishment_type = str(row.get("type") or "black").upper()
            text += f"• {info} (<code>{user_id}</code>) [{punishment_type}]{reason}\n└ 📅 {date_str}\n"
    if not shadow and not glob:
        text += "Nenhuma punição global registrada."
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.status(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_status(event):
    started = time.perf_counter()
    api_state = "⚠️ indisponível"
    api_latency = "-"
    identity = "não confirmada"
    try:
        was_cached = cache.me_loaded
        api_started = time.perf_counter()
        me = await get_cached_me()
        if me is None:
            raise RuntimeError("identidade da sessão indisponível")
        api_latency = "cache" if was_cached else f"{(time.perf_counter() - api_started) * 1000:.0f} ms"
        identity = escape(str(getattr(me, "username", None) or getattr(me, "first_name", None) or me.id))
        api_state = "✅ conectada" if not was_cached else "✅ conectada (cache)"
    except (RPCError, asyncio.TimeoutError) as exc:
        logger.warning("Falha ao consultar status da API: %s", exc)
    except Exception as exc:
        logger.warning("Falha inesperada ao consultar status da API: %s", exc)

    counts = get_cache_counts()
    db_counts, chats, db_size = await asyncio.gather(
        asyncio.to_thread(db.get_diagnostic_counts),
        asyncio.to_thread(db.all_chats_detailed),
        asyncio.to_thread(db.get_db_size_bytes),
    )
    active_chats = sum(1 for chat in chats if chat.get("active"))
    text = (
        f"📊 <b>STATUS DO JTZIN USERBOT {VERSION}</b>\n\n"
        f"• Estado: <b>{'✅ online' if client.is_connected() else '⚠️ desconectado'}</b>\n"
        f"• API Telegram: {api_state} | Latência: <code>{api_latency}</code>\n"
        f"• Conta: <code>{identity}</code>\n"
        f"• Uptime: <code>{format_duration(time.time() - STARTED_AT)}</code>\n"
        f"• Chats registrados: <code>{len(chats)}</code> | Ativos: <code>{active_chats}</code>\n"
        f"• Chats bloqueados: <code>{counts['locked_chats']}</code>\n"
        f"• Autorizados: <code>{counts['authorized']}</code>\n"
        f"• Blacklists: local <code>{counts['local_blacklist']}</code> | global <code>{counts['global_blacklist']}</code>\n"
        f"• Banimentos locais: <code>{counts['local_banperm']}</code> | Shadow: <code>{counts['shadow']}</code>\n"
        f"• Logs: <code>{db_counts['deleted_logs']}</code> | Banco: <code>{format_bytes(db_size)}</code>"
    )
    performance = get_performance_snapshot()
    command_stats = performance.get("command_metrics", {})
    text += (
        f"\n• Fila exclusão: <code>{performance['pending']}</code> pendentes | "
        f"RPC último/máx.: <code>{performance['last_delete_ms']:.0f}/{performance['max_delete_ms']:.0f} ms</code>"
        f"\n• Auditoria: <code>{performance['audit_pending']}</code> pendentes | "
        f"persistidos: <code>{performance['audit_persisted']}</code>"
        f"\n• Comandos medidos: <code>{command_stats.get('total', 0)}</code> | falhas: <code>{command_stats.get('failed', 0)}</code> | ativos: <code>{command_stats.get('active', 0)}</code>"
    )
    elapsed = (time.perf_counter() - started) * 1000
    text += f"\n• Diagnóstico concluído em: <code>{elapsed:.0f} ms</code>"
    await reply_or_edit(event, text, delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.latency(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_latency(event):
    started = time.perf_counter()
    try:
        api_started = time.perf_counter()
        await client.get_me()
        api_ms = (time.perf_counter() - api_started) * 1000
        api_state = "✅ disponível"
    except Exception as exc:
        api_ms = 0.0
        api_state = f"⚠️ falhou: {escape(str(exc)[:120])}"
    performance = get_performance_snapshot()
    delete_failures = performance.get("failed", performance.get("delete_failed", 0))
    elapsed_ms = (time.perf_counter() - started) * 1000
    command_stats = performance.get("command_metrics", {})
    text = (
        f"⚡ <b>DIAGNÓSTICO DE LATÊNCIA {VERSION}</b>\n\n"
        f"• API Telegram: <b>{api_state}</b> | <code>{api_ms:.0f} ms</code>\n"
        f"• Último RPC de exclusão: <code>{performance['last_delete_ms']:.0f} ms</code>\n"
        f"• Maior RPC observado: <code>{performance['max_delete_ms']:.0f} ms</code>\n"
        f"• Exclusões imediatas: <code>{performance['immediate']}</code>\n"
        f"• Mensagens agrupadas: <code>{performance['batched']}</code>\n"
        f"• Falhas/overflow: <code>{delete_failures}/{performance.get('overflow', 0)}</code>\n"
        f"• Auditoria pendente: <code>{performance['audit_pending']}</code> | "
        f"persistida: <code>{performance['audit_persisted']}</code>\n"
        f"• Comandos medidos: <code>{command_stats.get('total', 0)}</code> | falhas: <code>{command_stats.get('failed', 0)}</code>\n"
        f"• Diagnóstico concluído em: <code>{elapsed_ms:.0f} ms</code>"
    )
    await reply_or_edit(event, text, delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.health(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_health(event):
    checks = []
    critical_ok = True

    try:
        integrity = await asyncio.to_thread(db.fetchone, "PRAGMA integrity_check")
        db_ok = bool(integrity and str(integrity[0]).lower() == "ok")
    except Exception as exc:
        logger.error("Falha no integrity_check do banco: %s", exc)
        db_ok = False
    checks.append(("SQLite", "✅ íntegro" if db_ok else "❌ falha"))
    critical_ok = critical_ok and db_ok

    connected = bool(client.is_connected())
    checks.append(("Conexão", "✅ conectada" if connected else "❌ desconectada"))
    critical_ok = critical_ok and connected

    authorized_session = False
    if connected:
        try:
            authorized_session = bool(await client.is_user_authorized())
        except (RPCError, asyncio.TimeoutError) as exc:
            logger.warning("Falha ao confirmar autorização da sessão: %s", exc)
        except Exception as exc:
            logger.warning("Falha inesperada ao confirmar sessão: %s", exc)
    checks.append(("Sessão Telegram", "✅ autorizada" if authorized_session else "⚠️ não confirmada"))
    critical_ok = critical_ok and authorized_session

    try:
        db_counts = await asyncio.to_thread(db.get_diagnostic_counts)
        counts_ok = True
    except Exception as exc:
        logger.error("Falha ao consultar contadores do banco: %s", exc)
        db_counts = {}
        counts_ok = False
    checks.append(("Esquema", "✅ tabelas acessíveis" if counts_ok else "❌ consulta falhou"))
    critical_ok = critical_ok and counts_ok

    permission_state = await get_chat_permission_health(event.chat_id) if (event.is_group or event.is_channel) else "⚪ use em grupo/canal para verificar permissões"
    checks.append(("Permissões no chat", permission_state))

    check_text = "\n".join(f"• <b>{escape(name)}:</b> {value}" for name, value in checks)
    cache_counts = get_cache_counts()
    text = (
        f"🩺 <b>HEALTH CHECK — JTZIN USERBOT {VERSION}</b>\n\n"
        f"{check_text}\n\n"
        f"• Sessão local: {get_session_state()}\n"
        f"• Cache: <code>{sum(cache_counts.values())}</code> itens monitorados\n"
        f"• Registros no banco: <code>{db_counts.get('deleted_logs', 0)}</code> logs / <code>{db_counts.get('chats', 0)}</code> chats\n"
        f"• Resultado geral: <b>{'✅ saudável' if critical_ok else '⚠️ requer atenção'}</b>"
    )
    await reply_or_edit(event, text, delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.help(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_help(event):
    text = (
        f"📖 <b>GUIA DE COMANDOS — Jtzin Userbot {VERSION}</b>\n\n"
        "🛡️ <b>MODERAÇÃO LOCAL & REVERSÃO:</b>\n"
        "• <code>.jtdel</code> (apaga a mensagem respondida)\n"
        "• <code>.lock</code> | <code>.unlock</code> (somente administradores; restaura permissões anteriores)\n"
        "• <code>.kick</code> | <code>.jtban [duração] [--purge N]</code> | <code>.unban</code>\n"
        "• <code>.jtmute [duração] [--purge N]</code> | <code>.unmute</code>\n"
        "• <code>.jtpurge [5-100]</code> | <code>.purgeme [5-100]</code> | <code>.jtpurgeall [1-1000]</code>\n"
        "• <code>.blacklist [duração]</code> | <code>.unblacklist</code> (somente este chat)\n"
        "• <code>.banperm [duração] [--purge N]</code> | <code>.unbanperm</code>\n"
        "• <code>.shadow [duração]</code> | <code>.unshadow</code> (global)\n\n"
        "⚠️ <b>ADVERTÊNCIAS & ANTISPAM:</b>\n"
        "• <code>.jtwarn @user motivo</code> | <code>.warns</code>\n"
        "• <code>.unwarn</code> (remove uma advertência) | <code>.jtdelwarn</code> (apaga e adverte)\n"
        "• <code>.clearwarns @user</code> (remove todas as advertências)\n"
        "• <code>.antispam on/off</code> (pontuação adaptativa, mídia, links e duplicação)\n"
        "• <code>.quarantine on/off</code> (só pune com padrão forte)\n"
        "• <code>.pinned on/off</code> (protege mensagens fixadas)\n\n"
        "🔗 <b>CONTROLE DE LINKS:</b>\n"
        "• <code>.antilink on/off</code> (links somente para admins e autorizados)\n"
        "• <code>.autorizarlink</code> | <code>.desautorizarlink</code> | <code>.listlinkauth</code>\n\n"
        "👑 <b>CONTROLE GLOBAL:</b>\n"
        "• <code>.allban [duração] [--purge N]</code> | <code>.allblack [duração]</code> | <code>.unallblack</code>\n"
        "• <code>.maintenance on/off</code> (somente proprietário)\n"
        "• <code>.autorizar [duração]</code> | <code>.desautorizar</code> | <code>.listauth</code> (Acessos permanentes ou temporários: 10s, 30m, 10h, 10d)\n\n"
        "🔍 <b>SEGURANÇA & CONTRA-ESPIONAGEM:</b>\n"
        "• <code>.antiblack on/off</code> (Modo Fênix)\n"
        "• <code>.antispy</code> (Varredura de Espiões)\n"
        "• <code>.listspy</code> | <code>.delspy</code> (Gestão de Espiões)\n\n"
        "🛠️ <b>UTILITÁRIOS & RELATÓRIOS:</b>\n"
        "• <code>.start</code> | <code>.status</code> | <code>.health</code> | <code>.latency</code> (Diagnóstico rápido)\n"
        "• <code>.msg</code> (Broadcast Global)\n"
        "• <code>.chats</code> (Lista de Chats)\n"
        "• <code>.listdn</code> (Punições Globais)\n"
        "• <code>.logs</code> (Auditoria de Deleções)\n"
        "• <code>.id</code> (Mostra o ID do usuário)\n"
        "• <code>.infojt</code> (Informações detalhadas por reply, ID ou username)\n"
        "• <code>.help</code>"
    )
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.antispy(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_antispy(event):
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    bait_msg = await send_status_safely(event, "🕵️‍♂️ [AntiSpy] Varrendo o chat em busca de espiões... Analisando logs de moderação...", label="status do antispy")
    if bait_msg is None:
        return
    await asyncio.sleep(5)
    try:
        result = await client(functions.channels.GetAdminLogRequest(
            channel=event.chat_id,
            q='',
            events_filter=types.ChannelAdminLogEventsFilter(delete=True, edit=True, ban=True, unban=True, kick=True, unkick=True),
            admins=None, max_id=0, min_id=0, limit=15
        ))
        evidence = {}
        action_weights = {
            "ChannelAdminLogEventActionDeleteMessage": ("exclusão", 20),
            "ChannelAdminLogEventActionEditMessage": ("edição", 10),
            "ChannelAdminLogEventActionParticipantBan": ("banimento", 35),
            "ChannelAdminLogEventActionParticipantUnban": ("reversão de banimento", 15),
            "ChannelAdminLogEventActionParticipantToggleBan": ("alteração de restrição", 25),
        }
        for entry in result.events:
            uid = entry.user_id
            if not uid or uid in [OWNER_ID, SECOND_OWNER_ID, THIRD_OWNER_ID] or uid in cache.authorized_users:
                continue
            action_name = type(getattr(entry, "action", None)).__name__
            label, weight = action_weights.get(action_name, (action_name or "evento administrativo", 5))
            item = evidence.setdefault(uid, {"signals": set(), "confidence": 0, "events": 0})
            item["signals"].add(label)
            item["confidence"] = min(100, item["confidence"] + weight)
            item["events"] += 1
        suspects = {uid: item for uid, item in evidence.items() if item["confidence"] >= 20}
        if suspects:
            spy_list = []
            for uid, item in sorted(suspects.items(), key=lambda pair: pair[1]["confidence"], reverse=True):
                info = await asyncio.to_thread(db.get_user_info, uid)
                signals = ", ".join(sorted(item["signals"]))
                persisted = await asyncio.to_thread(db.add_detected_spy, uid, event.chat_id, signals, item["confidence"])
                persistence_note = "" if persisted else " | ⚠️ não persistido"
                spy_list.append(f"• {info} (<code>{uid}</code>)\n└ Sinais: {escape(signals)} | Confiança: <code>{item['confidence']}%</code> | Eventos: <code>{item['events']}</code>{persistence_note}")
            text = "⚠️ <b>ATIVIDADE ADMINISTRATIVA SUSPEITA REGISTRADA</b>\n\n" + "\n".join(spy_list) + "\n\n<i>Os sinais não provam que a conta usa userbot; exigem confirmação manual.</i>"
        else:
            text = "✅ <b>Nenhum conjunto suficiente de sinais foi encontrado neste grupo.</b>\n\n<i>O Telegram não informa diretamente se uma conta utiliza userbot.</i>"
        await edit_and_delete_safely(bait_msg, text, delete_after=15, label="resultado do antispy")
    except ChatAdminRequiredError:
        await edit_and_delete_safely(
            bait_msg,
            "❌ Erro: Preciso ser Administrador com acesso ao Log de Auditoria para detectar espiões.",
            label="erro do antispy",
        )
    except Exception as e:
        logger.error("Erro na varredura AntiSpy: %s", e)
        await edit_and_delete_safely(
            bait_msg,
            "❌ Não foi possível concluir a varredura AntiSpy.",
            label="erro do antispy",
        )
    await delete_command_safely(event)

@client.on(events.NewMessage(pattern=r'^\.listspy(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listspy(event):
    spies = await asyncio.to_thread(db.get_all_spies)
    if not spies:
        await reply_or_edit(event, "✅ <b>Nenhum espião registrado no banco de dados.</b>", delete_after=5)
        return
    text = "🕵️‍♂️ <b>LISTA DE ESPIÕES DETECTADOS (.listspy)</b>\n\n"
    for s in spies:
        info = await asyncio.to_thread(db.get_user_info, s['user_id'])
        date_str = datetime.fromtimestamp(s['detected_at']).strftime('%d/%m/%Y %H:%M')
        signals = escape(s.get('signals') or 'não informado')
        confidence = int(s.get('confidence') or 0)
        text += f"• {info} (<code>{s['user_id']}</code>)\n└ 🕒 {date_str} | Chat: <code>{s['chat_id']}</code> | Confiança: <code>{confidence}%</code>\n└ Sinais: {signals}\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.delspy(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_delspy(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Responda à mensagem do espião, ou digite o ID/Username após .delspy", delete_after=DEFAULT_DELETE_AFTER)
        return
    if not await asyncio.to_thread(db.remove_spy, target_id):
        await reply_or_edit(event, "❌ Não foi possível remover o registro de espião no banco de dados.", delete_after=DEFAULT_DELETE_AFTER)
        return
    info = await asyncio.to_thread(db.get_user_info, target_id)
    await reply_or_edit(event, f"✅ <b>{info} (<code>{target_id}</code>) removido da lista de espiões.</b>", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.jtpurgeall(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_purgeall(event):
    """Apaga mensagens recentes de todos os remetentes no chat atual."""
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return

    limit, limit_error = parse_purgeall_limit(event)
    if limit_error:
        await reply_or_edit(event, limit_error, delete_after=DEFAULT_DELETE_AFTER)
        return

    status_msg = await send_status_safely(
        event,
        f"🧹 [PurgeAll] Apagando até {limit} mensagens recentes de todos os usuários...",
        label="status do purgeall",
    )
    if status_msg is None:
        return
    message_ids = []
    try:
        # Não usamos deleteHistory: somente os IDs coletados nesta janela
        # são removidos, mantendo o alcance previsível e reversível no código.
        settings = await get_settings_async(event.chat_id)
        protect_pinned = bool(settings.get("protect_pinned", 1))
        include_pinned = include_pinned_requested(event)
        scan_limit = PURGEALL_MAX_SCAN
        async for msg in client.iter_messages(event.chat_id, limit=scan_limit):
            if msg.id in {event.id, status_msg.id}:
                continue
            if getattr(msg, "pinned", False) and protect_pinned and not include_pinned:
                continue
            message_ids.append(msg.id)
            if len(message_ids) >= limit:
                break

        deleted_count = await delete_message_ids_safely(
            event.chat_id, message_ids, batch_size=PURGEALL_BATCH_SIZE
        )
        await edit_and_delete_safely(
            status_msg,
            f"✅ <b>PurgeAll concluído!</b> {deleted_count} de {limit} mensagens foram apagadas.",
            label="status do purgeall",
        )
    except FloodWaitError as exc:
        logger.warning("FloodWait no .jtpurgeall por %s segundos", exc.seconds)
        await asyncio.sleep(exc.seconds)
        await delete_message_safely(status_msg, "status do purgeall")
    except Exception as exc:
        logger.error("Erro ao executar .jtpurgeall: %s", exc)
        await edit_and_delete_safely(
            status_msg,
            "❌ Não foi possível concluir o .jtpurgeall.",
            label="status de erro do purgeall",
        )

    await delete_command_safely(event)


@client.on(events.NewMessage(pattern=r'^\.jtpurge(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_purge(event):
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    
    target_id = await get_target_from_event(event)
    limit, limit_error = parse_purge_limit(event)
    if limit_error:
        await reply_or_edit(event, limit_error, delete_after=DEFAULT_DELETE_AFTER)
        return

    if not target_id:
        await reply_or_edit(event, "❌ Responda à mensagem do usuário ou informe @username / ID junto com a quantidade. Ex: <code>.jtpurge 10</code>", delete_after=DEFAULT_DELETE_AFTER)
        return

    info = await asyncio.to_thread(db.get_user_info, target_id)
    status_msg = await send_status_safely(
        event,
        f"🧹 [Purge] Apagando até {limit} mensagens (qualquer tipo) de {info}...",
        label="status do purge",
    )
    if status_msg is None:
        return
    message_ids = []
    try:
        # Primeiro coleta os IDs; depois envia a exclusão em lotes para reduzir
        # chamadas individuais sem alterar o limite de 5–100 mensagens.
        settings = await get_settings_async(event.chat_id)
        protect_pinned = bool(settings.get("protect_pinned", 1))
        include_pinned = include_pinned_requested(event)
        scan_limit = MAX_HISTORY_SCAN
        async for msg in client.iter_messages(event.chat_id, limit=scan_limit, from_user=target_id):
            if msg.id != event.id and (not getattr(msg, "pinned", False) or include_pinned or not protect_pinned):
                message_ids.append(msg.id)
                if len(message_ids) >= limit:
                    break
        deleted_count = await delete_message_ids_safely(event.chat_id, message_ids)
        await edit_and_delete_safely(status_msg, f"✅ <b>Purge concluído!</b> {deleted_count} mensagens de {info} foram apagadas.", label="status do purge")
    except Exception as e:
        logger.error("Erro ao executar .jtpurge: %s", e)
        await edit_and_delete_safely(status_msg, "❌ Não foi possível concluir o .jtpurge.", label="status de erro do purge")

    await delete_command_safely(event)

@client.on(events.NewMessage(pattern=r'^\.purgeme(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_purgeme(event):
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    
    limit, limit_error = parse_purge_limit(event)
    if limit_error:
        await reply_or_edit(event, limit_error, delete_after=DEFAULT_DELETE_AFTER)
        return

    status_msg = await send_status_safely(
        event,
        f"🧹 [PurgeMe] Apagando suas últimas {limit} mensagens...",
        label="status do purgeme",
    )
    if status_msg is None:
        return
    me_id = event.sender_id
    message_ids = []
    try:
        settings = await get_settings_async(event.chat_id)
        protect_pinned = bool(settings.get("protect_pinned", 1))
        include_pinned = include_pinned_requested(event)
        scan_limit = min(MAX_HISTORY_SCAN, limit + 2)
        async for msg in client.iter_messages(event.chat_id, limit=scan_limit, from_user=me_id):
            if msg.id != status_msg.id and msg.id != event.id and (not getattr(msg, "pinned", False) or include_pinned or not protect_pinned):
                message_ids.append(msg.id)
                if len(message_ids) >= limit:
                    break
        deleted_count = await delete_message_ids_safely(event.chat_id, message_ids)
        await edit_and_delete_safely(status_msg, f"✅ <b>PurgeMe concluído!</b> {deleted_count} mensagens suas foram apagadas.", label="status do purgeme")
    except Exception as e:
        logger.error("Erro ao executar .purgeme: %s", e)
        await edit_and_delete_safely(status_msg, "❌ Não foi possível concluir o .purgeme.", label="status de erro do purgeme")

    await delete_command_safely(event)

@client.on(events.NewMessage(pattern=r'^\.id(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_id(event):
    target_id = await get_target_from_event(event) or event.sender_id
    await reply_or_edit(event, f"🆔 ID: <code>{target_id}</code>", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.infojt(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_infojt(event):
    """Exibe informações detalhadas de um usuário por reply, ID ou username."""
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(
            event,
            "❌ Informe o usuário respondendo à mensagem ou usando um ID/@username.",
            delete_after=DEFAULT_DELETE_AFTER,
        )
        return

    target_id = int(target_id)
    display_name = "—"
    display_name_is_html = False
    username = None
    bot_label = "—"
    deleted = False

    try:
        entity = await client.get_entity(target_id)
        if isinstance(entity, User):
            db.remember_user(entity.id, entity.username, entity.first_name)
            first_name = entity.first_name or ""
            last_name = entity.last_name or ""
            display_name = (f"{first_name} {last_name}").strip() or "—"
            username = entity.username
            bot_label = "Sim" if bool(getattr(entity, "bot", False)) else "Não"
            deleted = bool(getattr(entity, "deleted", False))
        else:
            display_name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(target_id)
            username = getattr(entity, "username", None)
    except Exception as exc:
        logger.debug("Não foi possível resolver a entidade do .infojt para %s: %s", target_id, exc)
        display_name = await asyncio.to_thread(db.get_user_info, target_id) or str(target_id)
        display_name_is_html = True

    name_html = display_name if display_name_is_html else escape(str(display_name))
    username_html = escape(f"@{username}" if username else "—")
    deleted_suffix = " <i>(conta excluída)</i>" if deleted else ""

    lines = [
        "👤 <b>Informações do Usuário</b>",
        "",
        f"🆔 <b>ID:</b> <code>{target_id}</code>",
        f"📛 <b>Nome:</b> {name_html}{deleted_suffix}",
        f"🌐 <b>Username:</b> {username_html}",
        f"🤖 <b>Bot:</b> {bot_label}",
    ]

    is_chat = bool(event.is_group or event.is_channel)
    if is_chat:
        chat_status = "Indisponível"
        join_label = "Indisponível"
        try:
            permissions = await client.get_permissions(event.chat_id, target_id)
            participant = getattr(permissions, "participant", None)
            participant_type = type(participant).__name__
            if bool(getattr(permissions, "is_creator", False)) or participant_type == "ChannelParticipantCreator":
                chat_status = "Criador"
            elif bool(getattr(permissions, "is_admin", False)) or participant_type == "ChannelParticipantAdmin":
                chat_status = "Administrador"
            elif bool(getattr(permissions, "is_banned", False)) or participant_type in {
                "ChannelParticipantBanned",
                "ChannelParticipantLeft",
            }:
                chat_status = "Banido"
            elif getattr(permissions, "send_messages", True) is False:
                chat_status = "Silenciado"
            else:
                chat_status = "Membro"

            joined_at = getattr(participant, "date", None)
            if isinstance(joined_at, datetime):
                join_label = joined_at.strftime("%d/%m/%Y %H:%M")
            elif joined_at:
                join_label = format_timestamp(joined_at)
        except Exception as exc:
            logger.debug("Não foi possível consultar permissões do .infojt para %s/%s: %s", event.chat_id, target_id, exc)

        warning_data = await asyncio.to_thread(db.get_warning, event.chat_id, target_id)
        settings = await get_settings_async(event.chat_id) or {}
        try:
            threshold = _setting_int(settings, "warn_threshold", 3, 1, 20)
        except (TypeError, ValueError):
            threshold = 3
        if warning_data is None:
            warning_label = "Erro ao consultar"
        else:
            try:
                warning_count = max(0, int(warning_data.get("count", 0)))
            except (TypeError, ValueError):
                warning_count = 0
            warning_label = f"{warning_count}/{threshold}"

        chat_id = event.chat_id
        local_blacklisted = target_id in cache.local_blacklist.get(chat_id, ())
        local_banperm = target_id in cache.local_banperm.get(chat_id, ())
        global_type = cache.global_blacklist_types.get(target_id) if target_id in cache.global_blacklist else None
        shadow_banned = target_id in cache.shadow_ban

        lines.extend([
            "",
            "📊 <b>Status no Chat:</b>",
            f"• Situação: {chat_status}",
            f"• Advertências: {warning_label}",
            f"• Entrada: {join_label}",
            "",
            "🛡️ <b>Punições Ativas:</b>",
            f"• Blacklist Local: {'✅ Sim' if local_blacklisted else '❌ Não'}",
            f"• Ban Permanente Local: {'✅ Sim' if local_banperm else '❌ Não'}",
            f"• Blacklist Global: {'✅ Sim (' + escape(str(global_type).lower()) + ')' if global_type else '❌ Não'}",
            f"• Shadow Ban: {'✅ Sim' if shadow_banned else '❌ Não'}",
        ])

    if is_owner(target_id):
        authorization_label = "Proprietário (imune)"
    elif target_id in cache.authorized_users:
        expires_at = cache.authorized_expirations.get(target_id)
        if expires_at is None:
            authorization_label = "Sim (permanente)"
        elif int(expires_at) > int(time.time()):
            authorization_label = f"Sim (expira em {format_timestamp(expires_at)})"
        else:
            authorization_label = "Expirada"
    else:
        authorization_label = "Não"

    lines.extend(["", f"🔑 <b>Autorização:</b> {authorization_label}"])
    await reply_or_edit(event, "\n".join(lines), delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.msg(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_msg(event):
    reply = await event.get_reply_message()
    command_args = event.raw_text.split(maxsplit=1)
    text_arg = command_args[1] if len(command_args) > 1 else None
    if reply is None and not text_arg:
        await reply_or_edit(event, "❌ Digite a mensagem ou responda a uma mídia.", delete_after=DEFAULT_DELETE_AFTER)
        return
    chats = await asyncio.to_thread(db.all_chats_detailed)
    targets = [
        chat for chat in chats
        if chat.get('active') and chat.get('chat_type') not in ['private', 'User']
    ]
    semaphore = asyncio.Semaphore(BROADCAST_CONCURRENCY)

    async def _broadcast_one(chat):
        chat_id = chat.get('chat_id')
        async with semaphore:
            for attempt in range(2):
                try:
                    await send_broadcast_payload(chat_id, reply, text_arg)
                    return True
                except FloodWaitError as exc:
                    if attempt == 0 and int(getattr(exc, 'seconds', 0) or 0) <= FLOOD_SLEEP_THRESHOLD:
                        await asyncio.sleep(max(0, int(exc.seconds)))
                        continue
                    logger.warning("FloodWait ao transmitir para %s (%ss); chat ignorado nesta rodada.", chat_id, getattr(exc, 'seconds', '?'))
                    return False
                except Exception as exc:
                    logger.debug("Falha ao transmitir para %s: %s", chat_id, exc)
                    return False
        return False

    results = await asyncio.gather(
        *(_broadcast_one(chat) for chat in targets),
        return_exceptions=False,
    )
    success = sum(1 for result in results if result)
    await reply_or_edit(event, f"📢 Transmissão concluída: {success}/{len(targets)} chats receberam.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.chats(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_chats(event):
    chats = await asyncio.to_thread(db.all_chats_detailed)
    private_ids = [r.get('chat_id') for r in chats if r.get('chat_type') in ['private', 'User']]
    private_names = await asyncio.to_thread(db.get_user_info_many, private_ids)
    grupos, canais, privados = [], [], []
    for r in chats:
        status = "✅" if r['active'] else "❌"
        chat_title = escape(str(r.get('title') or 'Sem título'))
        chat_info = f"{status} {chat_title} (<code>{r['chat_id']}</code>)"
        if r['chat_type'] in ['group', 'supergroup', 'Chat']: grupos.append(chat_info)
        elif r['chat_type'] in ['channel', 'Channel']: canais.append(chat_info)
        elif r['chat_type'] in ['private', 'User']:
            user_info = private_names.get(int(r['chat_id']), str(r['chat_id']))
            privados.append(f"{status} {user_info} (<code>{r['chat_id']}</code>)")
    text = f"📡 <b>RELATÓRIO DE CHATS {VERSION}</b>\n\n"
    if grupos: text += "👥 <b>GRUPOS:</b>\n" + "\n".join(grupos) + "\n\n"
    if canais: text += "📣 <b>CANAIS:</b>\n" + "\n".join(canais) + "\n\n"
    if privados: text += "👤 <b>USUÁRIOS NO PRIVADO:</b>\n" + "\n".join(privados) + "\n\n"
    text += "📊 <b>RESUMO:</b>\n"
    text += f"• Grupos/Canais: {len(grupos) + len(canais)}\n• Usuários: {len(privados)}"
    await reply_or_edit(event, text, delete_after=15)

# --- INICIALIZAÇÃO ---
if __name__ == "__main__":
    cache.load_all(db.conn)
    logger.info("JTZIN USERBOT %s (STATUS E HEALTH) INICIANDO...", VERSION)
    client.start()
    try:
        # Aquece a identidade uma única vez para retirar get_me do comando .status.
        cache.me = client.loop.run_until_complete(client.get_me())
        cache.me_loaded = cache.me is not None
    except Exception as exc:
        logger.warning("Não foi possível aquecer a identidade da sessão: %s", exc)
    async def _start_expiry_supervised():
        # A tarefa precisa ser criada dentro de um loop ativo; Python 3.14
        # não permite asyncio.create_task() no escopo síncrono do módulo.
        return schedule_background(temporary_expiry_loop(), "temporary-expiry")

    expiry_task = client.loop.run_until_complete(_start_expiry_supervised())
    logger.info("USERBOT TELETHON ONLINE!")
    try:
        client.run_until_disconnected()
    finally:
        try:
            expiry_task.cancel()
            client.loop.run_until_complete(expiry_task)
        except (asyncio.CancelledError, Exception):
            pass
        try:
            client.loop.run_until_complete(background_supervisor.cancel_all())
        except Exception as exc:
            logger.error("Falha ao cancelar tarefas de fundo: %s", exc)
        try:
            client.loop.run_until_complete(audit_buffer.flush())
        except Exception as exc:
            logger.error("Falha no flush final da auditoria: %s", exc)
