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
from telegram.error import BadRequest, Forbidden, TelegramError
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

# --- CACHE EM MEMÓRIA (ALTA PERFORMANCE) ---
class Cache:
    def __init__(self):
        self.global_blacklist = set()
        self.shadow_ban = set()
        self.link_whitelist = defaultdict(set)
        self.settings = {}

    def load_all(self, db_conn):
        # Carregar Blacklist Global
        cursor = db_conn.execute("SELECT user_id FROM global_blacklist")
        self.global_blacklist = {row[0] for row in cursor.fetchall()}
        
        # Carregar Shadow Ban
        cursor = db_conn.execute("SELECT user_id FROM shadow_ban")
        self.shadow_ban = {row[0] for row in cursor.fetchall()}
        
        # Carregar Whitelist de Links
        cursor = db_conn.execute("SELECT chat_id, user_id FROM link_whitelist")
        for row in cursor.fetchall():
            self.link_whitelist[row[0]].add(row[1])
            
        # Carregar Settings
        cursor = db_conn.execute("SELECT chat_id, antispam, antilink, captcha_enabled FROM settings")
        for row in cursor.fetchall():
            self.settings[row[0]] = {
                "antispam": row[1],
                "antilink": row[2],
                "captcha_enabled": row[3]
            }
        logger.info(f"Cache carregado: {len(self.global_blacklist)} bans, {len(self.shadow_ban)} shadows.")

