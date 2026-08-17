from __future__ import annotations

import asyncio
import logging
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
from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    ContextTypes,
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


def _is_owner(user_id: int | None) -> bool:
    try:
        return int(user_id or 0) in OWNER_IDS
    except (TypeError, ValueError):
        return False

DB_PATH = DATA_DIR / "bot_api.db"
DELETE_AFTER_SECONDS = 5
ALLBAN_CONCURRENCY = 4
MAX_TARGET_ID_LENGTH = 20
# O lote aguarda apenas alguns milissegundos para capturar uma rajada sem atrasar
# uma mensagem isolada. O Telegram aceita de 1 a 100 IDs no deleteMessages.
DELETE_BATCH_WINDOW_SECONDS = 0.008
DELETE_BATCH_MAX_MESSAGES = 100
API_CONNECTION_POOL_SIZE = 32
GET_UPDATES_READ_TIMEOUT_SECONDS = 35.0

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
                CREATE INDEX IF NOT EXISTS idx_chats_active ON chats(active);
                CREATE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_blacklist_chat ON blacklist(chat_id);
                CREATE INDEX IF NOT EXISTS idx_banperm_chat ON banperm(chat_id);
                """
            )
            self.conn.commit()

    def _execute(self, sql, params=(), *, commit=False):
        with self._lock:
            cursor = self.conn.execute(sql, params)
            if commit:
                self.conn.commit()
            return cursor

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
        row = self._execute(
            "SELECT user_id,username,full_name FROM users WHERE username = ? COLLATE NOCASE LIMIT 1",
            (username.lstrip("@"),),
        ).fetchone()
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

    def remove_allban(self, user_id: int) -> bool:
        cursor = self._execute(
            "DELETE FROM allban WHERE user_id=?",
            (int(user_id),),
            commit=True,
        )
        return cursor.rowcount > 0

    def active_chats(self):
        return self._execute(
            "SELECT chat_id,title,chat_type FROM chats WHERE active=1 AND chat_type IN ('group','supergroup') ORDER BY chat_id"
        ).fetchall()

    def close(self):
        with self._lock:
            self.conn.close()


db = Database(DB_PATH)
try:
    KNOWN_CHAT_IDS, KNOWN_USERS, BLACKLIST_CACHE, BANPERM_CACHE, ALLBAN_CACHE = db.load_state()
except sqlite3.Error:
    logger.exception("Falha ao carregar o estado do banco do Bot API")
    raise

BOT_USER_ID = 0
_cleanup_tasks: set[asyncio.Task] = set()
_delete_batch_pending: dict[int, dict[int, float]] = defaultdict(dict)
_delete_batch_tasks: dict[int, asyncio.Task] = {}
_chat_registration_seen = set(KNOWN_CHAT_IDS)
KNOWN_USERNAME_IDS = {
    username.lower(): int(user_id)
    for user_id, (username, _full_name) in KNOWN_USERS.items()
    if username
}
CHAT_MEMBER_CACHE_TTL = 5.0
CHAT_MEMBER_ERROR_TTL = 1.5
CHAT_MEMBER_CACHE: dict[tuple[int, int], tuple[float, object | None]] = {}
ALLOWED_UPDATES = ["message", "my_chat_member"]
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
    return "—" if value is None else f"{float(value):.0f} ms"


def _track_task(coro):
    task = asyncio.create_task(coro)
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    return task


async def _delete_one(bot, chat_id: int, message_id: int, scheduled_at: float | None = None):
    started = time.perf_counter()
    if scheduled_at is not None:
        BLACKLIST_TELEMETRY["last_queue_ms"] = max(0.0, (started - scheduled_at) * 1000)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except RetryAfter as exc:
        # Respeita o flood control do Telegram e faz uma única repetição segura.
        try:
            await asyncio.sleep(min(max(float(exc.retry_after), 0.0), 60.0))
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except (BadRequest, Forbidden, RetryAfter, TelegramError):
            BLACKLIST_TELEMETRY["delete_failed"] += 1
            return False
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
                batch_ok = bool(await delete_messages(chat_id=chat_id, message_ids=message_ids))
            except RetryAfter as exc:
                try:
                    await asyncio.sleep(min(max(float(exc.retry_after), 0.0), 60.0))
                    batch_ok = bool(await delete_messages(chat_id=chat_id, message_ids=message_ids))
                except (BadRequest, Forbidden, RetryAfter, TelegramError):
                    pass
            except (BadRequest, Forbidden, TelegramError):
                pass
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


def _schedule_delete(message, delay: int = DELETE_AFTER_SECONDS):
    if message is None:
        return

    async def worker():
        try:
            await asyncio.sleep(delay)
            await message.delete()
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
        response = await message.reply_text(text, **kwargs)
    except TelegramError:
        return None
    _schedule_delete(message)
    _schedule_delete(response)
    return response


def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in {ChatType.GROUP, ChatType.SUPERGROUP})


async def _get_chat_member_cached(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    cached = CHAT_MEMBER_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        CHAT_MEMBER_CACHE[key] = (now + CHAT_MEMBER_ERROR_TTL, None)
        return None
    CHAT_MEMBER_CACHE[key] = (now + CHAT_MEMBER_CACHE_TTL, member)
    if len(CHAT_MEMBER_CACHE) > 4096:
        expired = [cache_key for cache_key, (expires, _member) in CHAT_MEMBER_CACHE.items() if expires <= now]
        for cache_key in expired[:1024]:
            CHAT_MEMBER_CACHE.pop(cache_key, None)
    return member


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


async def _require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_group(update):
        await _reply_and_cleanup(update, "❌ Este comando só pode ser usado em grupos ou supergrupos.")
        return False
    await _remember_message_context(update)
    if update.effective_user and _is_owner(update.effective_user.id):
        return True
    if await _is_chat_admin(update, context):
        return True
    await _reply_and_cleanup(update, "⛔ Apenas administradores do grupo ou os proprietários configurados podem usar este comando.")
    return False


async def _bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    global BOT_USER_ID
    if not BOT_USER_ID:
        try:
            BOT_USER_ID = (await context.bot.get_me()).id
        except TelegramError:
            return False
    member = await _get_chat_member_cached(chat_id, BOT_USER_ID, context)
    return bool(member and (member.status == "creator" or getattr(member, "can_restrict_members", False)))


async def _bot_can_delete(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    global BOT_USER_ID
    if not BOT_USER_ID:
        try:
            BOT_USER_ID = (await context.bot.get_me()).id
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
            await asyncio.to_thread(db.remember_user, target.user_id, target.username, target.full_name)
        return target

    args = list(context.args or [])
    if not args:
        return None
    raw = args[0].strip()
    if re.fullmatch(r"\d{4,20}", raw):
        user_id = int(raw)
        username, full_name = KNOWN_USERS.get(user_id, ("", ""))
        return Target(user_id, username, full_name)
    if raw.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", raw):
        username_key = raw[1:].lower()
        uid = KNOWN_USERNAME_IDS.get(username_key)
        if uid is not None:
            username, full_name = KNOWN_USERS[uid]
            return Target(uid, username, full_name)
        row = await asyncio.to_thread(db.resolve_username, raw[1:])
        if row:
            keys = row.keys()
            return Target(
                int(row["user_id"]),
                row["username"] if "username" in keys else "",
                row["full_name"] if "full_name" in keys else "",
            )
    return None


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
        await asyncio.to_thread(db.register_chat, chat.id, chat.title or "", chat.type)
    if not user.is_bot:
        user_id = int(user.id)
        previous = KNOWN_USERS.get(user_id)
        target = _remember_user_in_memory(user)
        if previous != (target.username, target.full_name):
            await asyncio.to_thread(db.remember_user, target.user_id, target.username, target.full_name)


async def _safe_delete(message) -> bool:
    try:
        await message.delete()
        return True
    except (BadRequest, Forbidden, TelegramError):
        return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_and_cleanup(
        update,
        "🛡️ <b>Jtzin Administrator Bot</b>\n\n"
        "Bot API dedicado a blacklist local, banimento permanente local e allban global.\n"
        "Use .help para consultar a forma de uso.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_and_cleanup(
        update,
        "🛡️ <b>Jtzin Administrator Bot</b>\n\n"
        "<b>Comandos disponíveis</b>\n"
        "<code>.blacklist</code> — adiciona o alvo à blacklist deste grupo.\n"
        "<code>.unblacklist</code> — remove a blacklist local.\n"
        "<code>.banperm</code> — bane permanentemente o alvo deste grupo.\n"
        "<code>.unbanperm</code> — remove o banimento deste grupo.\n"
        "<code>.allban</code> — o proprietário bane o alvo nos grupos registrados.\n"
        "<code>.unallban</code> — remove o allban global e tenta desbanir o alvo.\n"
        "<code>.latency</code> — mede uma chamada real à API do Telegram.\n\n"
        "Use respondendo à mensagem do alvo, informe o ID ou use um @username já registrado pelo bot. Os proprietários configurados podem usar "
        "a moderação local mesmo sem serem administradores do grupo; o bot ainda precisa ser administrador "
        "com permissão para apagar mensagens e restringir membros. Os comandos allban são exclusivos dos proprietários.",
    )


async def _require_operator(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and _is_owner(user.id):
        return True
    if _is_group(update) and await _is_chat_admin(update, context):
        return True
    await _reply_and_cleanup(update, "⛔ Este diagnóstico está disponível aos administradores do grupo e aos proprietários configurados.")
    return False


async def cmd_latency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_operator(update, context):
        return
    started = time.perf_counter()
    try:
        await context.bot.get_me()
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
        "• Polling: ✅ processo monitorado pelo watchdog\n"
        "• Userbot: ⏸️ desligado\n"
        "\nA medição separa atraso do update, fila local e tempo do RPC de exclusão."

    )


async def cmd_unblacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group_admin(update, context):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("unblacklist"))
        return
    chat_id = update.effective_chat.id
    was_cached = target.user_id in BLACKLIST_CACHE.get(chat_id, set())
    removed = await asyncio.to_thread(db.remove_blacklist, target.user_id, chat_id)
    BLACKLIST_CACHE.get(chat_id, set()).discard(target.user_id)
    if not removed and not was_cached:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava na blacklist deste grupo.")
        return
    await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) removido da blacklist local.")


DOT_COMMAND_RE = re.compile(r"^\.(unblacklist|unbanperm|unallban|blacklist|banperm|allban|latency)(?:\s+.*)?$", re.IGNORECASE)
DOT_COMMANDS = {
    "blacklist": "cmd_blacklist",
    "unblacklist": "cmd_unblacklist",
    "banperm": "cmd_banperm",
    "unbanperm": "cmd_unbanperm",
    "allban": "cmd_allban",
    "unallban": "cmd_unallban",
    "latency": "cmd_latency",
}


async def on_dot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    text = (message.text or "").strip() if message else ""
    match = DOT_COMMAND_RE.fullmatch(text)
    if not match:
        return
    parts = text.split()
    command = parts[0][1:].lower()
    handler_name = DOT_COMMANDS.get(command)
    handler = globals().get(handler_name) if handler_name else None
    if handler is None:
        return
    original_args = getattr(context, "args", None)
    context.args = parts[1:]
    try:
        await handler(update, context)
    finally:
        context.args = original_args


async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group_admin(update, context):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("blacklist"))
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "❌ O proprietário não pode ser colocado na blacklist.")
        return
    chat_id = update.effective_chat.id
    if not await _bot_can_delete(chat_id, context):
        await _reply_and_cleanup(update, "❌ O bot precisa ser administrador com permissão para apagar mensagens neste grupo.")
        return
    if target.user_id in BLACKLIST_CACHE[chat_id]:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está na blacklist deste grupo.")
        return
    reason = _reason(context)
    if not await asyncio.to_thread(db.add_blacklist, target, chat_id, update.effective_user.id, reason):
        await _reply_and_cleanup(update, "❌ Não foi possível persistir a blacklist.")
        return
    BLACKLIST_CACHE[chat_id].add(target.user_id)
    await _reply_and_cleanup(
        update,
        f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) foi adicionado à blacklist local.\n"
        "As próximas mensagens dele serão apagadas automaticamente.",
    )


async def cmd_banperm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group_admin(update, context):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("banperm"))
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "❌ O proprietário é imune a banimentos.")
        return
    chat_id = update.effective_chat.id
    if target.user_id in BANPERM_CACHE[chat_id]:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> já está banido permanentemente neste grupo.")
        return
    if not await _bot_can_restrict(chat_id, context):
        await _reply_and_cleanup(update, "❌ Conceda ao bot a permissão de restringir/banir membros.")
        return
    try:
        await context.bot.ban_chat_member(chat_id, target.user_id)
    except (Forbidden, BadRequest, RetryAfter, TelegramError):
        logger.exception("Falha ao aplicar banperm em %s/%s", chat_id, target.user_id)
        await _reply_and_cleanup(update, "❌ Não foi possível aplicar o banimento permanente neste grupo.")
        return
    reason = _reason(context)
    if not await asyncio.to_thread(db.add_banperm, target, chat_id, update.effective_user.id, reason):
        logger.error("Banimento aplicado, mas não persistido em %s/%s", chat_id, target.user_id)
        BANPERM_CACHE[chat_id].add(target.user_id)
        await _reply_and_cleanup(update, "⚠️ Banimento aplicado, mas o registro local não pôde ser persistido.")
        return
    BANPERM_CACHE[chat_id].add(target.user_id)
    await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) banido permanentemente neste grupo.")


async def cmd_unbanperm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group_admin(update, context):
        return
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("unbanperm"))
        return
    chat_id = update.effective_chat.id
    was_cached = target.user_id in BANPERM_CACHE.get(chat_id, set())
    if not was_cached:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava banido permanentemente neste grupo.")
        return
    try:
        await context.bot.unban_chat_member(chat_id, target.user_id, only_if_banned=True)
    except BadRequest as exc:
        text = str(exc).lower()
        if "not banned" not in text and "user is not banned" not in text:
            logger.warning("Falha ao retirar banperm em %s/%s: %s", chat_id, target.user_id, exc)
            await _reply_and_cleanup(update, "❌ Não foi possível retirar o banimento neste grupo.")
            return
    except TelegramError:
        logger.exception("Falha ao retirar banperm em %s/%s", chat_id, target.user_id)
        await _reply_and_cleanup(update, "❌ Não foi possível retirar o banimento neste grupo.")
        return
    await asyncio.to_thread(db.remove_banperm, target.user_id, chat_id)
    BANPERM_CACHE.get(chat_id, set()).discard(target.user_id)
    await _reply_and_cleanup(update, f"✅ <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>) desbanido neste grupo.")


async def _ban_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target):
    try:
        await context.bot.ban_chat_member(chat_id, target.user_id)
        return "ok"
    except RetryAfter as exc:
        try:
            await asyncio.sleep(float(exc.retry_after))
            await context.bot.ban_chat_member(chat_id, target.user_id)
            return "ok"
        except TelegramError:
            return "failed"
    except Forbidden:
        return "forbidden"
    except BadRequest as exc:
        text = str(exc).lower()
        if "user is an administrator" in text or "chat member status" in text:
            return "skipped"
        return "failed"
    except TelegramError:
        return "failed"


async def _unban_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target: Target):
    try:
        await context.bot.unban_chat_member(chat_id, target.user_id, only_if_banned=True)
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


async def cmd_unallban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_owner(update.effective_user.id):
        await _reply_and_cleanup(update, "⛔ Este comando é exclusivo dos proprietários configurados.")
        return
    if _is_group(update):
        await _remember_message_context(update)
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("unallban"))
        return
    if target.user_id not in ALLBAN_CACHE:
        await _reply_and_cleanup(update, f"ℹ️ <b>{_safe_html(target.label)}</b> não estava no allban global.")
        return
    if not await asyncio.to_thread(db.remove_allban, target.user_id):
        await _reply_and_cleanup(update, "❌ Não foi possível remover o allban global do banco.")
        return
    ALLBAN_CACHE.discard(target.user_id)
    rows = await asyncio.to_thread(db.active_chats)
    if not rows:
        await _reply_and_cleanup(update, f"✅ Allban removido para <b>{_safe_html(target.label)}</b> (<code>{target.user_id}</code>).")
        return
    semaphore = asyncio.Semaphore(ALLBAN_CONCURRENCY)

    async def apply(row):
        async with semaphore:
            return await _unban_in_chat(context, int(row["chat_id"]), target)

    results = await asyncio.gather(*(apply(row) for row in rows))
    counts = {key: results.count(key) for key in {"ok", "failed", "forbidden", "skipped"}}
    await _reply_and_cleanup(
        update,
        f"✅ <b>Allban removido</b> para {_safe_html(target.label)} (<code>{target.user_id}</code>).\n\n"
        f"✅ Desbanidos: <b>{counts['ok']}</b>\n"
        f"ℹ️ Já livres: <b>{counts['skipped']}</b>\n"
        f"🔒 Sem permissão: <b>{counts['forbidden']}</b>\n"
        f"❌ Falhas: <b>{counts['failed']}</b>\n"
        f"📊 Grupos processados: <b>{len(rows)}</b>",
    )


async def cmd_allban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_owner(update.effective_user.id):
        await _reply_and_cleanup(update, "⛔ Este comando é exclusivo do proprietário configurado.")
        return
    if _is_group(update):
        await _remember_message_context(update)
    target = await _resolve_target(update, context)
    if target is None:
        await _reply_and_cleanup(update, _target_error("allban"))
        return
    if _is_owner(target.user_id):
        await _reply_and_cleanup(update, "❌ O proprietário não pode ser banido.")
        return
    reason = _reason(context)
    if not await asyncio.to_thread(db.add_allban, target, update.effective_user.id, reason):
        await _reply_and_cleanup(update, "❌ Não foi possível registrar o allban global.")
        return
    ALLBAN_CACHE.add(target.user_id)

    rows = await asyncio.to_thread(db.active_chats)
    if not rows:
        await _reply_and_cleanup(update, "✅ Allban registrado. Nenhum grupo ativo está registrado para receber a ação agora.")
        return

    semaphore = asyncio.Semaphore(ALLBAN_CONCURRENCY)

    async def apply(row):
        async with semaphore:
            result = await _ban_in_chat(context, int(row["chat_id"]), target)
            return result

    results = await asyncio.gather(*(apply(row) for row in rows))
    counts = {key: results.count(key) for key in {"ok", "failed", "forbidden", "skipped"}}
    await _reply_and_cleanup(
        update,
        f"✅ <b>Allban registrado</b> para {_safe_html(target.label)} (<code>{target.user_id}</code>).\n\n"
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
    await asyncio.to_thread(db.register_chat, chat.id, chat.title or "", chat.type, active)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or user.is_bot:
        return

    # Fast path: decidir apenas com estruturas em memória antes de qualquer SQLite/RPC auxiliar.
    user_id = int(user.id)
    if user_id in ALLBAN_CACHE and not _is_owner(user_id):
        target = _remember_user_in_memory(user)
        BLACKLIST_TELEMETRY["matched"] += 1
        if getattr(message, "date", None) is not None:
            BLACKLIST_TELEMETRY["last_update_age_ms"] = max(0.0, (time.time() - message.date.timestamp()) * 1000)
        _schedule_delete_now(context.bot, message)
        _track_task(_ban_in_chat(context, chat.id, target))
        return
    if user_id in BANPERM_CACHE.get(chat.id, set()) or user_id in BLACKLIST_CACHE.get(chat.id, set()):
        BLACKLIST_TELEMETRY["matched"] += 1
        if getattr(message, "date", None) is not None:
            BLACKLIST_TELEMETRY["last_update_age_ms"] = max(0.0, (time.time() - message.date.timestamp()) * 1000)
        _schedule_delete_now(context.bot, message)
        return

    # Mensagens normais podem atualizar contexto e persistência sem atrasar a exclusão do fast path.
    await _remember_message_context(update)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, RetryAfter):
        logger.warning("Telegram solicitou RetryAfter: %s", context.error)
    else:
        logger.exception("Erro não tratado no Bot API", exc_info=context.error)


async def post_init(app: Application):
    global BOT_USER_ID
    BOT_USER_ID = (await app.bot.get_me()).id
    logger.info("Jtzin Bot API online; proprietários=%s", ",".join(str(owner_id) for owner_id in sorted(OWNER_IDS)))


async def post_shutdown(app: Application):
    for task in list(_cleanup_tasks):
        task.cancel()
    if _cleanup_tasks:
        await asyncio.gather(*_cleanup_tasks, return_exceptions=True)
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
    app.add_handler(MessageHandler(filters.Regex(DOT_COMMAND_RE), on_dot_command))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_message), group=1)
    app.add_error_handler(error_handler)
    logger.info("Iniciando polling do Jtzin Bot API")
    app.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
