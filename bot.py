from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import ChatPermissions, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    ContextTypes,
    MessageReactionHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

ENV_FILE = Path(os.getenv("BOT_ENV_FILE", ".env.bot"))
if not ENV_FILE.is_absolute():
    ENV_FILE = BASE_DIR / ENV_FILE
load_dotenv(ENV_FILE)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente em {ENV_FILE.name}: {name}")
    return value


BOT_TOKEN = _required_env("BOT_TOKEN")


def _parse_owner_ids() -> frozenset[int]:
    raw = os.getenv("OWNER_IDS", "").strip()
    if not raw:
        raw = _required_env("OWNER_ID")
    values = set()
    for token in re.split(r"[\s,;]+", raw):
        token = token.strip()
        if not token:
            continue
        try:
            owner_id = int(token)
        except ValueError as exc:
            raise RuntimeError("OWNER_IDS deve conter apenas IDs numéricos separados por vírgula") from exc
        if owner_id <= 0:
            raise RuntimeError("OWNER_IDS deve conter somente IDs positivos")
        values.add(owner_id)
    if not values:
        raise RuntimeError("Pelo menos um proprietário deve ser configurado em OWNER_IDS")
    return frozenset(values)


OWNER_IDS = _parse_owner_ids()
# Compatibilidade interna: OWNER_ID representa o menor ID, mas as autorizações usam OWNER_IDS.
OWNER_ID = min(OWNER_IDS)
# As notificações do .divulgar são sempre enviadas para esta conta owner.
DIVULGAR_NOTIFY_USER_ID = 6822870889


def _is_owner(user_id: int | None) -> bool:
    try:
        return int(user_id or 0) in OWNER_IDS
    except (TypeError, ValueError):
        return False

DB_PATH = DATA_DIR / "bot_api.db"
HEARTBEAT_PATH = DATA_DIR / "bot_api.heartbeat"
DELETE_AFTER_SECONDS = 5
JTBN_CONCURRENCY = 4
MAX_TARGET_ID_LENGTH = 20
# O lote aguarda apenas alguns milissegundos para capturar uma rajada sem atrasar
# uma mensagem isolada. O Telegram aceita de 1 a 100 IDs no deleteMessages.
DELETE_BATCH_WINDOW_SECONDS = 0.008
DELETE_BATCH_MAX_MESSAGES = 100
API_CONNECTION_POOL_SIZE = 32
GET_UPDATES_READ_TIMEOUT_SECONDS = 35.0
POLLING_BOOTSTRAP_RETRIES = -1
POLLING_TIMEOUT_SECONDS = 35
DB_RETRY_ATTEMPTS = 3
DB_RETRY_BACKOFF_SECONDS = 0.05
API_RETRY_ATTEMPTS = 3
API_RETRY_BASE_SECONDS = 0.25
API_RETRY_MAX_SECONDS = 4.0
HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_STALE_AFTER_SECONDS = 180.0
COMMAND_ARGUMENT_MAX_CHARS = 1024
DIVULGAR_MIN_INTERVAL_SECONDS = 30
DIVULGAR_MAX_INTERVAL_SECONDS = 30 * 24 * 60 * 60
DIVULGAR_MAX_TEXT_LENGTH = 4096
DIVULGAR_MAX_CAPTION_LENGTH = 1024
DIVULGAR_ALLOWED_MEDIA = {"photo", "video"}
DIVULGAR_MAX_SCHEDULES_PER_CHAT = 32
LIST_MAX_VISIBLE_ENTRIES = 30
LIST_MAX_PAGE = 1_000_000
LIST_MAX_OUTPUT_CHARS = 3600
LIST_REASON_MAX_CHARS = 80
SPAM_MIN_COUNT = 1
SPAM_MAX_COUNT = 100
# Grupos têm limites de envio mais restritos; o intervalo conservador reduz RetryAfter e falhas parciais.
SPAM_DELAY_SECONDS = 3.1
SPAM_MAX_ACTIVE_PER_CHAT = 1
SPAM_MAX_TEXT_LENGTH = 4096
SPAM_MAX_CAPTION_LENGTH = 1024
SPAM_SUPPORTED_TYPES = {"text", "photo", "video", "animation", "document", "audio", "voice", "sticker"}

# `.jt` é exclusivo dos owners. Usuários autorizados pelo owner podem usar
# somente os comandos explicitamente listados aqui, sempre dentro do grupo em
# que foram autorizados; não existe elevação de privilégio entre grupos.
OWNER_ONLY_COMMANDS = frozenset({"jt", "jtbn", "unjtbn", "lock", "unlock", "divulgar", "spam"})
DELEGATED_COMMANDS = frozenset({"help", "blacklist", "unblacklist", "jtperm", "unjtperm", "latency"})

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("jtzin-bot-api")
# httpx inclui a URL completa nas mensagens INFO; para a Bot API isso exporia o token.
for noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)


@dataclass(frozen=True)
class Target:
    user_id: int
    username: str = ""
    full_name: str = ""

    @property
    def label(self) -> str:
        if self.username:
            return f"@{self.username.lstrip('@')}"
        return self.full_name or str(self.user_id)


class Database:
    """Banco independente do Userbot, com escritas serializadas e cache carregável."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.execute("PRAGMA cache_size=-8192")
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    chat_type TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blacklist (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS banperm (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS allban (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS authorized_users (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    added_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS chat_locks (
                    chat_id INTEGER PRIMARY KEY,
                    permissions_json TEXT NOT NULL,
                    locked_by INTEGER NOT NULL,
                    locked_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS divulgacoes (
                    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    content_type TEXT NOT NULL CHECK(content_type IN ('text','photo','video')),
                    text TEXT NOT NULL DEFAULT '',
                    file_id TEXT NOT NULL DEFAULT '',
                    source_message_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    next_run_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_divulgacoes_chat ON divulgacoes(chat_id);
                CREATE INDEX IF NOT EXISTS idx_divulgacoes_updated ON divulgacoes(updated_at);
                CREATE INDEX IF NOT EXISTS idx_chats_active ON chats(active);
                CREATE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_blacklist_chat ON blacklist(chat_id);
                CREATE INDEX IF NOT EXISTS idx_blacklist_chat_created ON blacklist(chat_id,created_at,user_id);
                CREATE INDEX IF NOT EXISTS idx_banperm_chat ON banperm(chat_id);
                CREATE INDEX IF NOT EXISTS idx_allban_created ON allban(created_at,user_id);
                CREATE INDEX IF NOT EXISTS idx_authorized_users_chat ON authorized_users(chat_id,created_at,user_id);
                """
            )
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(divulgacoes)").fetchall()}
            if "schedule_id" not in columns:
                # Migração V2: a versão antiga usava chat_id como chave única.
                # O registro antigo é preservado como o primeiro agendamento do grupo.
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute("DROP INDEX IF EXISTS idx_divulgacoes_chat")
                    self.conn.execute("DROP INDEX IF EXISTS idx_divulgacoes_updated")
                    self.conn.execute("ALTER TABLE divulgacoes RENAME TO divulgacoes_legacy_v1")
                    self.conn.execute(
                        """
                        CREATE TABLE divulgacoes (
                            schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            chat_id INTEGER NOT NULL,
                            interval_seconds INTEGER NOT NULL,
                            content_type TEXT NOT NULL CHECK(content_type IN ('text','photo','video')),
                            text TEXT NOT NULL DEFAULT '',
                            file_id TEXT NOT NULL DEFAULT '',
                            source_message_id INTEGER NOT NULL,
                            owner_id INTEGER NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            next_run_at REAL NOT NULL DEFAULT 0
                        )
                        """
                    )
                    legacy_next_run_expression = "next_run_at" if "next_run_at" in columns else "0"
                    self.conn.execute(
                        f"""
                        INSERT INTO divulgacoes(
                            chat_id,interval_seconds,content_type,text,file_id,source_message_id,
                            owner_id,created_at,updated_at,next_run_at
                        )
                        SELECT chat_id,interval_seconds,content_type,text,file_id,source_message_id,
                               owner_id,created_at,updated_at,{legacy_next_run_expression}
                        FROM divulgacoes_legacy_v1
                        """
                    )
                    self.conn.execute("DROP TABLE divulgacoes_legacy_v1")
                    self.conn.execute("CREATE INDEX idx_divulgacoes_chat ON divulgacoes(chat_id)")
                    self.conn.execute("CREATE INDEX idx_divulgacoes_updated ON divulgacoes(updated_at)")
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
            elif "next_run_at" not in columns:
                self.conn.execute("ALTER TABLE divulgacoes ADD COLUMN next_run_at REAL NOT NULL DEFAULT 0")
            self.conn.commit()

    def _execute(self, sql, params=(), *, commit=False):
        with self._lock:
            cursor = self.conn.execute(sql, params)
            if commit:
                self.conn.commit()
            return cursor

    def _fetchone(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _fetchall(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def load_state(self):
        with self._lock:
            chats = self.conn.execute(
                "SELECT chat_id FROM chats WHERE active=1 AND chat_type IN ('group','supergroup')"
            ).fetchall()
            users = self.conn.execute(
                "SELECT user_id,username,full_name FROM users"
            ).fetchall()
            blacklist_rows = self.conn.execute(
                "SELECT chat_id,user_id FROM blacklist"
            ).fetchall()
            banperm_rows = self.conn.execute(
                "SELECT chat_id,user_id FROM banperm"
            ).fetchall()
            allban_rows = self.conn.execute("SELECT user_id FROM allban").fetchall()
        blacklist_by_chat = defaultdict(set)
        for row in blacklist_rows:
            blacklist_by_chat[int(row["chat_id"])].add(int(row["user_id"]))
        banperm_by_chat = defaultdict(set)
        for row in banperm_rows:
            banperm_by_chat[int(row["chat_id"])].add(int(row["user_id"]))
        return (
            {int(row["chat_id"]) for row in chats},
            {int(row["user_id"]): (row["username"] or "", row["full_name"] or "") for row in users},
            blacklist_by_chat,
            banperm_by_chat,
            {int(row["user_id"]) for row in allban_rows},
        )

    def register_chat(self, chat_id: int, title: str, chat_type: str, active: bool = True):
        self._execute(
            """
            INSERT INTO chats(chat_id,title,chat_type,active,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                chat_type=excluded.chat_type,
                active=excluded.active,
                updated_at=excluded.updated_at
            """,
            (int(chat_id), title or "", chat_type or "", int(active), int(time.time())),
            commit=True,
        )

    def remember_user(self, user_id: int, username: str = "", full_name: str = ""):
        self._execute(
            """
            INSERT INTO users(user_id,username,full_name,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                updated_at=excluded.updated_at
            """,
            (int(user_id), (username or "").lstrip("@"), full_name or "", int(time.time())),
            commit=True,
        )

    def resolve_username(self, username: str):
        row = self._fetchone(
            "SELECT user_id,username,full_name FROM users WHERE username = ? COLLATE NOCASE LIMIT 1",
            (username.lstrip("@"),),
        )
        return dict(row) if row else None

    def add_blacklist(self, target: Target, chat_id: int, added_by: int, reason: str):
        cursor = self._execute(
            """
            INSERT INTO blacklist(chat_id,user_id,username,reason,added_by,created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                username=excluded.username,
                reason=excluded.reason,
                added_by=excluded.added_by
            """,
            (int(chat_id), target.user_id, target.username, reason, int(added_by), int(time.time())),
            commit=True,
        )
        return cursor.rowcount >= 0

    def add_banperm(self, target: Target, chat_id: int, added_by: int, reason: str):
        cursor = self._execute(
            """
            INSERT INTO banperm(chat_id,user_id,username,reason,added_by,created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                username=excluded.username,
                reason=excluded.reason,
                added_by=excluded.added_by
            """,
            (int(chat_id), target.user_id, target.username, reason, int(added_by), int(time.time())),
            commit=True,
        )
        return cursor.rowcount >= 0

    def add_allban(self, target: Target, added_by: int, reason: str):
        cursor = self._execute(
            """
            INSERT INTO allban(user_id,username,reason,added_by,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                reason=excluded.reason,
                added_by=excluded.added_by
            """,
            (target.user_id, target.username, reason, int(added_by), int(time.time())),
            commit=True,
        )
        return cursor.rowcount >= 0

    def get_blacklist_for_chat(self, chat_id: int):
        return self._fetchall(
            "SELECT user_id,username,reason,added_by,created_at FROM blacklist WHERE chat_id=? ORDER BY created_at,user_id",
            (int(chat_id),),
        )

    def has_blacklist(self, user_id: int, chat_id: int) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM blacklist WHERE chat_id=? AND user_id=? LIMIT 1",
            (int(chat_id), int(user_id)),
        )
        return row is not None

    def count_blacklist_for_chat(self, chat_id: int) -> int:
        row = self._fetchone("SELECT COUNT(*) AS total FROM blacklist WHERE chat_id=?", (int(chat_id),))
        return int(row["total"] if row else 0)

    def get_blacklist_for_chat_page(self, chat_id: int, limit: int, offset: int):
        return self._fetchall(
            """
            SELECT user_id,username,reason,added_by,created_at
            FROM blacklist WHERE chat_id=?
            ORDER BY created_at,user_id LIMIT ? OFFSET ?
            """,
            (int(chat_id), max(1, int(limit)), max(0, int(offset))),
        )

    def count_banperm_for_chat(self, chat_id: int) -> int:
        row = self._fetchone("SELECT COUNT(*) AS total FROM banperm WHERE chat_id=?", (int(chat_id),))
        return int(row["total"] if row else 0)

    def get_banperm_for_chat_page(self, chat_id: int, limit: int, offset: int):
        return self._fetchall(
            """
            SELECT user_id,username,reason,added_by,created_at
            FROM banperm WHERE chat_id=?
            ORDER BY created_at,user_id LIMIT ? OFFSET ?
            """,
            (int(chat_id), max(1, int(limit)), max(0, int(offset))),
        )

    def get_allban_entries(self):
        return self._fetchall(
            "SELECT user_id,username,reason,added_by,created_at FROM allban ORDER BY created_at,user_id"
        )

    def count_allban(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS total FROM allban")
        return int(row["total"] if row else 0)

    def get_allban_entries_page(self, limit: int, offset: int):
        return self._fetchall(
            """
            SELECT user_id,username,reason,added_by,created_at
            FROM allban ORDER BY created_at,user_id LIMIT ? OFFSET ?
            """,
            (max(1, int(limit)), max(0, int(offset))),
        )

    def has_allban(self, user_id: int) -> bool:
        row = self._fetchone("SELECT 1 FROM allban WHERE user_id=? LIMIT 1", (int(user_id),))
        return row is not None

    def load_authorized_users(self):
        return self._fetchall(
            "SELECT chat_id,user_id FROM authorized_users"
        )

    def add_authorized(self, target: Target, chat_id: int, added_by: int) -> bool:
        now = int(time.time())
        cursor = self._execute(
            """
            INSERT INTO authorized_users(chat_id,user_id,username,full_name,added_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                added_by=excluded.added_by,
                updated_at=excluded.updated_at
            """,
            (
                int(chat_id), target.user_id, target.username, target.full_name,
                int(added_by), now, now,
            ),
            commit=True,
        )
        return cursor.rowcount >= 0

    def remove_authorized(self, user_id: int, chat_id: int) -> bool:
        cursor = self._execute(
            "DELETE FROM authorized_users WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
            commit=True,
        )
        return cursor.rowcount > 0

    def remove_all_authorized(self, chat_id: int) -> int:
        cursor = self._execute(
            "DELETE FROM authorized_users WHERE chat_id=?",
            (int(chat_id),),
            commit=True,
        )
        return int(cursor.rowcount)

    def has_authorized(self, user_id: int, chat_id: int) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM authorized_users WHERE chat_id=? AND user_id=? LIMIT 1",
            (int(chat_id), int(user_id)),
        )
        return row is not None

    def count_authorized_for_chat(self, chat_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS total FROM authorized_users WHERE chat_id=?",
            (int(chat_id),),
        )
        return int(row["total"] if row else 0)

    def get_authorized_for_chat_page(self, chat_id: int, limit: int, offset: int):
        return self._fetchall(
            """
            SELECT user_id,username,full_name,added_by,created_at
            FROM authorized_users WHERE chat_id=?
            ORDER BY created_at,user_id LIMIT ? OFFSET ?
            """,
            (int(chat_id), max(1, int(limit)), max(0, int(offset))),
        )

    def remove_blacklist(self, user_id: int, chat_id: int) -> bool:
        cursor = self._execute(
            "DELETE FROM blacklist WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
            commit=True,
        )
        return cursor.rowcount > 0

    def remove_banperm(self, user_id: int, chat_id: int) -> bool:
        cursor = self._execute(
            "DELETE FROM banperm WHERE chat_id=? AND user_id=?",
            (int(chat_id), int(user_id)),
            commit=True,
        )
        return cursor.rowcount > 0

    def has_banperm(self, user_id: int, chat_id: int) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM banperm WHERE chat_id=? AND user_id=? LIMIT 1",
            (int(chat_id), int(user_id)),
        )
        return row is not None

    def remove_allban(self, user_id: int) -> bool:
        cursor = self._execute(
            "DELETE FROM allban WHERE user_id=?",
            (int(user_id),),
            commit=True,
        )
        return cursor.rowcount > 0

    def save_chat_lock(self, chat_id: int, permissions_json: str, locked_by: int) -> bool:
        now = int(time.time())
        cursor = self._execute(
            """
            INSERT INTO chat_locks(chat_id,permissions_json,locked_by,locked_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                permissions_json=excluded.permissions_json,
                locked_by=excluded.locked_by,
                locked_at=excluded.locked_at,
                updated_at=excluded.updated_at
            """,
            (int(chat_id), str(permissions_json), int(locked_by), now, now),
            commit=True,
        )
        return cursor.rowcount >= 0

    def get_chat_lock(self, chat_id: int):
        row = self._fetchone(
            "SELECT chat_id,permissions_json,locked_by,locked_at,updated_at FROM chat_locks WHERE chat_id=? LIMIT 1",
            (int(chat_id),),
        )
        return dict(row) if row else None

    def remove_chat_lock(self, chat_id: int) -> bool:
        cursor = self._execute(
            "DELETE FROM chat_locks WHERE chat_id=?",
            (int(chat_id),),
            commit=True,
        )
        return cursor.rowcount > 0

    def active_chats(self):
        return self._fetchall(
            "SELECT chat_id,title,chat_type FROM chats WHERE active=1 AND chat_type IN ('group','supergroup') ORDER BY chat_id"
        )

    def save_divulgacao(self, chat_id: int, interval_seconds: int, content_type: str, text: str, file_id: str, source_message_id: int, owner_id: int, next_run_at: float | None = None) -> int:
        now = int(time.time())
        next_run_at = float(next_run_at if next_run_at is not None else time.time() + interval_seconds)
        cursor = self._execute(
            """
            INSERT INTO divulgacoes(
                chat_id,interval_seconds,content_type,text,file_id,source_message_id,
                owner_id,created_at,updated_at,next_run_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (int(chat_id), int(interval_seconds), content_type, text or "", file_id or "", int(source_message_id), int(owner_id), now, now, next_run_at),
            commit=True,
        )
        return int(cursor.lastrowid)

    def save_divulgacao_if_capacity(self, chat_id: int, interval_seconds: int, content_type: str, text: str, file_id: str, source_message_id: int, owner_id: int, max_schedules: int, next_run_at: float | None = None) -> int | None:
        """Insere uma divulgação somente se o limite ainda não foi atingido.

        O bloqueio imediato torna a checagem e o INSERT uma única operação lógica,
        evitando que dois comandos concorrentes ultrapassem o limite por corrida.
        """
        max_schedules = int(max_schedules)
        if max_schedules < 1:
            raise ValueError("max_schedules deve ser positivo")
        now = int(time.time())
        next_run_at = float(next_run_at if next_run_at is not None else time.time() + int(interval_seconds))
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS total FROM divulgacoes WHERE chat_id=?",
                    (int(chat_id),),
                ).fetchone()
                if int(row["total"] if row else 0) >= max_schedules:
                    self.conn.rollback()
                    return None
                cursor = self.conn.execute(
                    """
                    INSERT INTO divulgacoes(
                        chat_id,interval_seconds,content_type,text,file_id,source_message_id,
                        owner_id,created_at,updated_at,next_run_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(chat_id), int(interval_seconds), content_type, text or "", file_id or "",
                        int(source_message_id), int(owner_id), now, now, next_run_at,
                    ),
                )
                self.conn.commit()
                return int(cursor.lastrowid)
            except Exception:
                self.conn.rollback()
                raise

    def get_divulgacoes(self):
        return self._fetchall(
            "SELECT schedule_id,chat_id,interval_seconds,content_type,text,file_id,source_message_id,owner_id,next_run_at FROM divulgacoes ORDER BY chat_id,schedule_id"
        )

    def get_divulgacoes_for_chat(self, chat_id: int):
        return self._fetchall(
            "SELECT schedule_id,chat_id,interval_seconds,content_type,text,file_id,source_message_id,owner_id,next_run_at FROM divulgacoes WHERE chat_id=? ORDER BY schedule_id",
            (int(chat_id),),
        )

    def update_divulgacao_next_run(self, schedule_id: int, next_run_at: float):
        self._execute(
            "UPDATE divulgacoes SET next_run_at=?, updated_at=? WHERE schedule_id=?",
            (float(next_run_at), int(time.time()), int(schedule_id)),
            commit=True,
        )

    def remove_divulgacao(self, schedule_id: int) -> bool:
        cursor = self._execute("DELETE FROM divulgacoes WHERE schedule_id=?", (int(schedule_id),), commit=True)
        return cursor.rowcount > 0

    def remove_all_divulgacoes_for_chat(self, chat_id: int) -> int:
        cursor = self._execute("DELETE FROM divulgacoes WHERE chat_id=?", (int(chat_id),), commit=True)
        return int(cursor.rowcount)

    # Alias de compatibilidade para scripts locais que usavam o nome anterior.
    def remove_divulgacoes_for_chat(self, chat_id: int) -> int:
        return self.remove_all_divulgacoes_for_chat(chat_id)

    def close(self):
        with self._lock:
            self.conn.close()