cache = Cache()

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
        if int(chat_id) not in cache.settings:
            self.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (int(chat_id),), commit=True)
            cache.settings[int(chat_id)] = {"antispam": 1, "antilink": 0, "captcha_enabled": 0}

    def get_setting(self, chat_id, key, default=0):
        if chat_id in cache.settings:
            return cache.settings[chat_id].get(key, default)
        return default

    def set_setting(self, chat_id, key, value):
        self.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, int(chat_id)), commit=True)
        if chat_id not in cache.settings: cache.settings[chat_id] = {}
        cache.settings[chat_id][key] = value

    def add_link_whitelist(self, chat_id, user_id):
        self.execute("INSERT OR IGNORE INTO link_whitelist(chat_id, user_id) VALUES(?,?)", (int(chat_id), int(user_id)), commit=True)
        cache.link_whitelist[int(chat_id)].add(int(user_id))

    def remove_link_whitelist(self, chat_id, user_id):
        self.execute("DELETE FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        if int(chat_id) in cache.link_whitelist:
            cache.link_whitelist[int(chat_id)].discard(int(user_id))

    def add_global_blacklist(self, user_id, type_name="ban"):
        self.execute("INSERT OR REPLACE INTO global_blacklist(user_id, type, created_at) VALUES(?,?,?)", (int(user_id), type_name, int(time.time())), commit=True)
        cache.global_blacklist.add(int(user_id))

    def remove_global_blacklist(self, user_id):
        self.execute("DELETE FROM global_blacklist WHERE user_id=?", (int(user_id),), commit=True)
        cache.global_blacklist.discard(int(user_id))

    def add_shadow_ban(self, user_id):
        self.execute("INSERT OR REPLACE INTO shadow_ban(user_id, created_at) VALUES(?,?)", (int(user_id), int(time.time())), commit=True)
        cache.shadow_ban.add(int(user_id))

    def remove_shadow_ban(self, user_id):
        self.execute("DELETE FROM shadow_ban WHERE user_id=?", (int(user_id),), commit=True)
        cache.shadow_ban.discard(int(user_id))

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
def error_handler(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try: return await func(update, context, *args, **kwargs)
        except Exception as e:
            if "Message is not modified" in str(e): return
            logger.error(f"Erro em {func.__name__}: {e}")
    return wrapper

def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID]

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat: return False
    if is_owner(user.id): return True
    if chat.type == ChatType.PRIVATE: return True
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except: return False

def parse_time(time_str):
    if not time_str: return None
    match = re.match(r"(\d+)([smhd])", time_str.lower())
    if not match: return None
    val, unit = int(match.group(1)), match.group(2)
    if unit == 's': return timedelta(seconds=val)
    if unit == 'm': return timedelta(minutes=val)
    if unit == 'h': return timedelta(hours=val)
    if unit == 'd': return timedelta(days=val)
    return None

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
    await update.message.reply_text("🛡️ <b>Jtzin Administrator V1.3</b>\n<i>Performance Extrema Ativada.</i>", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_id(update, context):
    target_id = get_target(update)
    if not target_id: target_id = update.effective_user.id
    await update.message.reply_text(f"🆔 ID: <code>{target_id}</code>", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_allban(update, context):
    if not is_owner(update.effective_user.id): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'ban')
    chats = db.all_chats()
    for row in chats:
        try: await context.bot.ban_chat_member(row['chat_id'], target_id)
        except: continue
    await update.message.reply_text(f"✅ {target_id} banido de todos os grupos.")

@error_handler
async def cmd_allblack(update, context):
    if not is_owner(update.effective_user.id): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'black')
    await update.message.reply_text(f"✅ {target_id} em blacklist global.")

@error_handler
async def cmd_unblacklist(update, context):
    if not is_owner(update.effective_user.id): return
    target_id = get_target(update)
    if not target_id: return
    db.remove_global_blacklist(target_id)
    await update.message.reply_text(f"✅ {target_id} removido da blacklist.")

@error_handler
async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    t = parse_time(context.args[1]) if len(context.args) > 1 else None
    await context.bot.ban_chat_member(update.effective_chat.id, target_id, until_date=datetime.now() + t if t else None)
    await update.message.reply_text(f"🚫 Banido{' por ' + context.args[1] if t else ''}.")

@error_handler
async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    t = parse_time(context.args[1]) if len(context.args) > 1 else timedelta(hours=24)
    await context.bot.restrict_chat_member(update.effective_chat.id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=datetime.now() + t)
    await update.message.reply_text(f"🔇 Mutado por {context.args[1] if len(context.args) > 1 else '24h'}.")

@error_handler
async def cmd_shadow(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_shadow_ban(target_id)
    await update.message.reply_text("🌑 Shadow Ban Ativado.")

@error_handler
async def cmd_unshadow(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return
    db.remove_shadow_ban(target_id)
    await update.message.reply_text("✅ Shadow Ban Removido.")

@error_handler
async def cmd_purge(update, context):
    if not await is_admin(update, context): return
    msg = update.effective_message
    amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
    if not amount and msg.reply_to_message:
        amount = msg.message_id - msg.reply_to_message.message_id
    if not amount: return
    amount = min(amount, 100)
    await msg.delete()
    tasks = [context.bot.delete_message(msg.chat_id, msg.message_id - i - 1) for i in range(amount)]
    await asyncio.gather(*tasks, return_exceptions=True)

@error_handler
async def cmd_msg(update, context):
    if not is_owner(update.effective_user.id): return
    msg = update.effective_message
    chats = db.active_chats()
    sent = 0
    caption = " ".join(context.args)
    for row in chats:
        try:
            if msg.reply_to_message:
                await context.bot.copy_message(row['chat_id'], msg.chat_id, msg.reply_to_message.message_id, caption=caption or None)
            else:
                if caption: await context.bot.send_message(row['chat_id'], caption)
            sent += 1
            await asyncio.sleep(0.1)
        except: continue
    await msg.reply_text(f"📢 Transmissão: {sent} chats.")

@error_handler
async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    cid = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(cid, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(cid, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")],
        [InlineKeyboardButton(f"Captcha: {'✅' if db.get_setting(cid, 'captcha_enabled', 0) else '❌'}", callback_data="toggle_captcha")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

# --- HANDLERS ---
@error_handler
async def message_handler(update, context):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot: return

    # Cache Check (Velocidade de Luz)
    if user.id in cache.global_blacklist or user.id in cache.shadow_ban:
        try: await msg.delete()
        except: pass
        return

    db.register_chat(msg.chat_id, msg.chat.title, msg.chat.type)
    db.remember_user(user)

    if is_owner(user.id): return
    
    # Anti-Link (Cache Check)
    if db.get_setting(msg.chat_id, "antilink") and any(e.type in ["url", "text_link"] for e in msg.entities or []):
        if user.id not in cache.link_whitelist[msg.chat_id]:
            try: await msg.delete()
            except: pass

@error_handler
async def on_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context): return
    cid = query.message.chat_id
    data = query.data
    if data == "toggle_antispam": db.set_setting(cid, "antispam", 1 - db.get_setting(cid, "antispam", 1))
    elif data == "toggle_antilink": db.set_setting(cid, "antilink", 1 - db.get_setting(cid, "antilink", 0))
    elif data == "toggle_captcha": db.set_setting(cid, "captcha_enabled", 1 - db.get_setting(cid, "captcha_enabled", 0))
    await cmd_settings(update, context)

async def post_init(app: Application):
    cache.load_all(db.conn)
    await app.bot.set_my_commands([
        BotCommand("start", "Iniciar"), BotCommand("id", "Ver ID"), BotCommand("settings", "Configurações"),
        BotCommand("lock", "Fechar"), BotCommand("unlock", "Abrir"), BotCommand("purge", "Limpar"),
        BotCommand("ban", "Banir"), BotCommand("mute", "Silenciar"), BotCommand("msg", "Transmissão")
    ])
    logger.info("Jtzin Administrator V1.3 ONLINE!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("msg", cmd_msg))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("allban", cmd_allban))
    app.add_handler(CommandHandler("allblack", cmd_allblack))
    app.add_handler(CommandHandler("blacklist", cmd_allblack))
    app.add_handler(CommandHandler("unblacklist", cmd_unblacklist))
    app.add_handler(CommandHandler("shadow", cmd_shadow))
    app.add_handler(CommandHandler("unshadow", cmd_unshadow))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
