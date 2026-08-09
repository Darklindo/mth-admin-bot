import logging
import os
import re
import sqlite3
import time
import asyncio
import sys
import functools
from collections import defaultdict, deque
from pathlib import Path
from datetime import timedelta, datetime

from dotenv import load_dotenv
from telegram import (
    ChatPermissions, 
    Update, 
    BotCommand, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    WebAppInfo
)
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError, TimedOut, NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- CONFIGURAÇÕES INICIAIS ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "6822870889"))
SECOND_OWNER_ID = 6466326477

if not BOT_TOKEN:
    print("ERRO: BOT_TOKEN não configurado no .env")
    sys.exit(1)

DB_PATH = DATA_DIR / "bot.db"

# Configuração de Logs
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("Jtzin-Admin")
logger.setLevel(logging.INFO)

# --- DECORADOR DE ERROS ---
def error_handler(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            if "Message is not modified" in str(e): return
            logger.error(f"Erro em {func.__name__}: {e}")
    return wrapper

# --- BANCO DE DADOS ---
class Database:
    def __init__(self, path: Path):
        self.path = path
        self._connect()

    def _connect(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    def execute(self, query, params=(), commit=False):
        try:
            cursor = self.conn.execute(query, params)
            if commit: self.conn.commit()
            return cursor
        except Exception as e:
            logger.error(f"DB Error: {e}")
            return None

    def register_chat(self, chat_id, title, chat_type):
        self.execute(
            "INSERT INTO chats(chat_id,title,chat_type,active,created_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type",
            (int(chat_id), title or "", chat_type, 1, int(time.time())),
            commit=True
        )
        self.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (int(chat_id),), commit=True)

    def set_chat_active(self, chat_id, active):
        return self.execute("UPDATE chats SET active=? WHERE chat_id=?", (int(active), int(chat_id)), commit=True) is not None

    def get_setting(self, chat_id, key, default=0):
        row = self.execute(f"SELECT {key} FROM settings WHERE chat_id=?", (int(chat_id),)).fetchone()
        return row[key] if row and row[key] is not None else default

    def set_setting(self, chat_id, key, value):
        self.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, int(chat_id)), commit=True)

    def is_link_whitelisted(self, chat_id, user_id):
        row = self.execute("SELECT 1 FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
        return row is not None

    def get_global_status(self, user_id):
        row = self.execute("SELECT type FROM global_blacklist WHERE user_id=?", (int(user_id),)).fetchone()
        return row['type'] if row else None

    def add_global_blacklist(self, user_id, type_name, reason=""):
        self.execute("INSERT OR REPLACE INTO global_blacklist(user_id, type, reason, created_at) VALUES(?,?,?,?)", (int(user_id), type_name, reason, int(time.time())), commit=True)

    def remove_global_blacklist(self, user_id):
        self.execute("DELETE FROM global_blacklist WHERE user_id=?", (int(user_id),), commit=True)

    def is_shadow_banned(self, user_id):
        row = self.execute("SELECT 1 FROM shadow_ban WHERE user_id=?", (int(user_id),)).fetchone()
        return row is not None

    def add_shadow_ban(self, user_id):
        self.execute("INSERT OR REPLACE INTO shadow_ban(user_id, created_at) VALUES(?,?)", (int(user_id), int(time.time())), commit=True)

    def remove_shadow_ban(self, user_id):
        self.execute("DELETE FROM shadow_ban WHERE user_id=?", (int(user_id),), commit=True)

    def active_chats(self):
        return self.execute("SELECT chat_id FROM chats WHERE active=1").fetchall()

    def all_chats(self):
        return self.execute("SELECT chat_id FROM chats").fetchall()

    def resolve_username(self, username):
        username = username.lower().lstrip("@")
        row = self.execute("SELECT user_id FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
        return int(row["user_id"]) if row else None

    def remember_user(self, user):
        if not user: return
        username = (user.username or "").lower().lstrip("@") or None
        self.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user.id, username, user.first_name or ""),
            commit=True
        )

    def add_captcha_pending(self, chat_id, user_id, message_id):
        expiry = int(time.time()) + 60
        self.execute("INSERT OR REPLACE INTO captcha_pending(chat_id, user_id, message_id, expiry) VALUES(?,?,?,?)", (int(chat_id), int(user_id), int(message_id), expiry), commit=True)

    def remove_captcha_pending(self, chat_id, user_id):
        self.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)

    def get_expired_captchas(self):
        return self.execute("SELECT chat_id, user_id, message_id FROM captcha_pending WHERE expiry < ?", (int(time.time()),)).fetchall()

