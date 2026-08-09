import logging
import os
import re
import sqlite3
import time
import asyncio
from collections import defaultdict, deque
from pathlib import Path
from datetime import timedelta

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
                welcome_enabled INTEGER NOT NULL DEFAULT 0
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
                (chat_id, title or "", chat_type, 1, int(time.time())),
            )
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro ao registrar chat: {e}")

    def get_setting(self, chat_id, key, default=0):
        try:
            row = self.conn.execute(f"SELECT {key} FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
            if row and row[key] is not None:
                return row[key]
            return default
        except sqlite3.OperationalError:
            logger.warning(f"Coluna {key} não encontrada no banco. Rode migrate_db.py")
            return default

    def set_setting(self, chat_id, key, value):
        try:
            self.conn.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, chat_id))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro ao salvar setting {key}: {e}")

    def remember_user(self, user):
        if not user: return
        username = (user.username or "").lower().lstrip("@") or None
        self.conn.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user.id, username, user.first_name or ""),
        )
        self.conn.commit()

    def resolve_username(self, username):
        username = username.lower().lstrip("@")
        row = self.conn.execute("SELECT user_id FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
        return int(row["user_id"]) if row else None

    def add_blacklist(self, chat_id, user_id, username, added_by):
        self.conn.execute(
            "INSERT OR REPLACE INTO blacklist(chat_id,user_id,username,added_by,created_at) VALUES(?,?,?,?,?)",
            (chat_id, user_id, username or "", added_by, int(time.time())),
        )
        self.conn.commit()

    def is_blacklisted(self, chat_id, user_id):
        row = self.conn.execute("SELECT 1 FROM blacklist WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return row is not None

    def add_warning(self, chat_id, user_id):
        self.conn.execute(
            "INSERT INTO warnings(chat_id,user_id,count) VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1",
            (chat_id, user_id),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return int(row["count"])

    def active_chats(self):
        return self.conn.execute("SELECT chat_id,title FROM chats WHERE active=1").fetchall()

db = Database(DB_PATH)

# --- AUXILIARES ---
async def send_log(chat_id, text, context):
    log_channel = db.get_setting(chat_id, "log_channel", None)
    if log_channel:
        try:
            await context.bot.send_message(log_channel, f"📝 <b>LOG:</b>\n{text}", parse_mode=ParseMode.HTML)
        except: pass

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

async def is_owner(update: Update) -> bool:
    user = update.effective_user
    if not user: return False
    return user.id in [OWNER_ID, SECOND_OWNER_ID]

async def safe_delete(message):
    try: await message.delete(); return True
    except: return False

def target_from_update(update: Update):
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
    return None

# --- COMANDOS ---
async def cmd_start(update, context):
    keyboard = [
        [InlineKeyboardButton("📚 Ajuda", callback_data="help_main")],
        [InlineKeyboardButton("🆔 Meu ID", callback_data="my_id")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🛡️ <b>MTH ADMIN BOT V2</b>\n\nOlá! Sou seu assistente de moderação avançado.",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def cmd_help(update, context):
    text = (
        "🛡️ <b>MENU DE AJUDA</b>\n\n"
        "<b>Moderação:</b> /ban, /mute, /kick, /warn, /purge\n"
        "<b>Segurança:</b> /antispam, /antilink, /antiraid\n"
        "<b>Config:</b> /settings, /id\n"
        "<b>Dono:</b> /chats"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def cmd_purge(update, context):
    if not await is_admin(update, context): return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Responda à mensagem de onde deseja iniciar a limpeza.")
        return
    
    chat_id = update.effective_chat.id
    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    
    count = 0
    for m_id in range(end_id, start_id - 1, -1):
        try:
            await context.bot.delete_message(chat_id, m_id)
            count += 1
        except: continue
    
    status = await context.bot.send_message(chat_id, f"🧹 {count} mensagens limpas!")
    await asyncio.sleep(3)
    await safe_delete(status)

async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target:
        await update.message.reply_text("Uso: /mute @user ou responda a uma mensagem.")
        return
    
    uid = target.id if hasattr(target, 'id') else target
    try:
        until = timedelta(hours=24)
        await context.bot.restrict_chat_member(
            update.effective_chat.id, uid, 
            permissions=ChatPermissions(can_send_messages=False),
            until_date=update.message.date + until
        )
        await update.message.reply_text(f"🔇 Usuário silenciado por 24h.")
        await send_log(update.effective_chat.id, f"Mute: {uid} por {update.effective_user.id}", context)
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    
    antispam = "✅" if db.get_setting(chat_id, "antispam", 1) else "❌"
    antilink = "✅" if db.get_setting(chat_id, "antilink", 0) else "❌"
    antiraid = "✅" if db.get_setting(chat_id, "antiraid", 1) else "❌"
    
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {antispam}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {antilink}", callback_data="toggle_antilink")],
        [InlineKeyboardButton(f"Anti-Raid: {antiraid}", callback_data="toggle_antiraid")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES DO GRUPO</b>", 
                                   parse_mode=ParseMode.HTML, 
                                   reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_chats(update, context):
    if not await is_owner(update): return
    rows = db.active_chats()
    if not rows:
        await update.message.reply_text("Nenhum chat registrado.")
        return
    lines = ["📡 <b>CHATS REGISTRADOS:</b>"]
    for row in rows:
        lines.append(f"• {row['title']} — <code>{row['chat_id']}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_broadcast(update, context):
    if not await is_owner(update): return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Use: /divulgar texto")
        return
    rows = db.active_chats()
    sent = 0
    for row in rows:
        try:
            await context.bot.send_message(row["chat_id"], text)
            sent += 1
            await asyncio.sleep(0.5)
        except: continue
    await update.message.reply_text(f"📢 Divulgação concluída: {sent} chats.")

# --- HANDLERS DE EVENTOS ---
async def on_callback(update, context):
    query = update.callback_query
    if not query: return
    
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if not await is_admin(update, context) and data.startswith("toggle_"):
        return

    if data == "help_main":
        await query.edit_message_text("Use /help para ver todos os comandos disponíveis.")
    elif data == "my_id":
        await query.message.reply_text(f"🆔 Seu ID: <code>{query.from_user.id}</code>", parse_mode=ParseMode.HTML)
    elif data == "toggle_antispam":
        current = db.get_setting(chat_id, "antispam", 1)
        db.set_setting(chat_id, "antispam", 0 if current else 1)
        await cmd_settings(update, context)
    elif data == "toggle_antilink":
        current = db.get_setting(chat_id, "antilink", 0)
        db.set_setting(chat_id, "antilink", 0 if current else 1)
        await cmd_settings(update, context)
    elif data == "toggle_antiraid":
        current = db.get_setting(chat_id, "antiraid", 1)
        db.set_setting(chat_id, "antiraid", 0 if current else 1)
        await cmd_settings(update, context)

async def message_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot: return

    db.register_chat(chat.id, chat.title, chat.type)
    db.remember_user(user)

    if await is_admin(update, context): return

    # 1. Anti-Link
    if db.get_setting(chat.id, "antilink", 0):
        if any(entity.type in ["url", "text_link"] for entity in msg.entities or []):
            await safe_delete(msg)
            return

    # 2. Anti-Spam
    if db.get_setting(chat.id, "antispam", 1):
        now = time.monotonic()
        bucket = spam_buckets[(chat.id, user.id)]
        while bucket and now - bucket[0] > SPAM_WINDOW: bucket.popleft()
        bucket.append(now)
        if len(bucket) > SPAM_LIMIT:
            await safe_delete(msg)
            return

async def on_join(update, context):
    chat = update.effective_chat
    if not update.message or not update.message.new_chat_members: return
    
    for user in update.message.new_chat_members:
        if user.is_bot: continue
        if db.get_setting(chat.id, "antiraid", 1):
            now = time.monotonic()
            bucket = join_buckets[chat.id]
            while bucket and now - bucket[0] > RAID_WINDOW: bucket.popleft()
            bucket.append(now)
            if len(bucket) > RAID_LIMIT:
                try: await context.bot.ban_chat_member(chat.id, user.id)
                except: pass
                continue
        if db.get_setting(chat.id, "welcome_enabled", 0):
            text = db.get_setting(chat.id, "welcome_text", "Bem-vindo ao grupo!")
            try: await chat.send_message(text.replace("{name}", user.first_name))
            except: pass

# --- MAIN ---
async def post_init(app: Application):
    commands = [
        BotCommand("start", "Iniciar o bot"),
        BotCommand("help", "Menu de ajuda"),
        BotCommand("settings", "Configurações do grupo"),
        BotCommand("purge", "Limpar mensagens"),
        BotCommand("mute", "Silenciar usuário"),
        BotCommand("ban", "Banir usuário"),
        BotCommand("warn", "Dar advertência"),
        BotCommand("id", "Ver ID"),
    ]
    await app.bot.set_my_commands(commands)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("divulgar", cmd_broadcast))
    app.add_handler(CommandHandler("id", lambda u, c: u.message.reply_text(f"🆔 Seu ID: {u.effective_user.id}")))
    
    app.add_handler(CallbackQueryHandler(on_callback))
    
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
