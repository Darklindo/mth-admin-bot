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
    InlineKeyboardMarkup
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
SECOND_OWNER_ID = 6466326477  # @MHZINTADEVOLTAPORRRRRRAAAA

if not BOT_TOKEN:
    print("ERRO: BOT_TOKEN não configurado no .env")
    sys.exit(1)

DB_PATH = DATA_DIR / "bot.db"

# Anti-spam e Anti-Raid
SPAM_LIMIT = 7
SPAM_WINDOW = 8
RAID_LIMIT = 10
RAID_WINDOW = 10

# Configuração de Logs
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.WARNING,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger("mth-admin")
logger.setLevel(logging.INFO)

spam_buckets = defaultdict(deque)

# --- DECORADOR DE ERROS ---
def error_handler(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            if "Message is not modified" in str(e): return
            logger.error(f"Erro em {func.__name__}: {e}")
            if update.effective_message:
                try: await update.effective_message.reply_text("❌ Erro ao processar comando.")
                except: pass
    return wrapper

# --- BANCO DE DADOS ---
class Database:
    def __init__(self, path: Path):
        self.path = path
        self._connect()
        self.init()

    def _connect(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    def init(self):
        try:
            with self.conn:
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
                    CREATE TABLE IF NOT EXISTS settings (
                        chat_id INTEGER PRIMARY KEY,
                        antispam INTEGER NOT NULL DEFAULT 1,
                        antilink INTEGER NOT NULL DEFAULT 0,
                        antiraid INTEGER NOT NULL DEFAULT 1,
                        night_mode_auto INTEGER NOT NULL DEFAULT 0,
                        night_start TEXT DEFAULT '23:00',
                        night_end TEXT DEFAULT '07:00'
                    );
                    CREATE TABLE IF NOT EXISTS link_whitelist (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS global_blacklist (
                        user_id INTEGER PRIMARY KEY,
                        type TEXT NOT NULL,
                        reason TEXT,
                        created_at INTEGER NOT NULL
                    );
                    """
                )
        except Exception as e:
            logger.error(f"Erro DB Init: {e}")

    def execute(self, query, params=(), commit=False):
        try:
            cursor = self.conn.execute(query, params)
            if commit: self.conn.commit()
            return cursor
        except Exception as e:
            logger.error(f"Erro DB Exec: {e}")
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

    def active_chats(self):
        return self.execute("SELECT chat_id FROM chats WHERE active=1").fetchall()

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
    await update.message.reply_text("🛡️ <b>MTH ADMIN SUPREMO V4.0</b>\n\nImunidade ativada. Comandos nucleares prontos.", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_msg(update, context):
    if update.effective_user.id != OWNER_ID: return
    msg = update.effective_message
    target_chats = db.active_chats()
    sent = 0

    # Determinar o que enviar
    source_msg = msg.reply_to_message if msg.reply_to_message else msg
    caption = " ".join(context.args) if msg.reply_to_message else " ".join(context.args)

    for row in target_chats:
        try:
            chat_id = row['chat_id']
            if msg.reply_to_message:
                await context.bot.copy_message(chat_id, msg.chat_id, msg.reply_to_message.message_id, caption=caption if caption else None)
            else:
                if not caption: continue
                await context.bot.send_message(chat_id, caption)
            sent += 1
            await asyncio.sleep(0.3)
        except: continue
    
    await msg.reply_text(f"📢 Transmissão concluída para {sent} chats.")

@error_handler
async def cmd_lock(update, context):
    if not await is_admin(update, context): return
    await context.bot.set_chat_permissions(update.effective_chat.id, ChatPermissions(can_send_messages=False))
    await update.message.reply_text("🔒 <b>Grupo Fechado!</b>", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_unlock(update, context):
    if not await is_admin(update, context): return
    perms = ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    await context.bot.set_chat_permissions(update.effective_chat.id, perms)
    await update.message.reply_text("🔓 <b>Grupo Aberto!</b>", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_purge(update, context):
    if not await is_admin(update, context): return
    msg = update.effective_message
    amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
    if not amount and msg.reply_to_message:
        amount = msg.message_id - msg.reply_to_message.message_id
    
    if not amount: return await msg.reply_text("Uso: /purge [número] ou responda.")
    
    amount = min(amount, 100)
    await safe_delete(msg)
    for i in range(amount):
        try: await context.bot.delete_message(msg.chat_id, msg.message_id - i - 1)
        except: continue

@error_handler
async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return await update.message.reply_text("Alvo não encontrado.")
    if is_immune(target_id): return await update.message.reply_text("❌ Impossível punir o Mestre.")
    await context.bot.ban_chat_member(update.effective_chat.id, target_id)
    await update.message.reply_text("🚫 Usuário banido.")

@error_handler
async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return await update.message.reply_text("Alvo não encontrado.")
    if is_immune(target_id): return await update.message.reply_text("❌ Impossível punir o Mestre.")
    await context.bot.restrict_chat_member(update.effective_chat.id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now() + timedelta(hours=24))
    await update.message.reply_text("🔇 Usuário mutado por 24h.")

@error_handler
async def cmd_allban(update, context):
    if update.effective_user.id != OWNER_ID: return
    target_id = get_target(update)
    if not target_id: return
    db.execute("INSERT OR REPLACE INTO global_blacklist(user_id, type, reason, created_at) VALUES(?,?,?,?)", (target_id, 'ban', "Nuclear", int(time.time())), commit=True)
    for row in db.execute("SELECT chat_id FROM chats").fetchall():
        try: await context.bot.ban_chat_member(row['chat_id'], target_id)
        except: continue
    await update.message.reply_text("☢️ Banimento Global aplicado.")

@error_handler
async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(chat_id, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(chat_id, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

# --- HANDLERS ---
@error_handler
async def message_handler(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot: return

    db.register_chat(msg.chat_id, msg.chat.title, msg.chat.type)
    db.remember_user(user)

    # Verificação de Imunidade e Blacklist
    if is_immune(user.id): return
    
    status = db.get_global_status(user.id)
    if status == 'ban':
        try: await context.bot.ban_chat_member(msg.chat_id, user.id)
        except: pass
        return await safe_delete(msg)
    if status == 'black':
        return await safe_delete(msg)

    if db.get_setting(msg.chat_id, "antilink") and any(e.type in ["url", "text_link"] for e in msg.entities or []):
        if not db.is_link_whitelisted(msg.chat_id, user.id):
            return await safe_delete(msg)

@error_handler
async def on_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context): return
    chat_id = query.message.chat_id
    if query.data == "toggle_antispam": db.set_setting(chat_id, "antispam", 1 - db.get_setting(chat_id, "antispam", 1))
    elif query.data == "toggle_antilink": db.set_setting(chat_id, "antilink", 1 - db.get_setting(chat_id, "antilink", 0))
    await cmd_settings(update, context) # Atualiza o menu

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Iniciar"), BotCommand("settings", "Configurações"),
        BotCommand("lock", "Fechar"), BotCommand("unlock", "Abrir"),
        BotCommand("purge", "Limpar"), BotCommand("ban", "Banir"),
        BotCommand("mute", "Silenciar"), BotCommand("msg", "Transmissão Supremo")
    ])
    logger.info("MTH ADMIN SUPREMO V4.0 ONLINE!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("msg", cmd_msg))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("allban", cmd_allban))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
