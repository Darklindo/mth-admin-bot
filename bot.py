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
from telegram import BotCommand, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
            "SELECT user_id,username,full_name FROM users WHERE lower(username)=lower(?) LIMIT 1",
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
_chat_registration_seen = set(KNOWN_CHAT_IDS)


def _remember_user_in_memory(user) -> Target:
    username = (getattr(user, "username", "") or "").lstrip("@")
    full_name = getattr(user, "full_name", "") or ""
    user_id = int(user.id)
    KNOWN_USERS[user_id] = (username, full_name)
    return Target(user_id, username, full_name)


def _safe_html(text: str) -> str:
    return escape(str(text or ""))


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

    task = asyncio.create_task(worker())
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


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


async def _is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {"administrator", "creator"}
    except TelegramError:
        return False


async def _require_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not _is_group(update):
        await _reply_and_cleanup(update, "❌ Este comando só pode ser usado em grupos ou supergrupos.")
        return False
    await _remember_message_context(update)
    if await _is_chat_admin(update, context):
        return True
    await _reply_and_cleanup(update, "⛔ Apenas administradores do grupo podem usar este comando.")
    return False


async def _bot_can_restrict(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    global BOT_USER_ID
    try:
        if not BOT_USER_ID:
            BOT_USER_ID = (await context.bot.get_me()).id
        member = await context.bot.get_chat_member(chat_id, BOT_USER_ID)
        return member.status == "creator" or bool(getattr(member, "can_restrict_members", False))
    except TelegramError:
        return False


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Target | None:
    message = update.effective_message
    if message is None:
        return None
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        target = _remember_user_in_memory(reply.from_user)
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
        uid = next(
            (candidate_id for candidate_id, value in KNOWN_USERS.items() if value[0].lower() == username_key),
            None,
        )
        if uid is not None:
            username, full_name = KNOWN_USERS[uid]
            return Target(uid, username, full_name)
        row = await asyncio.to_thread(db.resolve_username, raw[1:])
        if row:
            return Target(int(row["user_id"]), row.get("username", ""), row.get("full_name", ""))
    return None


def _reason(context: ContextTypes.DEFAULT_TYPE) -> str:
    args = list(context.args or [])
    return " ".join(args[1:]).strip()[:500]


def _target_error(command: str) -> str:
    return (
        f"❌ Informe o alvo para <code>/{command}</code>: responda à mensagem do usuário "
        f"ou use um ID numérico/@username conhecido."
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
    if not user.is_bot and int(user.id) not in KNOWN_USERS:
        target = _remember_user_in_memory(user)
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
        "Use /help para consultar a forma de uso.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_and_cleanup(
        update,
        "🛡️ <b>Jtzin Administrator Bot</b>\n\n"
        "<b>Comandos disponíveis</b>\n"
        "<code>/blacklist</code> — adiciona o alvo à blacklist deste grupo e apaga as mensagens dele.\n"
        "<code>/banperm</code> — bane permanentemente o alvo deste grupo.\n"
        "<code>/allban</code> — o proprietário bane o alvo em todos os grupos registrados.\n\n"
        "Use respondendo à mensagem do alvo ou informe o ID. O bot precisa ser administrador "
        "com permissão para apagar mensagens e restringir membros. O allban é exclusivo do proprietário.",
    )


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
    await _remember_message_context(update)
    target = _remember_user_in_memory(user)
    user_id = target.user_id

    # Allban tem prioridade e tenta reforçar o banimento em chats onde o bot foi adicionado depois.
    if user_id in ALLBAN_CACHE and not _is_owner(user_id):
        await _safe_delete(message)
        await _ban_in_chat(context, chat.id, target)
        return
    if user_id in BANPERM_CACHE.get(chat.id, set()) or user_id in BLACKLIST_CACHE.get(chat.id, set()):
        await _safe_delete(message)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, RetryAfter):
        logger.warning("Telegram solicitou RetryAfter: %s", context.error)
    else:
        logger.exception("Erro não tratado no Bot API", exc_info=context.error)


async def post_init(app: Application):
    global BOT_USER_ID
    BOT_USER_ID = (await app.bot.get_me()).id
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Iniciar o bot"),
            BotCommand("help", "Ver instruções"),
            BotCommand("blacklist", "Adicionar à blacklist local"),
            BotCommand("banperm", "Banir permanentemente no grupo"),
            BotCommand("allban", "Banir em todos os grupos — proprietário"),
        ]
    )
    logger.info("Jtzin Bot API online; proprietários=%s", ",".join(str(owner_id) for owner_id in sorted(OWNER_IDS)))


async def post_shutdown(app: Application):
    for task in list(_cleanup_tasks):
        task.cancel()
    if _cleanup_tasks:
        await asyncio.gather(*_cleanup_tasks, return_exceptions=True)
    await asyncio.to_thread(db.close)


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(False)
        .build()
    )
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("banperm", cmd_banperm))
    app.add_handler(CommandHandler("allban", cmd_allban))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, on_group_message), group=1)
    app.add_error_handler(error_handler)
    logger.info("Iniciando polling do Jtzin Bot API")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