db = Database(DB_PATH)
try:
    KNOWN_CHAT_IDS, KNOWN_USERS, BLACKLIST_CACHE, BANPERM_CACHE, JTBN_CACHE = db.load_state()
    _authorized_rows = db.load_authorized_users()
except sqlite3.Error:
    logger.exception("Falha ao carregar o estado do banco do Bot API")
    raise

AUTHORIZED_CACHE: dict[int, set[int]] = defaultdict(set)
for _authorized_row in _authorized_rows:
    try:
        AUTHORIZED_CACHE[int(_authorized_row["chat_id"])].add(int(_authorized_row["user_id"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        logger.warning("Registro de autorização inválido ignorado no startup: %r", tuple(_authorized_row))

BOT_USER_ID = 0
_cleanup_tasks: set[asyncio.Task] = set()
_delete_batch_pending: dict[int, dict[int, float]] = defaultdict(dict)
_delete_batch_tasks: dict[int, asyncio.Task] = {}
_chat_registration_seen = set(KNOWN_CHAT_IDS)
DIVULGAR_TASKS: dict[int, asyncio.Task] = {}
DIVULGAR_CONFIGS: dict[int, dict] = {}
DIVULGAR_LAST_FAILURE_NOTIFY: dict[int, float] = {}
DIVULGAR_FAILURE_NOTIFY_COOLDOWN_SECONDS = 15 * 60
SPAM_TASKS: dict[int, asyncio.Task] = {}
SPAM_CONFIGS: dict[int, dict] = {}
JTBN_BAN_INFLIGHT: set[tuple[int, int]] = set()
BANPERM_REENTRY_INFLIGHT: set[tuple[int, int]] = set()
BANPERM_REENTRY_LAST_ENFORCED: dict[tuple[int, int], float] = {}
HEARTBEAT_TASK: asyncio.Task | None = None
KNOWN_USERNAME_IDS = {
    username.lower(): int(user_id)
    for user_id, (username, _full_name) in KNOWN_USERS.items()
    if username
}
CHAT_MEMBER_CACHE_TTL = 5.0
CHAT_MEMBER_ERROR_TTL = 1.5
CHAT_MEMBER_CACHE: dict[tuple[int, int], tuple[float, object | None]] = {}
CHAT_MEMBER_INFLIGHT: dict[tuple[int, int], asyncio.Task] = {}
ALLOWED_UPDATES = ["message", "edited_message", "message_reaction", "my_chat_member", "chat_member"]
BLACKLIST_TELEMETRY = {
    "matched": 0,
    "delete_scheduled": 0,
    "delete_success": 0,
    "delete_failed": 0,
    "last_update_age_ms": None,
    "last_queue_ms": None,
    "last_delete_rpc_ms": None,
    "max_delete_rpc_ms": 0.0,
    "batch_success": 0,
    "batch_messages": 0,
    "batch_fallbacks": 0,
    "network_errors": 0,
    "retry_after_events": 0,
    "command_started": 0,
    "command_completed": 0,
    "command_failed": 0,
    "last_command_ms": None,
    "max_command_ms": 0.0,
    "last_error": "",
    "polling_errors": 0,
    "last_polling_error": "",
    "background_task_errors": 0,
    "spam_started": 0,
    "spam_completed": 0,
    "spam_failed": 0,
    "spam_sent": 0,
    "spam_active": 0,
    "last_spam_error": "",
    "banperm_reentry_attempted": 0,
    "banperm_reentry_success": 0,
    "banperm_reentry_failed": 0,
    "reaction_detected": 0,
    "reaction_removed": 0,
    "reaction_remove_failed": 0,
}


def _remember_user_in_memory(user) -> Target:
    username = (getattr(user, "username", "") or "").lstrip("@")
    full_name = getattr(user, "full_name", "") or ""
    user_id = int(user.id)
    previous = KNOWN_USERS.get(user_id)
    if previous and previous[0] and previous[0].lower() != username.lower():
        if KNOWN_USERNAME_IDS.get(previous[0].lower()) == user_id:
            KNOWN_USERNAME_IDS.pop(previous[0].lower(), None)
    KNOWN_USERS[user_id] = (username, full_name)
    if username:
        KNOWN_USERNAME_IDS[username.lower()] = user_id
    return Target(user_id, username, full_name)


def _safe_html(text: str) -> str:
    return escape(str(text or ""))


def _format_ms(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if not math.isfinite(number) else f"{number:.0f} ms"


_CHAT_PERMISSION_FIELDS = tuple(ChatPermissions.all_permissions().to_dict().keys())


def _permissions_to_json(permissions: ChatPermissions) -> str:
    payload = {
        field: bool(value)
        for field, value in permissions.to_dict().items()
        if field in _CHAT_PERMISSION_FIELDS and value is not None
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _permissions_from_payload(payload: object) -> ChatPermissions | None:
    if not isinstance(payload, dict):
        return None
    values = {}
    for field in _CHAT_PERMISSION_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if isinstance(value, bool):
            values[field] = value
        elif isinstance(value, int) and value in {0, 1}:
            values[field] = bool(value)
        else:
            return None
    return ChatPermissions(**values) if values else None


def _permissions_from_json(raw: str) -> ChatPermissions | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("default"), dict):
        payload = payload["default"]
    return _permissions_from_payload(payload)


def _lock_snapshot_json(default_permissions: ChatPermissions, owner_overrides: dict[int, ChatPermissions]) -> str:
    owners = {
        str(int(user_id)): json.loads(_permissions_to_json(permissions))
        for user_id, permissions in owner_overrides.items()
    }
    return json.dumps(
        {
            "default": json.loads(_permissions_to_json(default_permissions)),
            "owners": owners,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_lock_snapshot(raw: str) -> tuple[ChatPermissions, dict[int, ChatPermissions]] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    # Snapshots antigos eram um objeto plano; continuam válidos para unlock.
    if "default" not in payload:
        permissions = _permissions_from_payload(payload)
        return (permissions, {}) if permissions is not None else None
    permissions = _permissions_from_payload(payload.get("default"))
    if permissions is None:
        return None
    raw_owners = payload.get("owners", {})
    if not isinstance(raw_owners, dict):
        return None
    owners = {}
    for raw_user_id, raw_permissions in raw_owners.items():
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            return None
        if user_id <= 0:
            return None
        owner_permissions = _permissions_from_payload(raw_permissions)
        if owner_permissions is None:
            return None
        owners[user_id] = owner_permissions
    return permissions, owners


def _locked_permissions() -> ChatPermissions:
    return ChatPermissions.no_permissions()


def _format_user_list(
    rows,
    *,
    title: str,
    empty_text: str,
    scope_text: str = "",
    total: int | None = None,
    page: int = 1,
    next_page_command: str = ".blacklist list",
) -> str:
    """Renderiza uma página de moderação sem exceder o limite de resposta do Telegram."""
    page = max(1, int(page))
    total = max(0, int(len(rows) if total is None else total))
    if not total:
        return empty_text
    if not rows:
        return f"{title} — <b>{total}</b> registro(s)\nℹ️ A página <b>{page}</b> está vazia."

    lines = []
    for row in rows[:LIST_MAX_VISIBLE_ENTRIES]:
        user_id = int(row["user_id"])
        keys = set(row.keys()) if hasattr(row, "keys") else set()
        username = (row["username"] or "").strip().lstrip("@") if "username" in keys else ""
        row_full_name = row["full_name"] if "full_name" in keys else ""
        known_username, known_full_name = KNOWN_USERS.get(user_id, ("", ""))
        target = Target(user_id, username or known_username, str(row_full_name or known_full_name))
        label = _safe_html(target.label)
        reason_value = row["reason"] if "reason" in keys else ""
        reason = " ".join(str(reason_value or "").split())[:LIST_REASON_MAX_CHARS]
        reason_text = f" — {_safe_html(reason)}" if reason else ""
        lines.append(f"• <b>{label}</b> (<code>{user_id}</code>){reason_text}")

    header = f"{title} — <b>{total}</b> registro(s)"
    if page > 1:
        header += f"\nPágina <b>{page}</b>"
    if scope_text:
        header += f"\n{scope_text}"
    output = header + "\n\n"
    rendered = 0
    for line in lines:
        candidate = output + line + "\n"
        if len(candidate) > LIST_MAX_OUTPUT_CHARS:
            break
        output = candidate
        rendered += 1
    offset = (page - 1) * LIST_MAX_VISIBLE_ENTRIES
    remaining = max(0, total - offset - rendered)
    if remaining > 0:
        output += (
            f"\n… e mais <b>{remaining}</b>. Use "
            f"<code>{_safe_html(next_page_command)} {page + 1}</code>."
        )
    return output.rstrip()


def _parse_list_page(raw: str) -> int | None:
    if not re.fullmatch(r"[1-9]\d{0,6}", (raw or "").strip()):
        return None
    page = int(raw)
    return page if page <= LIST_MAX_PAGE else None


def _retry_delay(exc: RetryAfter, maximum: float = 60.0):
    try:
        delay = float(exc.retry_after)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(delay):
        return 1.0
    return min(max(delay, 0.0), maximum)


def _consume_background_task_result(task: asyncio.Task):
    _cleanup_tasks.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is None:
        return
    BLACKLIST_TELEMETRY["background_task_errors"] += 1
    BLACKLIST_TELEMETRY["last_error"] = type(error).__name__
    logger.error("Worker assíncrono terminou com erro: %s", error, exc_info=(type(error), error, error.__traceback__))


def _track_task(coro):
    task = asyncio.create_task(coro)
    _cleanup_tasks.add(task)
    task.add_done_callback(_consume_background_task_result)
    return task


def _is_transient_api_error(exc: BaseException) -> bool:
    return isinstance(exc, (RetryAfter, TimedOut, NetworkError))


def _retry_backoff(attempt: int) -> float:
    # Backoff exponencial pequeno: recupera falhas transitórias sem travar comandos.
    return min(API_RETRY_MAX_SECONDS, API_RETRY_BASE_SECONDS * (2 ** max(0, int(attempt))))


async def _api_call(operation, *args, operation_name: str = "operação", retry_after_maximum: float = API_RETRY_MAX_SECONDS, **kwargs):
    """Executa uma chamada da Bot API com retry seguro e limitado."""
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            return await operation(*args, **kwargs)
        except RetryAfter as exc:
            BLACKLIST_TELEMETRY["retry_after_events"] += 1
            delay = _retry_delay(exc, maximum=retry_after_maximum)
            if attempt + 1 >= API_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError):
            BLACKLIST_TELEMETRY["network_errors"] += 1
            if attempt + 1 >= API_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(_retry_backoff(attempt))
        except asyncio.CancelledError:
            raise
    raise RuntimeError(f"Falha sem resultado em {operation_name}")


def _write_heartbeat():
    now = time.time()
    temporary_path = HEARTBEAT_PATH.with_suffix(".tmp")
    try:
        temporary_path.write_text(f"{os.getpid()}\n{now:.6f}\n", encoding="utf-8")
        temporary_path.replace(HEARTBEAT_PATH)
    except OSError:
        logger.debug("Não foi possível atualizar o heartbeat do Bot API", exc_info=True)


async def _heartbeat_worker():
    while True:
        _write_heartbeat()
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _db_call(method, *args, **kwargs):
    """Executa SQLite fora do event loop e repete somente locks transitórios."""
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt + 1 >= DB_RETRY_ATTEMPTS:
                raise
            await asyncio.sleep(DB_RETRY_BACKOFF_SECONDS * (attempt + 1))


async def _delete_one(bot, chat_id: int, message_id: int, scheduled_at: float | None = None):
    started = time.perf_counter()
    if scheduled_at is not None:
        BLACKLIST_TELEMETRY["last_queue_ms"] = max(0.0, (started - scheduled_at) * 1000)
    try:
        await _api_call(
            bot.delete_message,
            chat_id=chat_id,
            message_id=message_id,
            operation_name="exclusão de mensagem",
            retry_after_maximum=60.0,
        )
    except (BadRequest, Forbidden, TelegramError):
        BLACKLIST_TELEMETRY["delete_failed"] += 1
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        BLACKLIST_TELEMETRY["delete_failed"] += 1
        logger.debug("Falha inesperada ao apagar mensagem imediatamente", exc_info=True)
        return False
    rpc_ms = (time.perf_counter() - started) * 1000
    BLACKLIST_TELEMETRY["delete_success"] += 1
    BLACKLIST_TELEMETRY["last_delete_rpc_ms"] = rpc_ms
    BLACKLIST_TELEMETRY["max_delete_rpc_ms"] = max(BLACKLIST_TELEMETRY["max_delete_rpc_ms"], rpc_ms)
    return True


async def _delete_batch_worker(bot, chat_id: int):
    await asyncio.sleep(DELETE_BATCH_WINDOW_SECONDS)
    try:
        pending = _delete_batch_pending.get(chat_id)
        if not pending:
            return
        items = list(pending.items())[:DELETE_BATCH_MAX_MESSAGES]
        for message_id, _scheduled_at in items:
            pending.pop(message_id, None)
        message_ids = [message_id for message_id, _scheduled_at in items]
        oldest_scheduled_at = min(scheduled_at for _message_id, scheduled_at in items)
        started = time.perf_counter()
        delete_messages = getattr(bot, "delete_messages", None)
        batch_ok = False
        if callable(delete_messages):
            try:
                batch_ok = bool(
                    await _api_call(
                        delete_messages,
                        chat_id=chat_id,
                        message_ids=message_ids,
                        operation_name="exclusão em lote",
                        retry_after_maximum=60.0,
                    )
                )
            except (BadRequest, Forbidden, TelegramError):
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Falha inesperada no deleteMessages", exc_info=True)
        if batch_ok:
            rpc_ms = (time.perf_counter() - started) * 1000
            BLACKLIST_TELEMETRY["last_queue_ms"] = max(0.0, (started - oldest_scheduled_at) * 1000)
            BLACKLIST_TELEMETRY["last_delete_rpc_ms"] = rpc_ms
            BLACKLIST_TELEMETRY["max_delete_rpc_ms"] = max(BLACKLIST_TELEMETRY["max_delete_rpc_ms"], rpc_ms)
            BLACKLIST_TELEMETRY["delete_success"] += len(message_ids)
            BLACKLIST_TELEMETRY["batch_success"] += 1
            BLACKLIST_TELEMETRY["batch_messages"] += len(message_ids)
        else:
            BLACKLIST_TELEMETRY["batch_fallbacks"] += 1
            await asyncio.gather(
                *(_delete_one(bot, chat_id, message_id, scheduled_at) for message_id, scheduled_at in items),
                return_exceptions=False,
            )
    except asyncio.CancelledError:
        raise
    finally:
        current = asyncio.current_task()
        if _delete_batch_tasks.get(chat_id) is current:
            _delete_batch_tasks.pop(chat_id, None)
        pending = _delete_batch_pending.get(chat_id)
        if pending:
            if current is None or not current.cancelling():
                _delete_batch_tasks[chat_id] = _track_task(_delete_batch_worker(bot, chat_id))
        else:
            _delete_batch_pending.pop(chat_id, None)


def _schedule_delete_now(bot, message):
    if message is None:
        return
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    BLACKLIST_TELEMETRY["delete_scheduled"] += 1
    pending = _delete_batch_pending[int(chat_id)]
    pending[int(message_id)] = time.perf_counter()
    if int(chat_id) not in _delete_batch_tasks:
        _delete_batch_tasks[int(chat_id)] = _track_task(_delete_batch_worker(bot, int(chat_id)))


async def _remove_all_user_reactions(bot, chat_id: int, user_id: int):
    """Remove as reações recentes do usuário sem apagar mensagens de terceiros."""
    remove_all = getattr(bot, "delete_all_message_reactions", None)
    if not callable(remove_all):
        return False
    try:
        await _api_call(
            remove_all,
            chat_id=int(chat_id),
            user_id=int(user_id),
            operation_name="remoção das reações do usuário",
            retry_after_maximum=60.0,
        )
        return True
    except (BadRequest, Forbidden, TelegramError):
        BLACKLIST_TELEMETRY["reaction_remove_failed"] += 1
        logger.debug("Telegram recusou a remoção das reações do usuário", exc_info=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        BLACKLIST_TELEMETRY["reaction_remove_failed"] += 1
        logger.debug("Falha inesperada ao remover as reações do usuário", exc_info=True)
    return False


async def on_message_reaction_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a reação nova de um usuário punido sem apagar a mensagem reagida."""
    reaction = getattr(update, "message_reaction", None)
    chat = getattr(reaction, "chat", None) if reaction else None
    user = getattr(reaction, "user", None) if reaction else None
    new_reaction = getattr(reaction, "new_reaction", ()) if reaction else ()
    if not reaction or not chat or not user or user.is_bot or not new_reaction:
        return
    user_id = int(user.id)
    chat_id = int(chat.id)
    if _is_owner(user_id):
        return
    if user_id not in BLACKLIST_CACHE.get(chat_id, set()):
        return
    BLACKLIST_TELEMETRY["reaction_detected"] += 1
    remove_one = getattr(context.bot, "delete_message_reaction", None)
    if not callable(remove_one):
        BLACKLIST_TELEMETRY["reaction_remove_failed"] += 1
        return
    try:
        removed = await _api_call(
            remove_one,
            chat_id=chat_id,
            message_id=int(reaction.message_id),
            user_id=user_id,
            operation_name="remoção de reação blacklistada",
            retry_after_maximum=60.0,
        )
        if removed:
            BLACKLIST_TELEMETRY["reaction_removed"] += 1
        else:
            BLACKLIST_TELEMETRY["reaction_remove_failed"] += 1
    except (BadRequest, Forbidden, TelegramError):
        BLACKLIST_TELEMETRY["reaction_remove_failed"] += 1
        logger.debug("Telegram recusou a remoção da reação blacklistada", exc_info=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        BLACKLIST_TELEMETRY["reaction_remove_failed"] += 1
        logger.debug("Falha inesperada ao remover a reação blacklistada", exc_info=True)


def _schedule_delete(message, delay: int = DELETE_AFTER_SECONDS):
    if message is None:
        return

    async def worker():
        try:
            await asyncio.sleep(delay)
            await _api_call(
                message.delete,
                operation_name="limpeza de resposta",
                retry_after_maximum=60.0,
            )
        except (BadRequest, Forbidden, TelegramError):
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Falha inesperada ao apagar mensagem agendada", exc_info=True)

    _track_task(worker())


async def _reply_and_cleanup(update: Update, text: str, *, parse_mode: str | None = "HTML"):
    message = update.effective_message
    if message is None:
        return None
    kwargs = {"parse_mode": parse_mode} if parse_mode else {}
    try:
        response = await _api_call(
            message.reply_text,
            text,
            operation_name="resposta de comando",
            **kwargs,
        )
    except TelegramError as exc:
        BLACKLIST_TELEMETRY["last_error"] = type(exc).__name__
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        BLACKLIST_TELEMETRY["last_error"] = "resposta_de_comando"
        logger.debug("Falha inesperada ao enviar resposta do comando", exc_info=True)
        return None
    _schedule_delete(message)
    _schedule_delete(response)
    return response


def _parse_divulgar_interval(raw: str) -> int | None:
    match = re.fullmatch(r"(\d{1,12})([smhd])", (raw or "").strip().lower())
    if not match:
        return None
    try:
        amount = int(match.group(1))
    except (TypeError, ValueError):
        return None
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * multiplier
    if not DIVULGAR_MIN_INTERVAL_SECONDS <= seconds <= DIVULGAR_MAX_INTERVAL_SECONDS:
        return None
    return seconds


def _format_divulgar_interval(seconds: int) -> str:
    seconds = int(seconds)
    for unit, multiplier in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds % multiplier == 0:
            return f"{seconds // multiplier}{unit}"
    return f"{seconds}s"


def _format_divulgar_datetime(timestamp: float) -> str:
    try:
        value = float(timestamp)
        if not math.isfinite(value):
            return "indisponível"
        return datetime.fromtimestamp(value).strftime("%d/%m/%Y às %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "indisponível"


def _queue_divulgar_notification(bot, text: str):
    return _track_task(_send_divulgar_notification(bot, text))


async def _send_divulgar_notification(bot, text: str):
    if not _is_owner(DIVULGAR_NOTIFY_USER_ID):
        logger.error("Destinatário fixo de divulgação não está em OWNER_IDS; notificação bloqueada")
        return False
    try:
        await _api_call(
            bot.send_message,
            chat_id=DIVULGAR_NOTIFY_USER_ID,
            text=text,
            parse_mode="HTML",
            disable_notification=True,
            operation_name="notificação privada da divulgação",
            retry_after_maximum=30.0,
        )
        return True
    except Forbidden:
        logger.warning("Não foi possível enviar DM da divulgação; o owner precisa abrir o chat do bot")
        return False
    except TelegramError:
        logger.warning("Falha Telegram ao enviar notificação privada da divulgação", exc_info=True)
        return False
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Falha inesperada ao enviar notificação privada da divulgação")
        return False


def _extract_divulgacao(source_message):
    if source_message is None:
        return None, "❌ Responda a uma mensagem de texto, foto ou vídeo."
    if source_message.text and not source_message.photo and not source_message.video:
        text = source_message.text.strip()
        if not text:
            return None, "❌ A mensagem respondida não possui texto."
        if len(text) > DIVULGAR_MAX_TEXT_LENGTH:
            return None, f"❌ O texto excede o limite de {DIVULGAR_MAX_TEXT_LENGTH} caracteres."
        return {"content_type": "text", "text": text, "file_id": "", "source_message_id": int(source_message.message_id)}, None
    if source_message.photo:
        caption = (source_message.caption or "").strip()
        if len(caption) > DIVULGAR_MAX_CAPTION_LENGTH:
            return None, f"❌ A legenda excede o limite de {DIVULGAR_MAX_CAPTION_LENGTH} caracteres."
        return {
            "content_type": "photo",
            "text": caption,
            "file_id": source_message.photo[-1].file_id,
            "source_message_id": int(source_message.message_id),
        }, None
    if source_message.video:
        caption = (source_message.caption or "").strip()
        if len(caption) > DIVULGAR_MAX_CAPTION_LENGTH:
            return None, f"❌ A legenda excede o limite de {DIVULGAR_MAX_CAPTION_LENGTH} caracteres."
        return {
            "content_type": "video",
            "text": caption,
            "file_id": source_message.video.file_id,
            "source_message_id": int(source_message.message_id),
        }, None
    return None, "❌ O tipo respondido não é suportado. Use texto, foto ou vídeo."


async def _cancel_divulgar_task(schedule_id: int):
    schedule_id = int(schedule_id)
    task = DIVULGAR_TASKS.pop(schedule_id, None)
    if task is None or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _cancel_all_divulgar_tasks_for_chat(chat_id: int):
    chat_id = int(chat_id)
    schedule_ids = [
        schedule_id
        for schedule_id, config in DIVULGAR_CONFIGS.items()
        if int(config.get("chat_id", 0)) == chat_id
    ]
    for schedule_id in schedule_ids:
        await _cancel_divulgar_task(schedule_id)


def _divulgar_list_text(rows) -> str:
    lines = []
    for row in rows:
        try:
            raw_type = str(row["content_type"] or "desconhecido")
        except (KeyError, IndexError, TypeError):
            raw_type = "desconhecido"
        content_type = _safe_html({"text": "texto", "photo": "foto", "video": "vídeo"}.get(raw_type, raw_type))
        try:
            next_run_at = float(row["next_run_at"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            next_run_at = 0
        try:
            schedule_id = int(row["schedule_id"])
            interval = int(row["interval_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        lines.append(
            f"• ID <code>{schedule_id}</code> — {content_type}, "
            f"a cada <code>{_format_divulgar_interval(interval)}</code>; "
            f"próximo: <b>{_format_divulgar_datetime(next_run_at) if next_run_at else 'indisponível'}</b>"
        )
    if not lines:
        return "ℹ️ Nenhuma agenda válida encontrada."
    output = ""
    rendered = 0
    for line in lines:
        candidate = output + line + "\n"
        suffix = f"\n… e mais <b>{len(lines) - rendered - 1}</b>. Use <code>.divulgar list</code> para consultar novamente."
        if len(candidate.rstrip()) + (len(suffix) if rendered + 1 < len(lines) else 0) > LIST_MAX_OUTPUT_CHARS:
            break
        output = candidate
        rendered += 1
    if rendered < len(lines):
        output += f"\n… e mais <b>{len(lines) - rendered}</b>. Use <code>.divulgar list</code> para consultar novamente."
    return output.rstrip()


async def _notify_divulgar_failure(bot, schedule_id: int, detail: str):
    schedule_id = int(schedule_id)
    now = time.monotonic()
    last = DIVULGAR_LAST_FAILURE_NOTIFY.get(schedule_id, 0.0)
    if now - last < DIVULGAR_FAILURE_NOTIFY_COOLDOWN_SECONDS:
        return
    DIVULGAR_LAST_FAILURE_NOTIFY[schedule_id] = now
    config = DIVULGAR_CONFIGS.get(schedule_id, {})
    chat_id = int(config.get("chat_id", 0))
    next_run_at = config.get("next_run_at")
    next_text = _format_divulgar_datetime(next_run_at) if next_run_at else "indisponível"
    await _send_divulgar_notification(
        bot,
        "⚠️ <b>Falha na divulgação</b>\n\n"
        f"Agendamento: <code>{schedule_id}</code>\n"
        f"Grupo: <code>{chat_id}</code>\n"
        f"Motivo: {_safe_html(detail)}\n"
        f"Próxima tentativa: <b>{next_text}</b>\n"
        "O agendamento continua ativo e tentará novamente no próximo ciclo.",
    )


async def _cancel_spam_task(chat_id: int):
    chat_id = int(chat_id)
    task = SPAM_TASKS.pop(chat_id, None)
    SPAM_CONFIGS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _send_spam_status(bot, chat_id: int, text: str):
    try:
        status_message = await _api_call(
            bot.send_message,
            chat_id=int(chat_id),
            text=text,
            parse_mode="HTML",
            disable_notification=True,
            operation_name="status do spam",
        )
        _schedule_delete(status_message)
    except asyncio.CancelledError:
        raise
    except TelegramError:
        logger.debug("Não foi possível enviar o status temporário do spam", exc_info=True)
    except Exception:
        logger.debug("Falha inesperada no status temporário do spam", exc_info=True)


async def _spam_worker(bot, chat_id: int, config: dict):
    chat_id = int(chat_id)
    requested = int(config["count"])
    sent = 0
    try:
        for index in range(requested):
            if SPAM_CONFIGS.get(chat_id) is not config:
                return
            try:
                source_message_id = config.get("source_message_id")
                if source_message_id is not None:
                    copy_kwargs = {
                        "chat_id": chat_id,
                        "from_chat_id": chat_id,
                        "message_id": int(source_message_id),
                    }
                    if config.get("caption_override") is not None:
                        copy_kwargs["caption"] = config["caption_override"]
                    await _api_call(
                        bot.copy_message,
                        operation_name="cópia de mensagem do spam",
                        retry_after_maximum=120.0,
                        **copy_kwargs,
                    )
                    followup_text = config.get("followup_text")
                    if followup_text:
                        await _api_call(
                            bot.send_message,
                            chat_id=chat_id,
                            text=followup_text,
                            operation_name="texto complementar do spam",
                            retry_after_maximum=120.0,
                        )
                else:
                    await _api_call(
                        bot.send_message,
                        chat_id=chat_id,
                        text=config["text"],
                        operation_name="envio de texto do spam",
                        retry_after_maximum=120.0,
                    )
                sent += 1
                BLACKLIST_TELEMETRY["spam_sent"] += 1
            except asyncio.CancelledError:
                raise
            except (BadRequest, Forbidden, TelegramError) as exc:
                BLACKLIST_TELEMETRY["spam_failed"] += 1
                BLACKLIST_TELEMETRY["last_spam_error"] = type(exc).__name__
                logger.warning(
                    "Spam interrompido em chat_id=%s após %s/%s mensagens: %s",
                    chat_id,
                    sent,
                    requested,
                    exc,
                )
                _track_task(
                    _send_spam_status(
                        bot,
                        chat_id,
                        f"⚠️ <b>Spam interrompido</b>: <code>{sent}/{requested}</code> mensagens enviadas. "
                        "A API recusou ou limitou a próxima publicação.",
                    )
                )
                return
            except Exception:
                BLACKLIST_TELEMETRY["spam_failed"] += 1
                BLACKLIST_TELEMETRY["last_spam_error"] = "internal_error"
                logger.exception("Falha inesperada no spam em chat_id=%s", chat_id)
                _track_task(
                    _send_spam_status(
                        bot,
                        chat_id,
                        f"⚠️ <b>Spam interrompido</b>: <code>{sent}/{requested}</code> mensagens enviadas por erro interno.",
                    )
                )
                return
            if index + 1 < requested:
                await asyncio.sleep(SPAM_DELAY_SECONDS)
        BLACKLIST_TELEMETRY["spam_completed"] += 1
        logger.info("Spam concluído em chat_id=%s: %s mensagens", chat_id, sent)
    except asyncio.CancelledError:
        logger.info("Spam cancelado em chat_id=%s após %s/%s mensagens", chat_id, sent, requested)
        raise
    finally:
        current = asyncio.current_task()
        if SPAM_TASKS.get(chat_id) is current:
            SPAM_TASKS.pop(chat_id, None)
        if SPAM_CONFIGS.get(chat_id) is config:
            SPAM_CONFIGS.pop(chat_id, None)
        BLACKLIST_TELEMETRY["spam_active"] = len(SPAM_TASKS)


async def _divulgar_worker(bot, schedule_id: int, config: dict):
    schedule_id = int(schedule_id)
    chat_id = int(config["chat_id"])
    try:
        next_run_at = float(config.get("next_run_at") or (time.time() + config["interval_seconds"]))
        if next_run_at <= time.time():
            next_run_at = time.time() + config["interval_seconds"]
            config["next_run_at"] = next_run_at
            await _db_call(db.update_divulgacao_next_run, schedule_id, next_run_at)
        while True:
            if DIVULGAR_CONFIGS.get(schedule_id) is not config:
                return
            await asyncio.sleep(max(0.0, next_run_at - time.time()))
            if DIVULGAR_CONFIGS.get(schedule_id) is not config:
                return
            sent = False
            failure_detail = None
            try:
                if config["content_type"] == "text":
                    await _api_call(bot.send_message,chat_id=chat_id, text=config["text"])
                elif config["content_type"] == "photo":
                    await _api_call(bot.send_photo,chat_id=chat_id, photo=config["file_id"], caption=config["text"] or None)
                else:
                    await _api_call(bot.send_video,chat_id=chat_id, video=config["file_id"], caption=config["text"] or None)
                sent = True
                DIVULGAR_LAST_FAILURE_NOTIFY.pop(schedule_id, None)
                logger.info("Divulgação enviada em chat_id=%s schedule_id=%s", chat_id, schedule_id)
            except RetryAfter as exc:
                delay = _retry_delay(exc, maximum=300.0)
                logger.warning("Divulgação limitada em chat_id=%s schedule_id=%s após retries; próxima tentativa em %.1fs", chat_id, schedule_id, delay)
                failure_detail = f"limite temporário do Telegram ({delay:.0f}s)"
            except Forbidden:
                logger.warning("Sem permissão para divulgar em chat_id=%s schedule_id=%s", chat_id, schedule_id, exc_info=True)
                failure_detail = "o bot não pode enviar mensagens neste grupo"
            except BadRequest as exc:
                logger.warning("Requisição inválida ao divulgar em chat_id=%s schedule_id=%s: %s", chat_id, schedule_id, exc)
                failure_detail = "a API recusou o conteúdo ou a operação"
            except TelegramError as exc:
                logger.warning("Falha Telegram ao divulgar em chat_id=%s schedule_id=%s: %s", chat_id, schedule_id, exc)
                failure_detail = type(exc).__name__
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Falha inesperada ao divulgar em chat_id=%s schedule_id=%s", chat_id, schedule_id)
                failure_detail = "erro interno inesperado"
            finally:
                next_run_at = time.time() + config["interval_seconds"]
                config["next_run_at"] = next_run_at
                try:
                    await _db_call(db.update_divulgacao_next_run, schedule_id, next_run_at)
                except Exception:
                    logger.exception("Não foi possível persistir o próximo envio de schedule_id=%s", schedule_id)
                if failure_detail:
                    _track_task(_notify_divulgar_failure(bot, schedule_id, failure_detail))
                if sent:
                    _queue_divulgar_notification(
                        bot,
                        "✅ <b>Divulgação enviada</b>\n\n"
                        f"Agendamento: <code>{schedule_id}</code>\n"
                        f"Grupo: <code>{chat_id}</code>\n"
                        f"Horário: <b>{_format_divulgar_datetime(time.time())}</b>\n"
                        f"Próximo envio: <b>{_format_divulgar_datetime(next_run_at)}</b>\n"
                        f"Intervalo: <code>{_format_divulgar_interval(config['interval_seconds'])}</code>",
                    )
    except asyncio.CancelledError:
        raise
    finally:
        current = asyncio.current_task()
        if DIVULGAR_TASKS.get(schedule_id) is current:
            DIVULGAR_TASKS.pop(schedule_id, None)


async def _ensure_divulgar_task(bot, schedule_id: int, config: dict | None = None):
    schedule_id = int(schedule_id)
    config = config or DIVULGAR_CONFIGS.get(schedule_id)
    if not config:
        return
    task = DIVULGAR_TASKS.get(schedule_id)
    if task and not task.done():
        return
    DIVULGAR_CONFIGS[schedule_id] = config
    DIVULGAR_TASKS[schedule_id] = _track_task(_divulgar_worker(bot, schedule_id, config))


def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP})


async def _get_chat_member_cached(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, *, strict: bool = False):
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    cached = CHAT_MEMBER_CACHE.get(key)
    if cached and cached[0] > now:
        if strict and cached[1] is None:
            raise TelegramError("consulta de membro indisponível")
        return cached[1]

    async def load_member():
        try:
            member = await _api_call(
                context.bot.get_chat_member,
                int(chat_id),
                int(user_id),
                operation_name="consulta de membro",
                retry_after_maximum=30.0,
            )
        except TelegramError:
            CHAT_MEMBER_CACHE[key] = (time.monotonic() + CHAT_MEMBER_ERROR_TTL, None)
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            CHAT_MEMBER_CACHE[key] = (time.monotonic() + CHAT_MEMBER_ERROR_TTL, None)
            logger.debug("Falha inesperada ao consultar membro", exc_info=True)
            return None
        CHAT_MEMBER_CACHE[key] = (time.monotonic() + CHAT_MEMBER_CACHE_TTL, member)
        if len(CHAT_MEMBER_CACHE) > 4096:
            expired = [cache_key for cache_key, (expires, _member) in CHAT_MEMBER_CACHE.items() if expires <= time.monotonic()]
            for cache_key in expired[:1024]:
                CHAT_MEMBER_CACHE.pop(cache_key, None)
        return member

    task = CHAT_MEMBER_INFLIGHT.get(key)
    if task is None or task.done():
        task = asyncio.create_task(load_member())
        CHAT_MEMBER_INFLIGHT[key] = task

        def clear_inflight(done_task, cache_key=key):
            if CHAT_MEMBER_INFLIGHT.get(cache_key) is done_task:
                CHAT_MEMBER_INFLIGHT.pop(cache_key, None)

        task.add_done_callback(clear_inflight)
    try:
        result = await asyncio.shield(task)
        if strict and result is None:
            raise TelegramError("consulta de membro indisponível")
        return result
    except asyncio.CancelledError:
        raise
    except TelegramError:
        if strict:
            raise
        return None
    except Exception:
        if strict:
            raise TelegramError("consulta de membro indisponível")
        return None


def _invalidate_chat_member_cache(chat_id: int):
    for key in [cache_key for cache_key in CHAT_MEMBER_CACHE if cache_key[0] == int(chat_id)]:
        CHAT_MEMBER_CACHE.pop(key, None)


async def _is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    member = await _get_chat_member_cached(chat.id, user.id, context)
    return bool(member and member.status in {"administrator", "creator"})


async def _require_owner_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_group(update):
        await _reply_and_cleanup(update, "❌ Este comando só pode ser usado em grupos ou supergrupos.")
        return False
    if update.effective_user and _is_owner(update.effective_user.id):
        return True
    await _reply_and_cleanup(update, "⛔ Somente os proprietários configurados podem usar os comandos deste bot.")
    return False


async def _require_command_access(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> bool:
    """Autoriza owners e usuários delegados somente nos comandos não restritos."""
    if not _is_group(update):
        await _reply_and_cleanup(update, "❌ Este comando só pode ser usado em grupos ou supergrupos.")
        return False
    user = update.effective_user
    if not user:
        return False
    if _is_owner(user.id):
        return True
    if command not in DELEGATED_COMMANDS or command in OWNER_ONLY_COMMANDS:
        return False
    chat = update.effective_chat
    if chat and user.id in AUTHORIZED_CACHE.get(int(chat.id), set()):
        return True
    # Usuários comuns continuam silenciosos, como no contrato anterior.
    return False


async def _bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    global BOT_USER_ID
    if not BOT_USER_ID:
        try:
            BOT_USER_ID = (await _api_call(context.bot.get_me, operation_name="identificação do bot")).id
        except TelegramError:
            return False
    member = await _get_chat_member_cached(chat_id, BOT_USER_ID, context)
    return bool(member and (member.status == "creator" or getattr(member, "can_restrict_members", False)))


async def _bot_can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    global BOT_USER_ID
    if not BOT_USER_ID:
        try:
            BOT_USER_ID = (await _api_call(context.bot.get_me, operation_name="identificação do bot")).id
        except TelegramError:
            return False
    member = await _get_chat_member_cached(chat_id, BOT_USER_ID, context)
    return bool(member and (member.status == "creator" or getattr(member, "can_delete_messages", False)))


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Target | None:
    message = update.effective_message
    if message is None:
        return None
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        previous = KNOWN_USERS.get(int(reply.from_user.id))
        target = _remember_user_in_memory(reply.from_user)
        if previous != (target.username, target.full_name):
            await _db_call(db.remember_user, target.user_id, target.username, target.full_name)
        return target

    args = list(context.args or [])
    if not args:
        return None
    raw = args[0].strip()
    if re.fullmatch(rf"[0-9]{{1,{MAX_TARGET_ID_LENGTH}}}", raw):
        user_id = int(raw)
        if user_id <= 0:
            return None
        username, full_name = KNOWN_USERS.get(user_id, ("", ""))
        return Target(user_id, username, full_name)
    if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw):
        username_key = raw[1:].lower()
        uid = KNOWN_USERNAME_IDS.get(username_key)
        if uid is not None:
            username, full_name = KNOWN_USERS[uid]
            return Target(uid, username, full_name)
        row = await _db_call(db.resolve_username, raw[1:])
        if row:
            keys = row.keys()
            return Target(
                int(row["user_id"]),
                row["username"] if "username" in keys else "",
                row["full_name"] if "full_name" in keys else "",
            )
    return None


def _combine_spam_text(base: str, extra: str, maximum: int) -> str | None:
    parts = [str(value or "").strip() for value in (base, extra) if str(value or "").strip()]
    combined = "\n\n".join(parts)
    if len(combined) > int(maximum):
        return None
    return combined


def _spam_caption_capable(message) -> bool:
    return bool(
        getattr(message, "photo", None)
        or getattr(message, "video", None)
        or getattr(message, "animation", None)
        or getattr(message, "document", None)
        or getattr(message, "audio", None)
        or getattr(message, "voice", None)
    )


def _reason(context: ContextTypes.DEFAULT_TYPE) -> str:
    args = list(context.args or [])
    return " ".join(args[1:]).strip()[:500]


def _target_error(command: str) -> str:
    return (
        f"❌ Informe o alvo para <code>.{command}</code>: "
        f"responda à mensagem do usuário, use um ID numérico ou um @username que o bot já tenha registrado. "
        f"Um username pessoal desconhecido não pode ser convertido em ID pela Bot API."
    )


async def _remember_message_context(update: Update):
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if chat.id not in _chat_registration_seen:
        _chat_registration_seen.add(chat.id)
        KNOWN_CHAT_IDS.add(chat.id)
        await _db_call(db.register_chat, chat.id, chat.title or "", chat.type)
    if not user.is_bot:
        user_id = int(user.id)
        previous = KNOWN_USERS.get(user_id)
        target = _remember_user_in_memory(user)
        if previous != (target.username, target.full_name):
            await _db_call(db.remember_user, target.user_id, target.username, target.full_name)


async def _safe_delete(message) -> bool:
    try:
        await message.delete()
        return True
    except (BadRequest, Forbidden, TelegramError):
        return False


async def cmd_jt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gerencia usuários autorizados por grupo; o comando permanece exclusivo dos owners."""
    if not await _require_owner_access(update, context):
        return
    chat = update.effective_chat
    operator = update.effective_user
    if not chat or not operator:
        return
    chat_id = int(chat.id)
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    action = args[0].lower() if args else "add"

    if action == "list":
        if len(args) > 2:
            await _reply_and_cleanup(update, "❌ Uso: <code>.jt list</code> ou <code>.jt list N</code>.")
            return
        page = 1
        if len(args) == 2:
            page = _parse_list_page(args[1])
        if page is None or page < 1:
            await _reply_and_cleanup(update, "❌ O número da página deve ser um inteiro positivo.")
            return
        total, rows = await asyncio.gather(
            _db_call(db.count_authorized_for_chat, chat_id),
            _db_call(db.get_authorized_for_chat_page, chat_id, LIST_MAX_VISIBLE_ENTRIES, (page - 1) * LIST_MAX_VISIBLE_ENTRIES),
        )
        await _reply_and_cleanup(
            update,
            _format_user_list(
                rows,
                total=total,
                page=page,
                title="✅ <b>Usuários autorizados</b>",
                empty_text="ℹ️ Não há usuários autorizados neste grupo.",
                scope_text="Eles podem usar somente os comandos delegáveis do Bot API.",
                next_page_command=".jt list",
            ),
        )
        return

    revoke = action in {"off", "remove", "revoke", "revogar", "remover"}
    target_args = args[1:] if revoke else args
    if len(target_args) > 1:
        await _reply_and_cleanup(
            update,
            "❌ Informe apenas um alvo por vez: responda à mensagem, use um ID ou um @username.",
        )
        return
    original_args = getattr(context, "args", None)
    context.args = target_args
    try:
        target = await _resolve_target(update, context)
    finally:
        context.args = original_args
    if target is None:
        command = ".jt off" if revoke else ".jt"
        await _reply_and_cleanup(
            update,
            f"❌ Uso: <code>{command}</code> respondendo à mensagem do usuário, ou "
            f"<code>{command} ID/@username</code>. Para consultar: <code>.jt list</code>.",
        )
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "ℹ️ Os owners já têm acesso total e não precisam ser autorizados.")
        return

    already_authorized = target.user_id in AUTHORIZED_CACHE.get(chat_id, set())
    if not already_authorized:
        already_authorized = await _db_call(db.has_authorized, target.user_id, chat_id)

    if revoke:
        removed = await _db_call(db.remove_authorized, target.user_id, chat_id)
        AUTHORIZED_CACHE.get(chat_id, set()).discard(target.user_id)
        if removed:
            await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> não pode mais usar os comandos delegáveis neste grupo.")
        elif already_authorized:
            await _reply_and_cleanup(update, f"ℹ️ A autorização de <b>{_safe_html(target.label)}</b> já não estava registrada no banco.")
        else:
            await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava autorizado neste grupo.")
        return

    added = await _db_call(db.add_authorized, target, chat_id, operator.id)
    if not added:
        await _reply_and_cleanup(update, "❌ Não foi possível persistir a autorização.")
        return
    AUTHORIZED_CACHE[chat_id].add(target.user_id)
    if already_authorized:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já estava autorizado neste grupo; os dados foram atualizados.")
    else:
        await _reply_and_cleanup(
            update,
            f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) foi autorizado neste grupo.\n"
            "Ele pode usar apenas os comandos delegáveis; comandos de gestão e owner continuam bloqueados.",
        )


async def cmd_divulgar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_owner_access(update, context):
        return
    if not _is_group(update):
        await _reply_and_cleanup(update, "❌ O `.divulgar` só pode ser usado em grupos ou supergrupos.")
        return
    chat = update.effective_chat
    message = update.effective_message
    chat_id = int(chat.id)
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    command = args[0].lower() if args else ""

    if command == "list" and len(args) == 1:
        rows = await _db_call(db.get_divulgacoes_for_chat, chat_id)
        if not rows:
            await _reply_and_cleanup(update, "ℹ️ Não há divulgações ativas neste grupo.")
            return
        await _reply_and_cleanup(
            update,
            "📋 <b>Divulgações ativas neste grupo</b>\n\n"
            f"{_divulgar_list_text(rows)}\n\n"
            "Para cancelar uma específica, use <code>.divulgar off ID</code>.",
        )
        return

    if command == "off":
        rows = await _db_call(db.get_divulgacoes_for_chat, chat_id)
        if not rows:
            await _reply_and_cleanup(update, "ℹ️ Não havia divulgação ativa neste grupo.")
            return
        if len(args) == 1:
            if len(rows) > 1:
                await _reply_and_cleanup(
                    update,
                    "⚠️ Há várias divulgações ativas. Nenhuma foi cancelada.\n\n"
                    f"{_divulgar_list_text(rows)}\n\n"
                    "Use <code>.divulgar off ID</code> para cancelar uma ou "
                    "<code>.divulgar off all</code> para cancelar todas.",
                )
                return
            target_row = rows[0]
        elif len(args) == 2 and args[1].lower() == "all":
            for row in rows:
                schedule_id = int(row["schedule_id"])
                await _cancel_divulgar_task(schedule_id)
                DIVULGAR_CONFIGS.pop(schedule_id, None)
                DIVULGAR_LAST_FAILURE_NOTIFY.pop(schedule_id, None)
            removed = await _db_call(db.remove_all_divulgacoes_for_chat, chat_id)
            await _reply_and_cleanup(update, f"✅ {removed} divulgação(ões) desligada(s) neste grupo.")
            _queue_divulgar_notification(
                context.bot,
                "⏹️ <b>Todas as divulgações foram desligadas</b>\n\n"
                f"Grupo: <code>{chat_id}</code>\n"
                f"Quantidade: <b>{removed}</b>\n"
                f"Horário: <b>{_format_divulgar_datetime(time.time())}</b>",
            )
            return
        elif len(args) == 2:
            try:
                requested_id = int(args[1])
            except (TypeError, ValueError):
                requested_id = 0
            target_row = next((row for row in rows if int(row["schedule_id"]) == requested_id), None)
            if target_row is None:
                await _reply_and_cleanup(update, "❌ ID de agendamento inválido para este grupo. Use <code>.divulgar list</code> para consultar os IDs ativos.")
                return
        else:
            target_row = None

        if target_row is None:
            await _reply_and_cleanup(
                update,
                "ℹ️ Uso: <code>.divulgar off</code> quando há apenas uma agenda, "
                "<code>.divulgar off ID</code> para uma específica ou "
                "<code>.divulgar off all</code> para todas.",
            )
            return
        schedule_id = int(target_row["schedule_id"])
        await _cancel_divulgar_task(schedule_id)
        DIVULGAR_CONFIGS.pop(schedule_id, None)
        DIVULGAR_LAST_FAILURE_NOTIFY.pop(schedule_id, None)
        removed = await _db_call(db.remove_divulgacao, schedule_id)
        if removed:
            await _reply_and_cleanup(update, f"✅ Divulgação <code>{schedule_id}</code> desligada neste grupo.")
            _queue_divulgar_notification(
                context.bot,
                "⏹️ <b>Divulgação desligada</b>\n\n"
                f"Agendamento: <code>{schedule_id}</code>\n"
                f"Grupo: <code>{chat_id}</code>\n"
                f"Horário: <b>{_format_divulgar_datetime(time.time())}</b>",
            )
        else:
            await _reply_and_cleanup(update, "ℹ️ Esse agendamento já não está ativo.")
        return

    if len(args) != 2 or args[1].lower() != "on":
        await _reply_and_cleanup(
            update,
            "ℹ️ Uso: responda a uma mensagem com <code>.divulgar 30m on</code> para criar uma nova agenda.\n"
            "<code>.divulgar list</code> — lista as agendas ativas.\n"
            "<code>.divulgar off ID</code> — desliga uma agenda específica.\n"
            "<code>.divulgar off all</code> — desliga todas.\n"
            "Intervalo permitido: de 30s a 30d; máximo de "
            f"{DIVULGAR_MAX_SCHEDULES_PER_CHAT} agendas por grupo.",
        )
        return

    interval_seconds = _parse_divulgar_interval(args[0])
    if interval_seconds is None:
        await _reply_and_cleanup(update, "❌ Intervalo inválido. Use, por exemplo, <code>30s</code>, <code>30m</code>, <code>2h</code> ou <code>1d</code>; o mínimo é 30s.")
        return
    source, error = _extract_divulgacao(message.reply_to_message if message else None)
    if error:
        await _reply_and_cleanup(update, error)
        return
    next_run_at = time.time() + interval_seconds
    schedule_id = await _db_call(
        db.save_divulgacao_if_capacity,
        chat_id,
        interval_seconds,
        source["content_type"],
        source["text"],
        source["file_id"],
        source["source_message_id"],
        update.effective_user.id,
        DIVULGAR_MAX_SCHEDULES_PER_CHAT,
        next_run_at,
    )
    if schedule_id is None:
        await _reply_and_cleanup(
            update,
            f"❌ Este grupo já atingiu o limite de {DIVULGAR_MAX_SCHEDULES_PER_CHAT} divulgações simultâneas. "
            "Desligue uma agenda com <code>.divulgar off ID</code> antes de criar outra.",
        )
        return
    config = {
        "schedule_id": int(schedule_id),
        "chat_id": chat_id,
        "interval_seconds": interval_seconds,
        "content_type": source["content_type"],
        "text": source["text"],
        "file_id": source["file_id"],
        "source_message_id": source["source_message_id"],
        "owner_id": int(update.effective_user.id),
        "next_run_at": next_run_at,
    }
    DIVULGAR_CONFIGS[int(schedule_id)] = config
    DIVULGAR_LAST_FAILURE_NOTIFY.pop(int(schedule_id), None)
    try:
        await _ensure_divulgar_task(context.bot, schedule_id, config)
    except asyncio.CancelledError:
        DIVULGAR_CONFIGS.pop(int(schedule_id), None)
        await _db_call(db.remove_divulgacao, int(schedule_id))
        raise
    except Exception:
        DIVULGAR_CONFIGS.pop(int(schedule_id), None)
        DIVULGAR_LAST_FAILURE_NOTIFY.pop(int(schedule_id), None)
        await _db_call(db.remove_divulgacao, int(schedule_id))
        logger.exception("Falha ao iniciar divulgação schedule_id=%s após persistência", schedule_id)
        await _reply_and_cleanup(update, "❌ Não foi possível iniciar a divulgação; nenhuma agenda foi deixada ativa.")
        return
    active_rows = await _db_call(db.get_divulgacoes_for_chat, chat_id)
    await _reply_and_cleanup(
        update,
        f"✅ Divulgação <code>{schedule_id}</code> ativada neste grupo a cada "
        f"<b>{_format_divulgar_interval(interval_seconds)}</b>.\n"
        f"A primeira publicação ocorrerá em <b>{_format_divulgar_datetime(next_run_at)}</b>.\n"
        "Use <code>.divulgar list</code> para consultar as agendas.",
    )
    _queue_divulgar_notification(
        context.bot,
        "✅ <b>Divulgação ativada</b>\n\n"
        f"Agendamento: <code>{schedule_id}</code>\n"
        f"Grupo: <code>{chat_id}</code>\n"
        f"Conteúdo: <b>{_safe_html(source['content_type'])}</b>\n"
        f"Intervalo: <code>{_format_divulgar_interval(interval_seconds)}</code>\n"
        f"Primeiro envio: <b>{_format_divulgar_datetime(next_run_at)}</b>\n"
        f"Agendas ativas neste grupo: <b>{len(active_rows) + 1}</b>\n"
        "Após cada envio, você receberá o horário da publicação e o próximo agendamento.",
    )


async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_owner_access(update, context):
        return
    if not _is_group(update):
        await _reply_and_cleanup(update, "❌ O `.spam` só pode ser usado em grupos ou supergrupos.")
        return
    message = update.effective_message
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    if not args:
        await _reply_and_cleanup(
            update,
            "ℹ️ Uso: responda a uma mensagem com <code>.spam 10</code> ou use "
            "<code>.spam 10 seu texto</code>. O limite é de 1 a 100.",
        )
        return
    chat_id = int(update.effective_chat.id)
    if args[0].lower() == "off":
        if len(args) != 1:
            await _reply_and_cleanup(update, "ℹ️ Uso: <code>.spam off</code> para cancelar o spam em andamento neste grupo.")
            return
        if not SPAM_TASKS.get(chat_id) or SPAM_TASKS[chat_id].done():
            SPAM_TASKS.pop(chat_id, None)
            SPAM_CONFIGS.pop(chat_id, None)
            BLACKLIST_TELEMETRY["spam_active"] = len(SPAM_TASKS)
            await _reply_and_cleanup(update, "ℹ️ Não há spam em andamento neste grupo.")
            return
        await _cancel_spam_task(chat_id)
        BLACKLIST_TELEMETRY["spam_active"] = len(SPAM_TASKS)
        await _reply_and_cleanup(update, "⏹️ Spam cancelado com segurança neste grupo.")
        return
    try:
        count = int(args[0])
    except (TypeError, ValueError):
        count = 0
    if not SPAM_MIN_COUNT <= count <= SPAM_MAX_COUNT:
        await _reply_and_cleanup(update, f"❌ A quantidade deve ser um número entre {SPAM_MIN_COUNT} e {SPAM_MAX_COUNT}.")
        return

    custom_text = " ".join(args[1:]).strip()
    reply = message.reply_to_message if message else None
    if not reply and not custom_text:
        await _reply_and_cleanup(
            update,
            "❌ Responda a uma mensagem ou informe o texto: <code>.spam 10 seu texto</code>.",
        )
        return
    if custom_text and len(custom_text) > SPAM_MAX_TEXT_LENGTH:
        await _reply_and_cleanup(update, f"❌ O texto do spam não pode exceder {SPAM_MAX_TEXT_LENGTH} caracteres.")
        return

    source_message_id = int(reply.message_id) if reply else None
    spam_text = custom_text
    caption_override = None
    followup_text = None
    source_label = "texto informado"
    if reply:
        source_label = "mensagem respondida"
        if custom_text and getattr(reply, "text", None):
            spam_text = _combine_spam_text(reply.text, custom_text, SPAM_MAX_TEXT_LENGTH)
            if spam_text is None:
                await _reply_and_cleanup(update, f"❌ A mensagem combinada não pode exceder {SPAM_MAX_TEXT_LENGTH} caracteres.")
                return
            source_message_id = None
            source_label = "mensagem respondida + texto adicional"
        elif custom_text and _spam_caption_capable(reply):
            base_caption = getattr(reply, "caption", "") or ""
            caption_override = _combine_spam_text(base_caption, custom_text, SPAM_MAX_CAPTION_LENGTH)
            if caption_override is None:
                await _reply_and_cleanup(update, f"❌ A legenda combinada não pode exceder {SPAM_MAX_CAPTION_LENGTH} caracteres.")
                return
            source_label = "mídia respondida + legenda adicional"
        elif custom_text:
            # Stickers, video notes e outros tipos sem legenda são copiados e
            # recebem o complemento como uma mensagem imediatamente posterior.
            followup_text = custom_text
            source_label = "mídia respondida + texto complementar"
        elif getattr(reply, "text", None):
            source_label = "mensagem respondida"

    existing = SPAM_TASKS.get(chat_id)
    if existing and not existing.done():
        await _reply_and_cleanup(
            update,
            "⚠️ Já existe um spam em andamento neste grupo. Aguarde a conclusão ou use "
            "<code>.spam off</code> para cancelá-lo.",
        )
        return

    config = {
        "chat_id": chat_id,
        "count": count,
        "source_message_id": source_message_id,
        "text": spam_text,
        "caption_override": caption_override,
        "followup_text": followup_text,
        "owner_id": int(update.effective_user.id),
    }
    SPAM_CONFIGS[chat_id] = config
    SPAM_TASKS[chat_id] = _track_task(_spam_worker(context.bot, chat_id, config))
    BLACKLIST_TELEMETRY["spam_started"] += 1
    BLACKLIST_TELEMETRY["spam_active"] = len(SPAM_TASKS)
    await _reply_and_cleanup(
        update,
        f"🚀 Spam iniciado: <b>{count}</b> repetição(ões) usando {source_label}.\n"
        "Use <code>.spam off</code> para cancelar com segurança.",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_and_cleanup(
        update,
        "🛡️ <b>Jtzin Administrator Bot</b>\n\n"
        "Bot API dedicado a blacklist local, banimento permanente local e JTBN global.\n"
        "Use .help para consultar a forma de uso.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_command_access(update, context, "help"):
        return
    await _reply_and_cleanup(
        update,
        "🛡️ <b>Jtzin Administrator Bot</b>\n\n"
        "<b>Comandos disponíveis</b>\n"
        "<code>.jt</code> — owner autoriza um usuário por reply, ID ou @username neste grupo.\n"
        "<code>.jt off</code> — revoga a autorização de um usuário; <code>.jt list [página]</code> lista os autorizados.\n"
        "Usuários autorizados podem usar apenas <code>.help</code>, <code>.blacklist</code>, <code>.unblacklist</code>, <code>.jtperm</code>, <code>.unjtperm</code> e <code>.latency</code>; comandos owner permanecem bloqueados.\n"
        "<code>.blacklist</code> — adiciona o alvo à blacklist deste grupo e remove texto, mídias e reações permitidas pela Bot API.\n"
        "<code>.blacklist list</code> — lista a blacklist local deste grupo.\n"
        "<code>.unblacklist</code> — remove a blacklist local.\n"
        "<code>.jtperm</code> — bane permanentemente o alvo deste grupo e reaplica o bloqueio se ele tentar reentrar.\n"
        "<code>.jtperm list [página]</code> — lista os banimentos permanentes deste grupo.\n"
        "<code>.unjtperm</code> — remove o banimento deste grupo.\n"
        "<code>.jtbn</code> — o proprietário bane o alvo nos grupos registrados.\n"
        "<code>.jtbn list</code> — lista os usuários no JTBN global.\n"
        "<code>.unjtbn</code> — remove o JTBN global e tenta desbanir o alvo.\n"
        "<code>.lock</code> — fecha o grupo para membros; administradores e o dono continuam podendo falar.\n"
        "<code>.unlock</code> — abre o grupo e restaura as permissões anteriores.\n"
        "<code>.latency</code> — mede uma chamada real à API do Telegram.\n"
        "<code>.divulgar 30m on</code> — cria uma nova agenda para texto, foto ou vídeo respondido.\n"
        "<code>.divulgar list</code> — lista as agendas ativas com seus IDs.\n"
        "<code>.divulgar off ID</code> — desliga uma agenda específica; <code>off all</code> desliga todas.\n"
        "<code>.spam N</code> — repete uma mensagem respondida de 1 a 100 vezes.\n"
        "<code>.spam N texto</code> — repete um texto; ao responder uma fonte, acrescenta esse texto à mensagem/legenda.\n"
        "Em sticker ou mídia sem legenda, o texto adicional é enviado logo após a cópia; <code>.spam off</code> cancela.\n"
        "O spam aceita texto, foto, vídeo, GIF, sticker, documento, áudio, voz e outras mídias copiáveis.\n"
        "Cada envio usa retry isolado e uma falha de notificação privada não interrompe o agendamento.\n\n"
        "Somente os dois proprietários configurados têm acesso total; usuários autorizados pelo <code>.jt</code> recebem somente os comandos delegáveis deste grupo. "
        "A moderação local ainda exige que o bot seja administrador com permissão para apagar mensagens "
        "e restringir membros.",
    )


async def _require_operator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and _is_owner(user.id):
        return True
    await _reply_and_cleanup(update, "⛔ Somente os proprietários configurados podem usar os comandos deste bot.")
    return False


def _lock_permission_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, Forbidden) or "administrator" in text or "permission" in text:
        return "❌ O Bot API precisa ser administrador do grupo com permissão para restringir membros."
    if "not enough rights" in text or "rights" in text:
        return "❌ O Bot API não tem direitos suficientes para alterar as permissões deste grupo."
    return "❌ Não foi possível alterar as permissões do grupo. Tente novamente."


async def _capture_lock_owner_overrides(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Retorna permissões a restaurar para owners que estejam restritos antes do lock."""
    owner_restore: dict[int, ChatPermissions | None] = {}
    if not callable(getattr(context.bot, "get_chat_member", None)):
        return owner_restore, None
    for owner_id in sorted(OWNER_IDS):
        try:
            member = await _get_chat_member_cached(chat_id, owner_id, context, strict=True)
        except TelegramError:
            logger.warning("Não foi possível verificar o owner %s antes do lock no grupo %s", owner_id, chat_id, exc_info=True)
            return None, owner_id
        if member is None or member.status in {"administrator", "creator", "left", "kicked"}:
            continue
        if member.status == "restricted":
            permissions = getattr(member, "permissions", None)
            if not isinstance(permissions, ChatPermissions):
                return None, owner_id
            owner_restore[owner_id] = permissions
        else:
            # O owner era um membro comum; depois do unlock o snapshot padrão já o restaura.
            owner_restore[owner_id] = None
    return owner_restore, None


async def _apply_lock_owner_overrides(chat_id: int, context: ContextTypes.DEFAULT_TYPE, owner_restore: dict[int, ChatPermissions | None]):
    failures = []
    for owner_id in owner_restore:
        try:
            await _api_call(
                context.bot.restrict_chat_member,
                chat_id=chat_id,
                user_id=owner_id,
                permissions=ChatPermissions.all_permissions(),
                use_independent_chat_permissions=True,
                operation_name="liberação do owner durante lock",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append((owner_id, exc))
    _invalidate_chat_member_cache(chat_id)
    return failures


async def _restore_lock_owner_overrides(chat_id: int, context: ContextTypes.DEFAULT_TYPE, owner_restore: dict[int, ChatPermissions | None]):
    failures = []
    for owner_id, permissions in owner_restore.items():
        if permissions is None:
            continue
        try:
            await _api_call(
                context.bot.restrict_chat_member,
                chat_id=chat_id,
                user_id=owner_id,
                permissions=permissions,
                use_independent_chat_permissions=True,
                operation_name="restauração da permissão original do owner",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append((owner_id, exc))
    _invalidate_chat_member_cache(chat_id)
    return failures


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_owner_access(update, context):
        return
    chat = update.effective_chat
    operator = update.effective_user
    if not chat or not operator:
        return
    existing = await _db_call(db.get_chat_lock, chat.id)
    if existing:
        await _reply_and_cleanup(update, "ℹ️ O grupo já está fechado pelo Bot API.")
        return
    try:
        current_chat = await _api_call(
            context.bot.get_chat,
            chat_id=chat.id,
            operation_name="leitura das permissões do grupo",
        )
        current_permissions = getattr(current_chat, "permissions", None)
        if not isinstance(current_permissions, ChatPermissions):
            await _reply_and_cleanup(update, "❌ Não consegui ler as permissões atuais deste grupo.")
            return
        owner_restore, failed_owner = await _capture_lock_owner_overrides(chat.id, context)
        if owner_restore is None:
            await _reply_and_cleanup(
                update,
                f"❌ Não consegui ler as permissões originais do owner <code>{failed_owner}</code>; "
                "o lock não foi aplicado para evitar alterar o estado dele.",
            )
            return
        snapshot = _lock_snapshot_json(current_permissions, owner_restore)
        saved = await _db_call(db.save_chat_lock, chat.id, snapshot, operator.id)
        if not saved:
            await _reply_and_cleanup(update, "❌ Não consegui salvar o estado atual do grupo; o lock não foi aplicado.")
            return
        try:
            await _api_call(
                context.bot.set_chat_permissions,
                chat_id=chat.id,
                permissions=_locked_permissions(),
                use_independent_chat_permissions=True,
                operation_name="bloqueio de mensagens do grupo",
            )
            override_failures = await _apply_lock_owner_overrides(chat.id, context, owner_restore)
            if override_failures:
                raise TelegramError(
                    "não foi possível liberar todos os owners durante o lock: "
                    + ", ".join(str(owner_id) for owner_id, _exc in override_failures)
                )
        except Exception:
            try:
                await _restore_lock_owner_overrides(chat.id, context, owner_restore)
            except Exception:
                logger.exception("Falha ao restaurar exceções de owners após lock incompleto no grupo %s", chat.id)
            try:
                await _api_call(
                    context.bot.set_chat_permissions,
                    chat_id=chat.id,
                    permissions=current_permissions,
                    use_independent_chat_permissions=True,
                    operation_name="rollback do bloqueio de mensagens",
                )
            except Exception:
                logger.exception("Falha ao reverter permissões após lock incompleto no grupo %s", chat.id)
            await _db_call(db.remove_chat_lock, chat.id)
            raise
    except (BadRequest, Forbidden, TelegramError) as exc:
        await _reply_and_cleanup(update, _lock_permission_error(exc))
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Falha inesperada ao fechar o grupo %s", chat.id)
        await _db_call(db.remove_chat_lock, chat.id)
        await _reply_and_cleanup(update, "❌ Ocorreu um erro interno; o lock não foi concluído.")
        return
    await _reply_and_cleanup(
        update,
        "🔒 <b>Grupo fechado.</b> Apenas administradores e os dois proprietários configurados podem enviar mensagens; "
        "o Bot API continua podendo publicar e executar suas tarefas administrativas.",
    )


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_owner_access(update, context):
        return
    chat = update.effective_chat
    if not chat:
        return
    saved = await _db_call(db.get_chat_lock, chat.id)
    if not saved:
        await _reply_and_cleanup(update, "ℹ️ O grupo já está aberto ou não há um lock do Bot API registrado.")
        return
    decoded = _decode_lock_snapshot(saved.get("permissions_json", ""))
    if decoded is None:
        await _reply_and_cleanup(update, "❌ O snapshot de permissões está inválido; não alterei o grupo para evitar perda de configuração.")
        return
    permissions, owner_restore = decoded
    try:
        await _api_call(
            context.bot.set_chat_permissions,
            chat_id=chat.id,
            permissions=permissions,
            use_independent_chat_permissions=True,
            operation_name="restauração das permissões do grupo",
        )
        owner_failures = await _restore_lock_owner_overrides(chat.id, context, owner_restore)
        if owner_failures:
            logger.error(
                "Grupo %s aberto, mas não foi possível restaurar owners: %s",
                chat.id,
                ", ".join(str(owner_id) for owner_id, _exc in owner_failures),
            )
            await _reply_and_cleanup(
                update,
                "⚠️ O grupo foi aberto, mas não consegui restaurar as permissões individuais de "
                + ", ".join(f"<code>{owner_id}</code>" for owner_id, _exc in owner_failures)
                + ". O snapshot foi preservado; tente <code>.unlock</code> novamente.",
            )
            return
        await _db_call(db.remove_chat_lock, chat.id)
    except (BadRequest, Forbidden, TelegramError) as exc:
        await _reply_and_cleanup(update, _lock_permission_error(exc))
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Falha inesperada ao abrir o grupo %s", chat.id)
        await _reply_and_cleanup(update, "❌ Ocorreu um erro interno; as permissões salvas continuam preservadas para uma nova tentativa.")
        return
    await _reply_and_cleanup(update, "🔓 <b>Grupo aberto.</b> As permissões anteriores foram restauradas.")


async def cmd_latency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_command_access(update, context, "latency"):
        return
    started = time.perf_counter()
    try:
        await _api_call(context.bot.get_me, operation_name="diagnóstico de latência")
        api_ms = (time.perf_counter() - started) * 1000
        api_status = f"✅ disponível ({api_ms:.0f} ms)"
    except TelegramError as exc:
        api_ms = None
        api_status = f"❌ indisponível ({type(exc).__name__})"
    total_ms = (time.perf_counter() - started) * 1000
    await _reply_and_cleanup(
        update,
        "⚡ <b>Diagnóstico de latência — Bot API</b>\n\n"
        f"• API Telegram: {api_status}\n"
        f"• Tempo total: <code>{total_ms:.0f} ms</code>\n"
        f"• Blacklist: <code>{BLACKLIST_TELEMETRY['matched']}</code> mensagens detectadas\n"
        f"• Último update: <code>{_format_ms(BLACKLIST_TELEMETRY['last_update_age_ms'])}</code>\n"
        f"• Fila local: <code>{_format_ms(BLACKLIST_TELEMETRY['last_queue_ms'])}</code>\n"
        f"• Último RPC de exclusão: <code>{_format_ms(BLACKLIST_TELEMETRY['last_delete_rpc_ms'])}</code>\n"
        f"• Maior RPC de exclusão: <code>{_format_ms(BLACKLIST_TELEMETRY['max_delete_rpc_ms'])}</code>\n"
        f"• Exclusões: <code>{BLACKLIST_TELEMETRY['delete_success']}</code> OK / <code>{BLACKLIST_TELEMETRY['delete_failed']}</code> falhas\n"
        f"• Lotes nativos: <code>{BLACKLIST_TELEMETRY['batch_success']}</code> / <code>{BLACKLIST_TELEMETRY['batch_messages']}</code> mensagens\n"
        f"• Fallbacks individuais: <code>{BLACKLIST_TELEMETRY['batch_fallbacks']}</code>\n"
        f"• Comandos: <code>{BLACKLIST_TELEMETRY['command_completed']}</code> concluídos / <code>{BLACKLIST_TELEMETRY['command_failed']}</code> falhas\n"
        f"• Duração do comando: <code>{_format_ms(BLACKLIST_TELEMETRY['last_command_ms'])}</code> último / <code>{_format_ms(BLACKLIST_TELEMETRY['max_command_ms'])}</code> máximo\n"
        f"• Retries: <code>{BLACKLIST_TELEMETRY['retry_after_events']}</code> flood / <code>{BLACKLIST_TELEMETRY['network_errors']}</code> rede\n"
        f"• Polling: <code>{BLACKLIST_TELEMETRY['polling_errors']}</code> falhas recuperadas; última <code>{_safe_html(BLACKLIST_TELEMETRY['last_polling_error'] or 'nenhuma')}</code>\n"
        f"• Último erro: <code>{_safe_html(BLACKLIST_TELEMETRY['last_error'] or 'nenhum')}</code>\n"
        f"• Workers: <code>{BLACKLIST_TELEMETRY['background_task_errors']}</code> falhas capturadas\n"
        f"• Spam: <code>{BLACKLIST_TELEMETRY['spam_active']}</code> ativo; <code>{BLACKLIST_TELEMETRY['spam_started']}</code> iniciados; <code>{BLACKLIST_TELEMETRY['spam_completed']}</code> concluídos; <code>{BLACKLIST_TELEMETRY['spam_sent']}</code> enviados; <code>{BLACKLIST_TELEMETRY['spam_failed']}</code> falhas\n"
        f"• Banperm reentrada: <code>{BLACKLIST_TELEMETRY['banperm_reentry_attempted']}</code> tentativas / <code>{BLACKLIST_TELEMETRY['banperm_reentry_success']}</code> OK / <code>{BLACKLIST_TELEMETRY['banperm_reentry_failed']}</code> falhas\n"
        f"• Reações blacklistadas: <code>{BLACKLIST_TELEMETRY['reaction_detected']}</code> detectadas / <code>{BLACKLIST_TELEMETRY['reaction_removed']}</code> removidas / <code>{BLACKLIST_TELEMETRY['reaction_remove_failed']}</code> falhas\n"
        "• Polling: ✅ processo monitorado pelo watchdog + heartbeat\n"
        "• Userbot: ⏸️ desligado\n"
        "\nA medição separa atraso do update, fila local e tempo do RPC de exclusão."

    )


async def cmd_unblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_command_access(update, context, "unblacklist"):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("unblacklist"))
        return
    chat_id = update.effective_chat.id
    was_cached = target.user_id in BLACKLIST_CACHE.get(chat_id, set())
    removed = await _db_call(db.remove_blacklist, target.user_id, chat_id)
    BLACKLIST_CACHE.get(chat_id, set()).discard(target.user_id)
    if not removed and not was_cached:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava na blacklist deste grupo.")
        return
    await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) removido da blacklist local.")


DOT_COMMAND_RE = re.compile(r"^\.(help|jt|unblacklist|unjtperm|unjtbn|blacklist|jtperm|jtbn|lock|unlock|latency|divulgar|spam)(?:\s+.*)?$", re.IGNORECASE)
DOT_COMMANDS = {
    "help": "cmd_help",
    "blacklist": "cmd_blacklist",
    "unblacklist": "cmd_unblacklist",
    "jtperm": "cmd_banperm",
    "unjtperm": "cmd_unbanperm",
    "jtbn": "cmd_jtbn",
    "unjtbn": "cmd_unjtbn",
    "lock": "cmd_lock",
    "unlock": "cmd_unlock",
    "latency": "cmd_latency",
    "divulgar": "cmd_divulgar",
    "spam": "cmd_spam",
    "jt": "cmd_jt",
}


async def on_dot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    text = (message.text or "").strip() if message else ""
    match = DOT_COMMAND_RE.fullmatch(text)
    if not match:
        return
    parts = text.split()
    command = parts[0][1:].lower()
    if not message.from_user or not _is_owner(message.from_user.id):
        # Não owners só passam se o comando estiver delegado neste grupo.
        if not message.from_user or command not in DELEGATED_COMMANDS or not await _require_command_access(update, context, command):
            # Não responder, não editar e não enviar mensagem a usuários não autorizados.
            return
    if len(text) > COMMAND_ARGUMENT_MAX_CHARS:
        await _reply_and_cleanup(update, f"❌ O comando excede o limite de {COMMAND_ARGUMENT_MAX_CHARS} caracteres.")
        return
    handler_name = DOT_COMMANDS.get(command)
    handler = globals().get(handler_name) if handler_name else None
    if handler is None:
        return
    original_args = getattr(context, "args", None)
    context.args = parts[1:]
    command_started = time.perf_counter()
    BLACKLIST_TELEMETRY["command_started"] += 1
    try:
        await handler(update, context)
        BLACKLIST_TELEMETRY["command_completed"] += 1
    except asyncio.CancelledError:
        raise
    except TelegramError as exc:
        BLACKLIST_TELEMETRY["command_failed"] += 1
        BLACKLIST_TELEMETRY["last_error"] = type(exc).__name__
        logger.warning("Falha do Telegram ao executar .%s: %s", command, exc)
        await _reply_and_cleanup(update, "❌ O Telegram recusou esta operação ou a conexão falhou. Tente novamente.")
    except Exception:
        BLACKLIST_TELEMETRY["command_failed"] += 1
        BLACKLIST_TELEMETRY["last_error"] = "command_internal_error"
        logger.exception("Falha inesperada ao executar .%s", command)
        await _reply_and_cleanup(update, "❌ Ocorreu um erro interno ao executar este comando.")
    finally:
        elapsed_ms = (time.perf_counter() - command_started) * 1000
        BLACKLIST_TELEMETRY["last_command_ms"] = elapsed_ms
        BLACKLIST_TELEMETRY["max_command_ms"] = max(BLACKLIST_TELEMETRY["max_command_ms"], elapsed_ms)
        context.args = original_args


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_command_access(update, context, "blacklist"):
        return
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    chat_id = int(update.effective_chat.id)
    if args and args[0].lower() == "list":
        if len(args) > 2:
            await _reply_and_cleanup(update, "❌ Uso: <code>.blacklist list</code> ou <code>.blacklist list N</code>.")
            return
        page = 1
        if len(args) == 2:
            page = _parse_list_page(args[1])
        if page is None or page < 1:
            await _reply_and_cleanup(update, "❌ O número da página deve ser um inteiro positivo.")
            return
        total, rows = await asyncio.gather(
            _db_call(db.count_blacklist_for_chat, chat_id),
            _db_call(db.get_blacklist_for_chat_page, chat_id, LIST_MAX_VISIBLE_ENTRIES, (page - 1) * LIST_MAX_VISIBLE_ENTRIES),
        )
        await _reply_and_cleanup(
            update,
            _format_user_list(
                rows,
                total=total,
                page=page,
                title="📋 <b>Blacklist local</b>",
                empty_text="ℹ️ Não há usuários na blacklist deste grupo.",
                scope_text="As mensagens dos usuários listados são apagadas automaticamente.",
                next_page_command=".blacklist list",
            ),
        )
        return

    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("blacklist"))
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "❌ O proprietário não pode ser colocado na blacklist.")
        return
    # Idempotência rápida: não faça RPC de permissões se o alvo já está registrado.
    if target.user_id in BLACKLIST_CACHE.get(chat_id, set()):
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está na blacklist deste grupo.")
        return
    if await _db_call(db.has_blacklist, target.user_id, chat_id):
        BLACKLIST_CACHE.setdefault(chat_id, set()).add(target.user_id)
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está na blacklist deste grupo.")
        return
    if not await _bot_can_delete(chat_id, context):
        await _reply_and_cleanup(update, "❌ O bot precisa ser administrador com permissão para apagar mensagens neste grupo.")
        return
    reason = _reason(context)
    if not await _db_call(db.add_blacklist, target, chat_id, update.effective_user.id, reason):
        await _reply_and_cleanup(update, "❌ Não foi possível persistir a blacklist.")
        return
    BLACKLIST_CACHE[chat_id].add(target.user_id)
    # Limpa as reações existentes sem apagar mensagens de terceiros; as novas
    # reações são removidas pelo MessageReactionHandler em tempo real.
    _track_task(_remove_all_user_reactions(context.bot, chat_id, target.user_id))
    await _reply_and_cleanup(
        update,
        f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) foi adicionado à blacklist local.\n"
        "Texto, mídia, stickers, GIFs, documentos, áudios, vozes e outras mensagens dele serão apagados enquanto o bot estiver ativo; reações também serão removidas quando a API as reportar.",
    )


async def cmd_banperm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_command_access(update, context, "jtperm"):
        return
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    chat_id = int(update.effective_chat.id)
    if args and args[0].lower() == "list":
        if len(args) > 2:
            await _reply_and_cleanup(update, "❌ Uso: <code>.jtperm list</code> ou <code>.jtperm list N</code>.")
            return
        page = 1
        if len(args) == 2:
            page = _parse_list_page(args[1])
        if page is None or page < 1:
            await _reply_and_cleanup(update, "❌ O número da página deve ser um inteiro positivo.")
            return
        total, rows = await asyncio.gather(
            _db_call(db.count_banperm_for_chat, chat_id),
            _db_call(db.get_banperm_for_chat_page, chat_id, LIST_MAX_VISIBLE_ENTRIES, (page - 1) * LIST_MAX_VISIBLE_ENTRIES),
        )
        await _reply_and_cleanup(
            update,
            _format_user_list(
                rows,
                total=total,
                page=page,
                title="🔨 <b>Banperm local</b>",
                empty_text="ℹ️ Não há usuários banidos permanentemente neste grupo.",
                scope_text="As reentradas são bloqueadas automaticamente quando detectadas.",
                next_page_command=".jtperm list",
            ),
        )
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("jtperm"))
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "❌ O proprietário é imune a banimentos.")
        return
    chat_id = update.effective_chat.id
    if target.user_id in BANPERM_CACHE.get(chat_id, set()):
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está banido permanentemente neste grupo.")
        return
    if await _db_call(db.has_banperm, target.user_id, chat_id):
        BANPERM_CACHE.setdefault(chat_id, set()).add(target.user_id)
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está banido permanentemente neste grupo.")
        return
    if not await _bot_can_restrict(chat_id, context):
        await _reply_and_cleanup(update, "❌ Conceda ao bot a permissão de restringir/banir membros.")
        return
    ban_result = await _ban_in_chat(context, chat_id, target)
    if ban_result != "ok":
        logger.warning("Falha ao aplicar banperm em %s/%s: %s", chat_id, target.user_id, ban_result)
        if ban_result == "forbidden":
            text = "❌ O bot não tem permissão para banir membros neste grupo."
        elif ban_result == "skipped":
            text = "❌ O Telegram não permitiu banir este alvo, possivelmente por ele ser administrador."
        else:
            text = "❌ Não foi possível aplicar o banimento permanente neste grupo."
        await _reply_and_cleanup(update, text)
        return
    reason = _reason(context)
    if not await _db_call(db.add_banperm, target, chat_id, update.effective_user.id, reason):
        logger.error("Banimento aplicado, mas não persistido em %s/%s", chat_id, target.user_id)
        BANPERM_CACHE[chat_id].add(target.user_id)
        await _reply_and_cleanup(update, "⚠️ Banimento aplicado, mas o registro local não pôde ser persistido.")
        return
    BANPERM_CACHE[chat_id].add(target.user_id)
    await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) banido permanentemente neste grupo.")


async def cmd_unbanperm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_command_access(update, context, "unjtperm"):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("unjtperm"))
        return
    chat_id = update.effective_chat.id
    was_cached = target.user_id in BANPERM_CACHE.get(chat_id, set())
    if not was_cached and not await _db_call(db.has_banperm, target.user_id, chat_id):
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava banido permanentemente neste grupo.")
        return
    unban_result = await _unban_in_chat(context, chat_id, target)
    if unban_result not in {"ok", "skipped"}:
        logger.warning("Falha ao retirar banperm em %s/%s: %s", chat_id, target.user_id, unban_result)
        if unban_result == "forbidden":
            text = "❌ O bot não tem permissão para retirar banimentos neste grupo."
        else:
            text = "❌ Não foi possível retirar o banimento neste grupo."
        await _reply_and_cleanup(update, text)
        return
    removed = await _db_call(db.remove_banperm, target.user_id, chat_id)
    BANPERM_CACHE.get(chat_id, set()).discard(target.user_id)
    if not removed:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já não estava registrado como banperm neste grupo.")
        return
    await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) desbanido neste grupo.")


async def _ban_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target):
    try:
        await _api_call(
            context.bot.ban_chat_member,
            chat_id,
            target.user_id,
            operation_name="banimento",
            retry_after_maximum=60.0,
        )
        return "ok"
    except Forbidden:
        return "forbidden"
    except BadRequest as exc:
        text = str(exc).lower()
        if "user is an administrator" in text or "chat member status" in text:
            return "skipped"
        return "failed"
    except TelegramError:
        return "failed"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Falha inesperada ao banir em %s/%s", chat_id, target.user_id)
        return "failed"


async def _unban_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target):
    try:
        await _api_call(
            context.bot.unban_chat_member,
            chat_id,
            target.user_id,
            only_if_banned=True,
            operation_name="desbanimento",
            retry_after_maximum=60.0,
        )
        return "ok"
    except BadRequest as exc:
        text = str(exc).lower()
        if "not banned" in text or "user is not banned" in text:
            return "skipped"
        return "failed"
    except Forbidden:
        return "forbidden"
    except TelegramError:
        return "failed"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Falha inesperada ao desbanir em %s/%s", chat_id, target.user_id)
        return "failed"


async def _enforce_jtbn_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target):
    key = (int(chat_id), int(target.user_id))
    if key in JTBN_BAN_INFLIGHT:
        return
    JTBN_BAN_INFLIGHT.add(key)
    try:
        result = await _ban_in_chat(context, key[0], target)
        if result not in {"ok", "skipped"}:
            logger.debug("JTBN não conseguiu reforçar o banimento em %s/%s: %s", key[0], key[1], result)
    finally:
        JTBN_BAN_INFLIGHT.discard(key)


async def _enforce_banperm_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target):
    """Reaplica um banperm local com deduplicação e retry pelo próximo evento."""
    chat_id = int(chat_id)
    user_id = int(target.user_id)
    if _is_owner(user_id):
        return
    key = (chat_id, user_id)
    now = time.monotonic()
    if len(BANPERM_REENTRY_LAST_ENFORCED) > 4096:
        cutoff = now - 120.0
        for old_key, timestamp in list(BANPERM_REENTRY_LAST_ENFORCED.items()):
            if timestamp < cutoff:
                BANPERM_REENTRY_LAST_ENFORCED.pop(old_key, None)
    last = BANPERM_REENTRY_LAST_ENFORCED.get(key, 0.0)
    if key in BANPERM_REENTRY_INFLIGHT or now - last < 2.0:
        return
    BANPERM_REENTRY_LAST_ENFORCED[key] = now
    BANPERM_REENTRY_INFLIGHT.add(key)
    BLACKLIST_TELEMETRY["banperm_reentry_attempted"] += 1
    try:
        result = await _ban_in_chat(context, chat_id, target)
        if result in {"ok", "skipped"}:
            BLACKLIST_TELEMETRY["banperm_reentry_success"] += 1
            _invalidate_chat_member_cache(chat_id)
            logger.info("Banperm reaplicado automaticamente em %s/%s: %s", chat_id, user_id, result)
        else:
            BLACKLIST_TELEMETRY["banperm_reentry_failed"] += 1
            logger.warning("Falha ao reaplicar banperm em %s/%s: %s", chat_id, user_id, result)
    finally:
        BANPERM_REENTRY_INFLIGHT.discard(key)


async def _enforce_banperm_reentry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reaplica banperm quando um usuário registrado reaparece no grupo.

    O evento `chat_member` é a fonte principal porque cobre entradas sem mensagem.
    O caminho de mensagens também chama o helper como fallback caso uma atualização
    de entrada não tenha chegado ao processo.
    """
    chat = update.effective_chat
    change = update.chat_member
    if not chat or not change or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    old_status = getattr(change.old_chat_member, "status", None)
    new_status = getattr(change.new_chat_member, "status", None)
    if new_status not in {"member", "restricted"} or old_status not in {"left", "kicked"}:
        return
    user = change.new_chat_member.user
    if not user or getattr(user, "is_bot", False) or _is_owner(getattr(user, "id", None)):
        return
    user_id = int(user.id)
    chat_id = int(chat.id)
    in_memory = user_id in BANPERM_CACHE.get(chat_id, set())
    persisted = False if in_memory else await _db_call(db.has_banperm, user_id, chat_id)
    if not in_memory and not persisted:
        return
    BANPERM_CACHE.setdefault(chat_id, set()).add(user_id)
    await _enforce_banperm_in_chat(context, chat_id, _remember_user_in_memory(user))


async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa alterações de membros comuns, especialmente reentradas banperm."""
    await _enforce_banperm_reentry(update, context)


async def cmd_unjtbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_owner_access(update, context):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("unjtbn"))
        return
    if target.user_id not in JTBN_CACHE:
        # O cache é apenas um acelerador; em caso de processo reiniciado ou
        # divergência transitória, confirma no SQLite antes de responder.
        if not await _db_call(db.has_allban, target.user_id):
            await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava no JTBN global.")
            return
        JTBN_CACHE.add(target.user_id)
    if not await _db_call(db.remove_allban, target.user_id):
        await _reply_and_cleanup(update, "❌ Não foi possível remover o JTBN global do banco.")
        return
    JTBN_CACHE.discard(target.user_id)
    rows = await _db_call(db.active_chats)
    if not rows:
        await _reply_and_cleanup(update, f"✅ JTBN removido para <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>).")
        return
    semaphore = asyncio.Semaphore(JTBN_CONCURRENCY)

    async def apply(row):
        async with semaphore:
            return await _unban_in_chat(context, int(row["chat_id"]), target)

    results = await asyncio.gather(*(apply(row) for row in rows))
    counts = {key: results.count(key) for key in {"ok", "failed", "forbidden", "skipped"}}
    await _reply_and_cleanup(
        update,
        f"✅ <b>JTBN removido</b> para {_safe_html(target.label)} (<code>{target.user_id}</code>).\n\n"
        f"✅ Desbanidos: <b>{counts['ok']}</b>\n"
        f"ℹ️ Já livres: <b>{counts['skipped']}</b>\n"
        f"🔒 Sem permissão: <b>{counts['forbidden']}</b>\n"
        f"❌ Falhas: <b>{counts['failed']}</b>\n"
        f"📊 Grupos processados: <b>{len(rows)}</b>",
    )


async def cmd_jtbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_owner_access(update, context):
        return
    args = [str(arg).strip() for arg in (context.args or []) if str(arg).strip()]
    if args and args[0].lower() == "list":
        if len(args) > 2:
            await _reply_and_cleanup(update, "❌ Uso: <code>.jtbn list</code> ou <code>.jtbn list N</code>.")
            return
        page = 1
        if len(args) == 2:
            page = _parse_list_page(args[1])
        if page is None or page < 1:
            await _reply_and_cleanup(update, "❌ O número da página deve ser um inteiro positivo.")
            return
        total, rows = await asyncio.gather(
            _db_call(db.count_allban),
            _db_call(db.get_allban_entries_page, LIST_MAX_VISIBLE_ENTRIES, (page - 1) * LIST_MAX_VISIBLE_ENTRIES),
        )
        await _reply_and_cleanup(
            update,
            _format_user_list(
                rows,
                total=total,
                page=page,
                title="🌐 <b>JTBN global</b>",
                empty_text="ℹ️ Não há usuários no JTBN global.",
                scope_text="O JTBN é aplicado aos grupos ativos registrados pelo Bot API.",
                next_page_command=".jtbn list",
            ),
        )
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("jtbn"))
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "❌ O proprietário não pode ser banido.")
        return
    if target.user_id in JTBN_CACHE:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está no JTBN global.")
        return
    # Autocorreção barata para evitar duplicidade quando o cache foi perdido,
    # sem adicionar consulta em mensagens normais ou no fast path.
    if await _db_call(db.has_allban, target.user_id):
        JTBN_CACHE.add(target.user_id)
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está no JTBN global.")
        return
    reason = _reason(context)
    if not await _db_call(db.add_allban, target, update.effective_user.id, reason):
        await _reply_and_cleanup(update, "❌ Não foi possível registrar o JTBN global.")
        return
    JTBN_CACHE.add(target.user_id)

    rows = await _db_call(db.active_chats)
    if not rows:
        await _reply_and_cleanup(update, "✅ JTBN registrado. Nenhum grupo ativo está registrado para receber a ação agora.")
        return

    semaphore = asyncio.Semaphore(JTBN_CONCURRENCY)

    async def apply(row):
        async with semaphore:
            result = await _ban_in_chat(context, int(row["chat_id"]), target)
            return result

    results = await asyncio.gather(*(apply(row) for row in rows))
    counts = {key: results.count(key) for key in {"ok", "failed", "forbidden", "skipped"}}
    await _reply_and_cleanup(
        update,
        f"✅ <b>JTBN registrado</b> para {_safe_html(target.label)} (<code>{target.user_id}</code>).\n\n"
        f"✅ Banidos: <b>{counts['ok']}</b>\n"
        f"⚠️ Ignorados: <b>{counts['skipped']}</b>\n"
        f"🔒 Sem permissão: <b>{counts['forbidden']}</b>\n"
        f"❌ Falhas: <b>{counts['failed']}</b>\n"
        f"📊 Grupos processados: <b>{len(rows)}</b>",
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    change = update.my_chat_member
    if not chat or not change:
        return
    status = change.new_chat_member.status
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    _invalidate_chat_member_cache(chat.id)
    active = status not in {"left", "kicked"}
    if active:
        KNOWN_CHAT_IDS.add(chat.id)
        _chat_registration_seen.add(chat.id)
    else:
        KNOWN_CHAT_IDS.discard(chat.id)
        _chat_registration_seen.discard(chat.id)
        await _cancel_all_divulgar_tasks_for_chat(chat.id)
        await _cancel_spam_task(chat.id)
        schedule_ids = [
            schedule_id
            for schedule_id, config in list(DIVULGAR_CONFIGS.items())
            if int(config.get("chat_id", 0)) == int(chat.id)
        ]
        for schedule_id in schedule_ids:
            DIVULGAR_CONFIGS.pop(schedule_id, None)
            DIVULGAR_LAST_FAILURE_NOTIFY.pop(schedule_id, None)
        await _db_call(db.remove_all_divulgacoes_for_chat, chat.id)
    await _db_call(db.register_chat, chat.id, chat.title or "", chat.type, active)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or user.is_bot:
        return

    # Fast path: decidir apenas com estruturas em memória antes de qualquer SQLite/RPC auxiliar.
    user_id = int(user.id)
    if user_id in JTBN_CACHE and not _is_owner(user_id):
        target = _remember_user_in_memory(user)
        BLACKLIST_TELEMETRY["matched"] += 1
        if getattr(message, "date", None) is not None:
            BLACKLIST_TELEMETRY["last_update_age_ms"] = max(0.0, (time.time() - message.date.timestamp()) * 1000)
        _schedule_delete_now(context.bot, message)
        _track_task(_enforce_jtbn_in_chat(context, chat.id, target))
        return
    if not _is_owner(user_id) and (
        user_id in BANPERM_CACHE.get(chat.id, set())
        or user_id in BLACKLIST_CACHE.get(chat.id, set())
    ):
        BLACKLIST_TELEMETRY["matched"] += 1
        if getattr(message, "date", None) is not None:
            BLACKLIST_TELEMETRY["last_update_age_ms"] = max(0.0, (time.time() - message.date.timestamp()) * 1000)
        _schedule_delete_now(context.bot, message)
        if user_id in BANPERM_CACHE.get(chat.id, set()):
            # Fallback para quando a atualização `chat_member` não chegou ao processo.
            _track_task(_enforce_banperm_in_chat(context, chat.id, _remember_user_in_memory(user)))
        return

    # Não desperdiçar SQLite com comandos pontuados já tratados pelo dispatcher.
    # A verificação ocorre depois do fast path para nunca deixar de apagar uma
    # mensagem de usuário que esteja em JTBN, banperm ou blacklist.
    if (getattr(message, "text", "") or "").lstrip().startswith("."):
        return
    # Mensagens normais podem atualizar contexto e persistência sem atrasar a exclusão do fast path.
    await _remember_message_context(update)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error is None:
        return
    if isinstance(error, RetryAfter):
        BLACKLIST_TELEMETRY["polling_errors"] += 1
        BLACKLIST_TELEMETRY["last_polling_error"] = "RetryAfter"
        logger.warning("Telegram solicitou RetryAfter: %s", error)
    elif isinstance(error, (NetworkError, TimedOut)):
        BLACKLIST_TELEMETRY["polling_errors"] += 1
        BLACKLIST_TELEMETRY["last_polling_error"] = type(error).__name__
        logger.warning("Falha transitória de rede no Bot API; o polling continuará tentando: %s", error)
    else:
        BLACKLIST_TELEMETRY["last_polling_error"] = type(error).__name__
        logger.error(
            "Erro não tratado no Bot API",
            exc_info=(type(error), error, getattr(error, "__traceback__", None)),
        )


async def post_init(app: Application):
    global BOT_USER_ID, HEARTBEAT_TASK
    _write_heartbeat()
    HEARTBEAT_TASK = _track_task(_heartbeat_worker())
    BOT_USER_ID = (await _api_call(app.bot.get_me, operation_name="identificação do bot")).id
    rows = await _db_call(db.get_divulgacoes)
    for row in rows:
        try:
            schedule_id = int(row["schedule_id"])
            chat_id = int(row["chat_id"])
            interval_seconds = int(row["interval_seconds"])
            source_message_id = int(row["source_message_id"])
            owner_id = int(row["owner_id"])
            next_run_at = float(row["next_run_at"] or 0)
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            logger.warning("Divulgação inválida ignorada durante o startup: linha=%r", tuple(row), exc_info=True)
            continue
        if chat_id == 0 or source_message_id <= 0 or owner_id <= 0 or not math.isfinite(next_run_at):
            logger.warning("Divulgação com identificadores/timestamp inválidos ignorada: chat_id=%s schedule_id=%s", chat_id, schedule_id)
            continue
        config = {
            "schedule_id": schedule_id,
            "chat_id": chat_id,
            "interval_seconds": interval_seconds,
            "content_type": str(row["content_type"] or ""),
            "text": str(row["text"] or ""),
            "file_id": str(row["file_id"] or ""),
            "source_message_id": source_message_id,
            "owner_id": owner_id,
            "next_run_at": next_run_at,
        }
        if config["content_type"] not in {"text", "photo", "video"}:
            logger.warning("Divulgação inválida ignorada no chat_id=%s schedule_id=%s", chat_id, schedule_id)
            continue
        if not DIVULGAR_MIN_INTERVAL_SECONDS <= interval_seconds <= DIVULGAR_MAX_INTERVAL_SECONDS:
            logger.warning("Intervalo de divulgação inválido ignorado no chat_id=%s schedule_id=%s", chat_id, schedule_id)
            continue
        if config["content_type"] != "text" and not config["file_id"]:
            logger.warning("Divulgação de mídia sem file_id ignorada no chat_id=%s schedule_id=%s", chat_id, schedule_id)
            continue
        if config["content_type"] == "text" and not config["text"]:
            logger.warning("Divulgação de texto vazia ignorada no chat_id=%s schedule_id=%s", chat_id, schedule_id)
            continue
        DIVULGAR_CONFIGS[schedule_id] = config
        await _ensure_divulgar_task(app.bot, schedule_id, config)
    logger.info(
        "Jtzin Bot API online; proprietários=%s; divulgações restauradas=%s",
        ",".join(str(owner_id) for owner_id in sorted(OWNER_IDS)),
        len(DIVULGAR_CONFIGS),
    )


async def post_shutdown(app: Application):
    global HEARTBEAT_TASK
    for task in set(DIVULGAR_TASKS.values()):
        task.cancel()
    for task in set(SPAM_TASKS.values()):
        task.cancel()
    for task in list(_cleanup_tasks):
        task.cancel()
    if _cleanup_tasks:
        await asyncio.gather(*_cleanup_tasks, return_exceptions=True)
    DIVULGAR_TASKS.clear()
    DIVULGAR_CONFIGS.clear()
    SPAM_TASKS.clear()
    SPAM_CONFIGS.clear()
    for task in set(CHAT_MEMBER_INFLIGHT.values()):
        task.cancel()
    if CHAT_MEMBER_INFLIGHT:
        await asyncio.gather(*CHAT_MEMBER_INFLIGHT.values(), return_exceptions=True)
        CHAT_MEMBER_INFLIGHT.clear()
    CHAT_MEMBER_CACHE.clear()
    JTBN_BAN_INFLIGHT.clear()
    BANPERM_REENTRY_INFLIGHT.clear()
    BANPERM_REENTRY_LAST_ENFORCED.clear()
    if HEARTBEAT_TASK is not None:
        HEARTBEAT_TASK.cancel()
        await asyncio.gather(HEARTBEAT_TASK, return_exceptions=True)
        HEARTBEAT_TASK = None
    try:
        HEARTBEAT_PATH.unlink(missing_ok=True)
    except OSError:
        logger.debug("Não foi possível remover o heartbeat do Bot API", exc_info=True)
    await asyncio.to_thread(db.close)


def main():
    # O polling usa uma conexão exclusiva; as ações de moderação mantêm um pool
    # separado para que uma longa espera de getUpdates nunca ocupe a conexão de delete.
    api_request = HTTPXRequest(
        connection_pool_size=API_CONNECTION_POOL_SIZE,
        connect_timeout=3.0,
        read_timeout=10.0,
        write_timeout=10.0,
        pool_timeout=1.0,
    )
    polling_request = HTTPXRequest(
        connection_pool_size=1,
        connect_timeout=3.0,
        read_timeout=GET_UPDATES_READ_TIMEOUT_SECONDS,
        write_timeout=10.0,
        pool_timeout=2.0,
    )
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(api_request)
        .get_updates_request(polling_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(8)
        .build()
    )
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageReactionHandler(
        on_message_reaction_update,
        message_reaction_types=MessageReactionHandler.MESSAGE_REACTION_UPDATED,
        block=False,
    ), group=1)
    app.add_handler(MessageHandler(filters.Regex(DOT_COMMAND_RE), on_dot_command))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_message), group=1)
    app.add_error_handler(error_handler)
    logger.info(
        "Iniciando polling do Jtzin Bot API (bootstrap_retries=%s, timeout=%ss)",
        POLLING_BOOTSTRAP_RETRIES,
        POLLING_TIMEOUT_SECONDS,
    )
    app.run_polling(
        timeout=POLLING_TIMEOUT_SECONDS,
        bootstrap_retries=POLLING_BOOTSTRAP_RETRIES,
        allowed_updates=ALLOWED_UPDATES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
