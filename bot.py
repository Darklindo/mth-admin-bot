import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from telegram import ChatPermissions, Update, BotCommand
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

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado. Crie o arquivo .env.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID não configurado. Crie o arquivo .env.")

DB_PATH = DATA_DIR / "bot.db"

# Anti-spam: 7 mensagens em 8 segundos => mensagens excedentes são apagadas.
SPAM_LIMIT = 7
SPAM_WINDOW = 8

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mth-admin")

spam_buckets = defaultdict(deque)


class Database:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init()

    def init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                chat_type TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT
            );

            CREATE TABLE IF NOT EXISTS blacklist (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                added_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                antispam INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(chat_id, user_id)
            );
            """
        )
        self.conn.commit()

    def register_chat(self, chat_id, title, chat_type):
        self.conn.execute(
            """
            INSERT INTO chats(chat_id,title,chat_type,active,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                chat_type=excluded.chat_type,
                active=1
            """,
            (chat_id, title or "", chat_type, 1, int(time.time())),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO settings(chat_id, antispam) VALUES(?,1)",
            (chat_id,),
        )
        self.conn.commit()

    def set_active(self, chat_id, active):
        self.conn.execute("UPDATE chats SET active=? WHERE chat_id=?", (int(active), chat_id))
        self.conn.commit()

    def active_chats(self):
        return self.conn.execute(
            "SELECT chat_id,title,chat_type FROM chats WHERE active=1 ORDER BY chat_id"
        ).fetchall()

    def remember_user(self, user):
        if not user:
            return
        username = (user.username or "").lower().lstrip("@") or None
        self.conn.execute(
            """
            INSERT INTO users(user_id,username,first_name)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (user.id, username, user.first_name or ""),
        )
        self.conn.commit()

    def resolve_username(self, username):
        username = username.lower().lstrip("@")
        row = self.conn.execute(
            "SELECT user_id FROM users WHERE username=? LIMIT 1", (username,)
        ).fetchone()
        return int(row["user_id"]) if row else None

    def add_blacklist(self, chat_id, user_id, username, added_by):
        self.conn.execute(
            """
            INSERT INTO blacklist(chat_id,user_id,username,added_by,created_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
                username=excluded.username,
                added_by=excluded.added_by
            """,
            (chat_id, user_id, username or "", added_by, int(time.time())),
        )
        self.conn.commit()

    def remove_blacklist(self, chat_id, user_id):
        cur = self.conn.execute(
            "DELETE FROM blacklist WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_blacklisted(self, chat_id, user_id):
        row = self.conn.execute(
            "SELECT 1 FROM blacklist WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return row is not None

    def blacklist_rows(self, chat_id):
        return self.conn.execute(
            "SELECT user_id,username FROM blacklist WHERE chat_id=? ORDER BY created_at",
            (chat_id,),
        ).fetchall()

    def set_antispam(self, chat_id, enabled):
        self.conn.execute(
            """
            INSERT INTO settings(chat_id,antispam) VALUES(?,?)
            ON CONFLICT(chat_id) DO UPDATE SET antispam=excluded.antispam
            """,
            (chat_id, int(enabled)),
        )
        self.conn.commit()

    def antispam_enabled(self, chat_id):
        row = self.conn.execute(
            "SELECT antispam FROM settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return bool(row["antispam"]) if row else True

    def add_warning(self, chat_id, user_id):
        self.conn.execute(
            """
            INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1
            """,
            (chat_id, user_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT count FROM warnings WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        return int(row["count"])

    def close(self):
        self.conn.close()


db = Database(DB_PATH)


def owner_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


async def require_owner(update: Update) -> bool:
    if owner_only(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Este comando é exclusivo do dono.")
    return False


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if user.id == OWNER_ID:
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError:
        return False


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if await is_admin(update, context):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Apenas administradores podem usar este comando.")
    return False


async def bot_can_delete(chat_id, context):
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_delete_messages", False) or member.status == "creator"
    except TelegramError:
        return False


async def bot_can_restrict(chat_id, context):
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        return getattr(member, "can_restrict_members", False) or member.status == "creator"
    except TelegramError:
        return False


async def safe_delete(message):
    try:
        await message.delete()
        return True
    except (BadRequest, Forbidden, TelegramError):
        return False


def target_from_update(update: Update):
    msg = update.effective_message
    if not msg:
        return None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user

    if not update.message:
        return None

    args = update.message.text.split(maxsplit=2) if update.message.text else []
    if len(args) < 2:
        return None

    raw = args[1].strip()
    if raw.startswith("@"):
        uid = db.resolve_username(raw)
        if uid:
            return uid
        return None

    if re.fullmatch(r"\d{4,20}", raw):
        return int(raw)

    return None


async def target_required(update, context):
    target = target_from_update(update)
    if target is None:
        await update.effective_message.reply_text(
            "Use respondendo à mensagem do usuário, ou informe o ID.\n"
            "Ex.: /banperm 123456789\n"
            "Para @username, o bot precisa já ter visto esse usuário no grupo."
        )
    return target


async def register_group(update: Update):
    chat = update.effective_chat
    if chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        db.register_chat(chat.id, chat.title or "", chat.type)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        db.register_chat(chat.id, chat.title or "", chat.type)
        logger.info("Chat registrado: %s (%s)", chat.id, chat.title)
    elif status in ("left", "kicked"):
        db.set_active(chat.id, False)


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat:
        db.register_chat(chat.id, chat.title or "", chat.type)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return

    db.register_chat(chat.id, chat.title or "", chat.type)
    db.remember_user(user)

    if user.is_bot:
        return

    # Blacklist tem prioridade sobre o anti-spam.
    if db.is_blacklisted(chat.id, user.id):
        await safe_delete(msg)
        return

    if not db.antispam_enabled(chat.id):
        return

    # Não aplicar anti-spam a administradores.
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in ("administrator", "creator"):
            return
    except TelegramError:
        return

    now = time.monotonic()
    bucket = spam_buckets[(chat.id, user.id)]
    while bucket and now - bucket[0] > SPAM_WINDOW:
        bucket.popleft()
    bucket.append(now)

    if len(bucket) > SPAM_LIMIT:
        await safe_delete(msg)
        if len(bucket) == SPAM_LIMIT + 1:
            await msg.reply_text(
                f"⚠️ {user.mention_html()} diminua o ritmo de mensagens para evitar spam.",
                parse_mode="HTML",
            )


async def cmd_start(update, context):
    await update.effective_message.reply_text(
        "🛡️ MTH ADMIN BOT\n\n"
        "Use /help para ver os comandos.\n"
        "Adicione o bot como administrador do grupo e conceda as permissões necessárias."
    )


async def cmd_help(update, context):
    text = (
        "🛡️ <b>MTH ADMIN BOT</b>\n\n"
        "<b>Moderação</b>\n"
        "/ban — banir usuário\n"
        "/banperm — banimento permanente\n"
        "/unban — remover ban\n"
        "/kick — remover usuário\n"
        "/mute — silenciar por minutos\n"
        "/unmute — remover silêncio\n"
        "/warn — advertência\n\n"
        "<b>Blacklist</b>\n"
        "/blacklist — apagar automaticamente mensagens do usuário\n"
        "/unblacklist — remover da blacklist\n"
        "/blacklistlist — listar blacklist\n\n"
        "<b>Anti-spam</b>\n"
        "/antispam on|off\n\n"
        "<b>Dono</b>\n"
        "/divulgar texto — enviar aos chats registrados\n"
        "/chats — mostrar chats registrados\n"
        "/id — mostrar seu ID"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def cmd_id(update, context):
    await update.effective_message.reply_text(f"🆔 Seu ID: <code>{update.effective_user.id}</code>", parse_mode="HTML")


async def cmd_ban(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target
    chat_id = update.effective_chat.id

    if not await bot_can_restrict(chat_id, context):
        await update.effective_message.reply_text("❌ O bot precisa da permissão de restringir/banir usuários.")
        return

    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await update.effective_message.reply_text("🔨 Usuário banido.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ Não foi possível banir: {e}")


async def cmd_banperm(update, context):
    # Telegram trata ban sem data de expiração como permanente.
    await cmd_ban(update, context)


async def cmd_unban(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, user_id, only_if_banned=True)
        await update.effective_message.reply_text("✅ Ban removido.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ Não foi possível remover o ban: {e}")


async def cmd_kick(update, context):
    # Kick = ban temporário e desbanir imediatamente.
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_id)
        await context.bot.unban_chat_member(update.effective_chat.id, user_id)
        await update.effective_message.reply_text("👢 Usuário removido do grupo.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ Não foi possível remover: {e}")


async def cmd_mute(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return

    minutes = 10
    if context.args and context.args[-1].isdigit():
        minutes = max(1, min(int(context.args[-1]), 10080))

    user_id = target.id if hasattr(target, "id") else target
    permissions = ChatPermissions(can_send_messages=False)

    try:
        until = int(time.time()) + minutes * 60
        await context.bot.restrict_chat_member(
            update.effective_chat.id, user_id, permissions=permissions, until_date=until
        )
        await update.effective_message.reply_text(f"🔇 Usuário silenciado por {minutes} minuto(s).")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ Não foi possível silenciar: {e}")


async def cmd_unmute(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target

    try:
        chat = await context.bot.get_chat(update.effective_chat.id)
        permissions = chat.permissions or ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(
            update.effective_chat.id, user_id, permissions=permissions
        )
        await update.effective_message.reply_text("🔊 Usuário liberado.")
    except TelegramError as e:
        await update.effective_message.reply_text(f"❌ Não foi possível liberar: {e}")


async def cmd_warn(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target
    count = db.add_warning(update.effective_chat.id, user_id)
    await update.effective_message.reply_text(f"⚠️ Advertência registrada. Total: {count}")


async def cmd_blacklist(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target
    username = getattr(target, "username", "") if hasattr(target, "username") else ""
    db.add_blacklist(update.effective_chat.id, user_id, username, update.effective_user.id)

    # Tenta remover a mensagem do comando também.
    await safe_delete(update.effective_message)
    try:
        await context.bot.send_message(
            update.effective_chat.id,
            "🚫 Usuário adicionado à blacklist. As próximas mensagens dele serão apagadas automaticamente."
        )
    except TelegramError:
        pass


async def cmd_unblacklist(update, context):
    if not await require_admin(update, context):
        return
    target = await target_required(update, context)
    if target is None:
        return
    user_id = target.id if hasattr(target, "id") else target
    if db.remove_blacklist(update.effective_chat.id, user_id):
        await update.effective_message.reply_text("✅ Usuário removido da blacklist.")
    else:
        await update.effective_message.reply_text("ℹ️ Esse usuário não está na blacklist.")


async def cmd_blacklistlist(update, context):
    if not await require_admin(update, context):
        return
    rows = db.blacklist_rows(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("📋 Blacklist vazia.")
        return
    lines = ["📋 <b>BLACKLIST</b>"]
    for i, row in enumerate(rows, 1):
        name = f"@{row['username']}" if row["username"] else str(row["user_id"])
        lines.append(f"{i}. {name} — <code>{row['user_id']}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_antispam(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Use: /antispam on ou /antispam off")
        return
    enabled = context.args[0].lower() == "on"
    db.set_antispam(update.effective_chat.id, enabled)
    await update.effective_message.reply_text(
        f"🛡️ Anti-spam {'ATIVADO' if enabled else 'DESATIVADO'}."
    )


async def cmd_chats(update, context):
    if not await require_owner(update):
        return
    rows = db.active_chats()
    if not rows:
        await update.effective_message.reply_text("Nenhum grupo/canal registrado ainda.")
        return
    lines = [f"📡 <b>CHATS REGISTRADOS: {len(rows)}</b>"]
    for row in rows[:50]:
        lines.append(f"• {row['title'] or 'Sem título'} — <code>{row['chat_id']}</code>")
    if len(rows) > 50:
        lines.append(f"… e mais {len(rows)-50}.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_broadcast(update, context):
    if not await require_owner(update):
        return

    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await update.effective_message.reply_text("Use: /divulgar seu texto aqui")
        return

    rows = db.active_chats()
    if not rows:
        await update.effective_message.reply_text("❌ Nenhum chat registrado.")
        return

    sent = failed = 0
    status = await update.effective_message.reply_text(
        f"📢 Iniciando divulgação em {len(rows)} chat(s)..."
    )

    for row in rows:
        chat_id = row["chat_id"]
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            sent += 1
            # Pequena pausa para reduzir risco de flood/rate-limit.
            await asyncio_sleep(0.8)
        except RetryAfter as e:
            await asyncio_sleep(float(e.retry_after) + 0.5)
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                sent += 1
            except TelegramError:
                failed += 1
        except (Forbidden, BadRequest):
            failed += 1
        except TelegramError:
            failed += 1

    await status.edit_text(
        f"📢 <b>DIVULGAÇÃO CONCLUÍDA</b>\n\n"
        f"✅ Enviadas: {sent}\n"
        f"❌ Falhas: {failed}\n"
        f"📡 Total: {len(rows)}",
        parse_mode="HTML",
    )


async def asyncio_sleep(seconds):
    import asyncio
    await asyncio.sleep(seconds)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Erro não tratado: %s", context.error)


async def post_init(app: Application):
    commands = [
        BotCommand("start", "Iniciar"),
        BotCommand("help", "Ajuda"),
        BotCommand("id", "Ver seu ID"),
        BotCommand("ban", "Banir usuário"),
        BotCommand("banperm", "Banir permanentemente"),
        BotCommand("unban", "Remover ban"),
        BotCommand("kick", "Remover usuário"),
        BotCommand("mute", "Silenciar"),
        BotCommand("unmute", "Liberar usuário"),
        BotCommand("warn", "Advertir"),
        BotCommand("blacklist", "Adicionar à blacklist"),
        BotCommand("unblacklist", "Remover da blacklist"),
        BotCommand("blacklistlist", "Listar blacklist"),
        BotCommand("antispam", "Ativar/desativar anti-spam"),
        BotCommand("divulgar", "Divulgar (somente dono)"),
        BotCommand("chats", "Chats registrados (dono)"),
    ]
    await app.bot.set_my_commands(commands)


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )

    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("banperm", cmd_banperm))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("unblacklist", cmd_unblacklist))
    app.add_handler(CommandHandler("blacklistlist", cmd_blacklistlist))
    app.add_handler(CommandHandler("antispam", cmd_antispam))
    app.add_handler(CommandHandler("divulgar", cmd_broadcast))
    app.add_handler(CommandHandler("chats", cmd_chats))

    app.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POSTS, on_channel_post),
        group=1,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, on_group_message),
        group=2,
    )

    app.add_error_handler(error_handler)

    logger.info("MTH Admin Bot iniciando...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