db = Database(DB_PATH)

# --- AUXILIARES ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat: return False
    if user.id in [OWNER_ID, SECOND_OWNER_ID]: return True
    if chat.type == ChatType.PRIVATE: return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except: return False

def is_immune(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID]

async def safe_delete(message):
    try: await message.delete(); return True
    except: return False

def get_target(update: Update):
    msg = update.effective_message
    if not msg: return None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    args = msg.text.split()
    if len(args) > 1:
        raw = args[1].strip()
        if raw.startswith("@"): return db.resolve_username(raw)
        if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()): return int(raw)
    return None

# --- COMANDOS ---
@error_handler
async def cmd_start(update, context):
    await update.message.reply_text("🛡️ <b>Jtzin Administrator V1</b>", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_help(update, context):
    text = (
        "🛡️ <b>LISTA DE COMANDOS</b>\n\n"
        "<b>Moderação:</b> /ban, /mute, /kick, /warn, /purge, /lock, /unlock\n"
        "<b>Segurança:</b> /settings, /allowlink, /removelink, /shadow, /unshadow\n"
        "<b>Global:</b> /allban, /allblack, /blacklist, /unblacklist\n"
        "<b>Broadcast:</b> /msg, /chats"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_allban(update, context):
    if update.effective_user.id != OWNER_ID: return
    target_id = get_target(update)
    if not target_id or is_immune(target_id): return
    db.add_global_blacklist(target_id, 'ban', "Ban Global")
    for row in db.all_chats():
        try: await context.bot.ban_chat_member(row['chat_id'], target_id)
        except: continue
    await update.message.reply_text(f"✅ Usuário {target_id} banido globalmente.")

@error_handler
async def cmd_allblack(update, context):
    if update.effective_user.id != OWNER_ID: return
    target_id = get_target(update)
    if not target_id or is_immune(target_id): return
    db.add_global_blacklist(target_id, 'black', "Blacklist Global")
    await update.message.reply_text(f"✅ Usuário {target_id} em blacklist global.")

@error_handler
async def cmd_unblacklist(update, context):
    if update.effective_user.id != OWNER_ID: return
    target_id = get_target(update)
    if not target_id: return
    db.remove_global_blacklist(target_id)
    await update.message.reply_text(f"✅ Usuário {target_id} removido da blacklist.")

@error_handler
async def cmd_msg(update, context):
    if update.effective_user.id != OWNER_ID: return
    msg = update.effective_message
    target_chats = db.active_chats()
    sent = 0
    caption = " ".join(context.args)
    for row in target_chats:
        try:
            if msg.reply_to_message:
                await context.bot.copy_message(row['chat_id'], msg.chat_id, msg.reply_to_message.message_id, caption=caption if caption else None)
            else:
                if caption: await context.bot.send_message(row['chat_id'], caption)
            sent += 1
            await asyncio.sleep(0.3)
        except: continue
    await msg.reply_text(f"📢 Transmissão concluída: {sent} chats.")

@error_handler
async def cmd_lock(update, context):
    if not await is_admin(update, context): return
    await context.bot.set_chat_permissions(update.effective_chat.id, ChatPermissions(can_send_messages=False))
    await update.message.reply_text("🔒 Grupo Fechado.")

@error_handler
async def cmd_unlock(update, context):
    if not await is_admin(update, context): return
    perms = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_other_messages=True, can_add_web_page_previews=True)
    await context.bot.set_chat_permissions(update.effective_chat.id, perms)
    await update.message.reply_text("🔓 Grupo Aberto.")

@error_handler
async def cmd_purge(update, context):
    if not await is_admin(update, context): return
    msg = update.effective_message
    amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
    if not amount and msg.reply_to_message:
        amount = msg.message_id - msg.reply_to_message.message_id
    if not amount: return
    amount = min(amount, 100)
    await safe_delete(msg)
    for i in range(amount):
        try: await context.bot.delete_message(msg.chat_id, msg.message_id - i - 1)
        except: continue

@error_handler
async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_immune(target_id): return
    await context.bot.ban_chat_member(update.effective_chat.id, target_id)
    await update.message.reply_text("🚫 Usuário banido.")

@error_handler
async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_immune(target_id): return
    await context.bot.restrict_chat_member(update.effective_chat.id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now() + timedelta(hours=24))
    await update.message.reply_text("🔇 Usuário mutado por 24h.")

@error_handler
async def cmd_shadow(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_immune(target_id): return
    db.add_shadow_ban(target_id)
    await update.message.reply_text("🌑 Usuário em Shadow Ban.")

@error_handler
async def cmd_unshadow(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return
    db.remove_shadow_ban(target_id)
    await update.message.reply_text("✅ Shadow Ban removido.")

@error_handler
async def cmd_chats(update, context):
    if update.effective_user.id != OWNER_ID: return
    rows = db.all_chats()
    if not rows: return await update.message.reply_text("Nenhum chat registrado.")
    lines = ["📡 <b>CHATS:</b>"]
    for row in rows:
        lines.append(f"• <code>{row['chat_id']}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

@error_handler
async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(chat_id, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(chat_id, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")],
        [InlineKeyboardButton(f"Captcha: {'✅' if db.get_setting(chat_id, 'captcha_enabled', 0) else '❌'}", callback_data="toggle_captcha")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

# --- HANDLERS ---
@error_handler
async def join_handler(update, context):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or user.is_bot: return
    if db.get_setting(chat.id, "captcha_enabled", 0):
        if is_immune(user.id): return
        try:
            await context.bot.restrict_chat_member(chat.id, user.id, permissions=ChatPermissions(can_send_messages=False))
            keyboard = [[InlineKeyboardButton("✅ Verificar", callback_data=f"verify_{user.id}")]]
            msg = await context.bot.send_message(chat.id, f"👋 {user.first_name}, clique abaixo para verificar.", reply_markup=InlineKeyboardMarkup(keyboard))
            db.add_captcha_pending(chat.id, user.id, msg.message_id)
        except: pass

@error_handler
async def message_handler(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot: return
    db.register_chat(msg.chat_id, msg.chat.title, msg.chat.type)
    db.remember_user(user)
    if is_immune(user.id): return
    if db.is_shadow_banned(user.id) or db.get_global_status(user.id):
        return await safe_delete(msg)
    if db.get_setting(msg.chat_id, "antilink") and any(e.type in ["url", "text_link"] for e in msg.entities or []):
        if not db.is_link_whitelisted(msg.chat_id, user.id):
            return await safe_delete(msg)

@error_handler
async def on_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    if data.startswith("verify_"):
        target_id = int(data.split("_")[1])
        if query.from_user.id != target_id: return
        db.remove_captcha_pending(chat_id, target_id)
        perms = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_other_messages=True, can_add_web_page_previews=True)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=perms)
        await query.message.edit_text("✅ Verificado.")
        await asyncio.sleep(2)
        await safe_delete(query.message)
    elif await is_admin(update, context):
        if data == "toggle_antispam": db.set_setting(chat_id, "antispam", 1 - db.get_setting(chat_id, "antispam", 1))
        elif data == "toggle_antilink": db.set_setting(chat_id, "antilink", 1 - db.get_setting(chat_id, "antilink", 0))
        elif data == "toggle_captcha": db.set_setting(chat_id, "captcha_enabled", 1 - db.get_setting(chat_id, "captcha_enabled", 0))
        await cmd_settings(update, context)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Iniciar"), BotCommand("help", "Ajuda"), BotCommand("settings", "Configurações"),
        BotCommand("lock", "Fechar"), BotCommand("unlock", "Abrir"), BotCommand("purge", "Limpar"),
        BotCommand("ban", "Banir"), BotCommand("mute", "Silenciar"), BotCommand("msg", "Transmissão")
    ])
    logger.info("Jtzin Administrator V1 ONLINE!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("msg", cmd_msg))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("allban", cmd_allban))
    app.add_handler(CommandHandler("allblack", cmd_allblack))
    app.add_handler(CommandHandler("blacklist", cmd_allblack))
    app.add_handler(CommandHandler("unblacklist", cmd_unblacklist))
    app.add_handler(CommandHandler("shadow", cmd_shadow))
    app.add_handler(CommandHandler("unshadow", cmd_unshadow))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, join_handler))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
