import logging
import os
import re
import sqlite3
import time
import asyncio
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
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SECOND_OWNER_ID = 6466326477  # @MHZINTADEVOLTAPORRRRRRAAAA

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não configurado. Crie o arquivo .env.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID não configurado. Crie o arquivo .env.")

DB_PATH = DATA_DIR / "bot.db"

# Anti-spam e Anti-Raid
SPAM_LIMIT = 7
SPAM_WINDOW = 8
RAID_LIMIT = 10
RAID_WINDOW = 10

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mth-admin")

spam_buckets = defaultdict(deque)
join_buckets = defaultdict(deque)

# --- BANCO DE DADOS ---
class Database:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init()

    def init(self):
        try:
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
                    antispam INTEGER NOT NULL DEFAULT 1,
                    antilink INTEGER NOT NULL DEFAULT 0,
                    antiraid INTEGER NOT NULL DEFAULT 1,
                    log_channel INTEGER,
                    welcome_text TEXT,
                    welcome_enabled INTEGER NOT NULL DEFAULT 0,
                    night_mode_auto INTEGER NOT NULL DEFAULT 0,
                    night_start TEXT DEFAULT '23:00',
                    night_end TEXT DEFAULT '07:00'
                );

                CREATE TABLE IF NOT EXISTS warnings (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS link_whitelist (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, user_id)
                );
                """
            )
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro na inicialização do DB: {e}")

    def register_chat(self, chat_id, title, chat_type):
        try:
            self.conn.execute(
                """
                INSERT INTO chats(chat_id,title,chat_type,active,created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title=excluded.title,
                    chat_type=excluded.chat_type,
                    active=1
                """,
                (int(chat_id), title or "", chat_type, 1, int(time.time())),
            )
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (int(chat_id),))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro ao registrar chat: {e}")

    def get_setting(self, chat_id, key, default=0):
        try:
            row = self.conn.execute(f"SELECT {key} FROM settings WHERE chat_id=?", (int(chat_id),)).fetchone()
            if row and row[key] is not None:
                return row[key]
            return default
        except:
            return default

    def set_setting(self, chat_id, key, value):
        try:
            self.conn.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, int(chat_id)))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro ao salvar setting {key}: {e}")

    def add_link_whitelist(self, chat_id, user_id):
        try:
            self.conn.execute("INSERT OR IGNORE INTO link_whitelist(chat_id, user_id) VALUES(?,?)", (int(chat_id), int(user_id)))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro na whitelist: {e}")

    def is_link_whitelisted(self, chat_id, user_id):
        try:
            row = self.conn.execute("SELECT 1 FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
            return row is not None
        except:
            return False

    def active_chats_with_night_mode(self):
        try:
            return self.conn.execute("SELECT chat_id, night_start, night_end FROM settings WHERE night_mode_auto=1").fetchall()
        except:
            return []

db = Database(DB_PATH)

# --- AUXILIARES ---
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat: return False
        if user.id in [OWNER_ID, SECOND_OWNER_ID]: return True
        if chat.type == ChatType.PRIVATE: return True
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except: return False

async def is_owner(update: Update) -> bool:
    user = update.effective_user
    if not user: return False
    return user.id in [OWNER_ID, SECOND_OWNER_ID]

async def safe_delete(message):
    try: await message.delete(); return True
    except: return False

def target_from_update(update: Update):
    try:
        msg = update.effective_message
        if not msg: return None
        if msg.reply_to_message and msg.reply_to_message.from_user:
            return msg.reply_to_message.from_user
        args = msg.text.split() if msg.text else []
        if len(args) < 2: return None
        raw = args[1].strip()
        if raw.startswith("@"):
            uid = db.resolve_username(raw)
            return uid if uid else None
        if re.fullmatch(r"\d+", raw): return int(raw)
    except: pass
    return None

# --- COMANDOS ---
async def cmd_start(update, context):
    keyboard = [[InlineKeyboardButton("📚 Ajuda", callback_data="help_main")]]
    await update.message.reply_text("🛡️ <b>MTH ADMIN BOT V2.1</b>\n\nPronto para moderar com estabilidade!", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_help(update, context):
    text = (
        "🛡️ <b>MENU DE AJUDA</b>\n\n"
        "<b>Moderação:</b> /ban, /mute, /kick, /warn, /purge\n"
        "<b>Links:</b> /allowlink, /removelink\n"
        "<b>Controle:</b> /lock, /unlock, /settings\n"
        "<b>Dono:</b> /chats"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_lock(update, context):
    if not await is_admin(update, context): return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, ChatPermissions(can_send_messages=False))
        await update.message.reply_text("🔒 <b>Grupo Fechado!</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_unlock(update, context):
    if not await is_admin(update, context): return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
        await update.message.reply_text("🔓 <b>Grupo Aberto!</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    antispam = "✅" if db.get_setting(chat_id, "antispam", 1) else "❌"
    antilink = "✅" if db.get_setting(chat_id, "antilink", 0) else "❌"
    antiraid = "✅" if db.get_setting(chat_id, "antiraid", 1) else "❌"
    night_auto = "✅" if db.get_setting(chat_id, "night_mode_auto", 0) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {antispam}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {antilink}", callback_data="toggle_antilink")],
        [InlineKeyboardButton(f"Anti-Raid: {antiraid}", callback_data="toggle_antiraid")],
        [InlineKeyboardButton(f"Modo Noturno Auto: {night_auto}", callback_data="toggle_night")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

# --- TAREFAS AUTOMÁTICAS ---
async def night_mode_checker(context: ContextTypes.DEFAULT_TYPE):
    try:
        now = datetime.now().strftime("%H:%M")
        rows = db.active_chats_with_night_mode()
        for row in rows:
            chat_id = row['chat_id']
            if now == row['night_start']:
                await context.bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=False))
                await context.bot.send_message(chat_id, "🌙 <b>Modo Noturno Ativado!</b>", parse_mode=ParseMode.HTML)
            elif now == row['night_end']:
                await context.bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
                await context.bot.send_message(chat_id, "☀️ <b>Modo Noturno Desativado!</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Erro no checker noturno: {e}")

# --- HANDLERS ---
async def message_handler(update, context):
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not msg or not chat or not user or user.is_bot: return

        db.register_chat(chat.id, chat.title, chat.type)
        if await is_admin(update, context): return

        if db.get_setting(chat.id, "antilink", 0):
            if any(entity.type in ["url", "text_link"] for entity in msg.entities or []):
                if not db.is_link_whitelisted(chat.id, user.id):
                    await safe_delete(msg)
                    return

        if db.get_setting(chat.id, "antispam", 1):
            now = time.monotonic()
            bucket = spam_buckets[(chat.id, user.id)]
            while bucket and now - bucket[0] > SPAM_WINDOW: bucket.popleft()
            bucket.append(now)
            if len(bucket) > SPAM_LIMIT:
                await safe_delete(msg)
                return
    except Exception as e:
        logger.error(f"Erro no message_handler: {e}")

async def on_callback(update, context):
    try:
        query = update.callback_query
        if not query: return
        await query.answer()
        chat_id = query.message.chat_id
        if not await is_admin(update, context): return

        if query.data == "toggle_antispam":
            db.set_setting(chat_id, "antispam", 0 if db.get_setting(chat_id, "antispam", 1) else 1)
        elif query.data == "toggle_antilink":
            db.set_setting(chat_id, "antilink", 0 if db.get_setting(chat_id, "antilink", 0) else 1)
        elif query.data == "toggle_antiraid":
            db.set_setting(chat_id, "antiraid", 0 if db.get_setting(chat_id, "antiraid", 1) else 1)
        elif query.data == "toggle_night":
            db.set_setting(chat_id, "night_mode_auto", 0 if db.get_setting(chat_id, "night_mode_auto", 0) else 1)
        
        await cmd_settings(update, context)
    except Exception as e:
        logger.error(f"Erro no callback: {e}")

async def post_init(app: Application):
    try:
        commands = [
            BotCommand("start", "Iniciar"),
            BotCommand("help", "Ajuda"),
            BotCommand("settings", "Configurações"),
            BotCommand("lock", "Fechar grupo"),
            BotCommand("unlock", "Abrir grupo"),
        ]
        await app.bot.set_my_commands(commands)
        
        # Tenta iniciar a JobQueue com segurança
        if app.job_queue:
            app.job_queue.run_repeating(night_mode_checker, interval=60, first=10)
            logger.info("JobQueue iniciada com sucesso.")
        else:
            logger.warning("JobQueue não disponível. Modo Noturno Automático desativado.")
    except Exception as e:
        logger.error(f"Erro no post_init: {e}")

def main():
    try:
        app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("settings", cmd_settings))
        app.add_handler(CommandHandler("lock", cmd_lock))
        app.add_handler(CommandHandler("unlock", cmd_unlock))
        app.add_handler(CommandHandler("allowlink", lambda u, c: None)) # Adicionar handler real se necessário
        app.add_handler(CallbackQueryHandler(on_callback))
        app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))
        
        logger.info("Bot iniciado...")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.critical(f"Erro fatal ao iniciar o bot: {e}")

if __name__ == "__main__":
    main()
