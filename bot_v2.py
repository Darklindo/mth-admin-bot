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
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SECOND_OWNER_ID = 6466326477  # @MHZINTADEVOLTAPORRRRRRAAAA

if not BOT_TOKEN:
    print("ERRO: BOT_TOKEN não configurado no .env")
    sys.exit(1)
if not OWNER_ID:
    print("ERRO: OWNER_ID não configurado no .env")
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
join_buckets = defaultdict(deque)

# --- DECORADOR DE ERROS ---
def error_handler(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Forbidden:
            logger.warning(f"Sem permissão no chat {update.effective_chat.id if update.effective_chat else 'Unknown'}")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Erro de requisição: {e}")
        except RetryAfter as e:
            logger.warning(f"Flood limit atingido. Aguardando {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
        except (TimedOut, NetworkError):
            logger.error("Erro de rede/timeout. O bot tentará continuar.")
        except Exception as e:
            logger.error(f"Erro inesperado em {func.__name__}: {e}", exc_info=True)
            if update.effective_message:
                try:
                    await update.effective_message.reply_text("❌ Ocorreu um erro interno ao processar este comando.")
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
        self.conn.execute("PRAGMA foreign_keys=ON")

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
                        log_channel INTEGER,
                        welcome_text TEXT,
                        welcome_enabled INTEGER NOT NULL DEFAULT 0,
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
                        type TEXT NOT NULL, -- 'ban' ou 'black'
                        reason TEXT,
                        created_at INTEGER NOT NULL
                    );
                    """
                )
        except Exception as e:
            logger.error(f"Erro na inicialização do DB: {e}")

    def execute(self, query, params=(), commit=False):
        for attempt in range(3):
            try:
                cursor = self.conn.execute(query, params)
                if commit:
                    self.conn.commit()
                return cursor
            except sqlite3.OperationalError as e:
                if "locked" in str(e):
                    time.sleep(0.1)
                    continue
                raise e
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

    def add_link_whitelist(self, chat_id, user_id):
        self.execute("INSERT OR IGNORE INTO link_whitelist(chat_id, user_id) VALUES(?,?)", (int(chat_id), int(user_id)), commit=True)

    def remove_link_whitelist(self, chat_id, user_id):
        self.execute("DELETE FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)

    def is_link_whitelisted(self, chat_id, user_id):
        row = self.execute("SELECT 1 FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
        return row is not None

    def add_global_blacklist(self, user_id, type_name, reason=""):
        self.execute(
            "INSERT INTO global_blacklist(user_id, type, reason, created_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET type=excluded.type, reason=excluded.reason",
            (int(user_id), type_name, reason, int(time.time())),
            commit=True
        )

    def get_global_status(self, user_id):
        row = self.execute("SELECT type, reason FROM global_blacklist WHERE user_id=?", (int(user_id),)).fetchone()
        return row if row else None

    def active_chats_for_broadcast(self):
        return self.execute("SELECT chat_id, title FROM chats WHERE active=1").fetchall()

    def all_chats(self):
        return self.execute("SELECT chat_id FROM chats").fetchall()

    def active_chats_with_night_mode(self):
        return self.execute("SELECT chat_id, night_start, night_end FROM settings WHERE night_mode_auto=1").fetchall()

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

async def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user.id in [OWNER_ID, SECOND_OWNER_ID] if user else False

async def is_primary_owner(update: Update) -> bool:
    user = update.effective_user
    return user.id == OWNER_ID if user else False

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
    if re.fullmatch(r"-?\d+", raw): return int(raw)
    return None

# --- COMANDOS ---
@error_handler
async def cmd_start(update, context):
    keyboard = [[InlineKeyboardButton("📚 Ajuda", callback_data="help_main")]]
    await update.message.reply_text("🛡️ <b>MTH ADMIN BOT V3.1</b>\n\nComandos nucleares ativados!", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

@error_handler
async def cmd_help(update, context):
    text = (
        "🛡️ <b>MENU DE AJUDA</b>\n\n"
        "<b>Moderação:</b> /ban, /mute, /kick, /warn, /purge\n"
        "<b>Links:</b> /allowlink, /removelink\n"
        "<b>Controle:</b> /lock, /unlock, /settings\n"
        "<b>Dono:</b> /chats"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_allban(update, context):
    if not await is_primary_owner(update): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /allban @user ou ID [motivo]")
    
    uid = target.id if hasattr(target, 'id') else target
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Sem motivo especificado."
    
    db.add_global_blacklist(uid, 'ban', reason)
    
    chats = db.all_chats()
    success = 0
    for chat in chats:
        try:
            await context.bot.ban_chat_member(chat['chat_id'], uid)
            success += 1
            await asyncio.sleep(0.1)
        except: continue
        
    await update.message.reply_text(f"☢️ <b>BANIMENTO NUCLEAR APLICADO!</b>\n\nUsuário: <code>{uid}</code>\nChats afetados: {success}\nMotivo: {reason}", parse_mode=ParseMode.HTML)

@error_handler
async def cmd_allblack(update, context):
    if not await is_primary_owner(update): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /allblack @user ou ID")
    
    uid = target.id if hasattr(target, 'id') else target
    db.add_global_blacklist(uid, 'black', "Blacklist Global")
    
    await update.message.reply_text(f"🌑 <b>SILENCIAMENTO NUCLEAR APLICADO!</b>\n\nUsuário: <code>{uid}</code>\nTodas as mensagens deste usuário serão apagadas em qualquer grupo.", parse_mode=ParseMode.HTML)

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
    chat_id = update.effective_chat.id
    
    amount = 0
    if context.args and context.args[0].isdigit():
        amount = min(int(context.args[0]), 100)
        start_id = msg.message_id
    elif msg.reply_to_message:
        start_id = msg.message_id
        amount = msg.message_id - msg.reply_to_message.message_id
        amount = min(amount, 100)
    else:
        await msg.reply_text("Uso: /purge [quantidade] ou responda a uma mensagem.")
        return

    await safe_delete(msg)
    count = 0
    for i in range(amount):
        try:
            await context.bot.delete_message(chat_id, start_id - i - 1)
            count += 1
        except: continue
    
    status = await context.bot.send_message(chat_id, f"🧹 {count} mensagens limpas!")
    await asyncio.sleep(3)
    await safe_delete(status)

@error_handler
async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /ban @user ou responda.")
    uid = target.id if hasattr(target, 'id') else target
    await context.bot.ban_chat_member(update.effective_chat.id, uid)
    await update.message.reply_text(f"🚫 Usuário banido.")

@error_handler
async def cmd_kick(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /kick @user ou responda.")
    uid = target.id if hasattr(target, 'id') else target
    await context.bot.unban_chat_member(update.effective_chat.id, uid)
    await update.message.reply_text(f"👢 Usuário expulso.")

@error_handler
async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /mute @user ou responda.")
    uid = target.id if hasattr(target, 'id') else target
    until = timedelta(hours=24)
    await context.bot.restrict_chat_member(update.effective_chat.id, uid, permissions=ChatPermissions(can_send_messages=False), until_date=update.message.date + until)
    await update.message.reply_text(f"🔇 Usuário silenciado por 24h.")

@error_handler
async def cmd_warn(update, context):
    if not await is_admin(update, context): return
    await update.message.reply_text(f"⚠️ Usuário advertido.")

@error_handler
async def cmd_allowlink(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /allowlink @user ou responda.")
    uid = target.id if hasattr(target, 'id') else target
    db.add_link_whitelist(update.effective_chat.id, uid)
    await update.message.reply_text("✅ Usuário autorizado a enviar links!")

@error_handler
async def cmd_removelink(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target: return await update.message.reply_text("Uso: /removelink @user ou responda.")
    uid = target.id if hasattr(target, 'id') else target
    db.remove_link_whitelist(update.effective_chat.id, uid)
    await update.message.reply_text("❌ Autorização de links removida.")

@error_handler
async def cmd_rmdivulgar(update, context):
    if not await is_owner(update): return
    if not context.args: return await update.message.reply_text("Uso: /rmdivulgar [ID]")
    if db.set_chat_active(context.args[0], 0):
        await update.message.reply_text(f"❌ Chat {context.args[0]} removido da divulgação.")

@error_handler
async def cmd_adddivulgar(update, context):
    if not await is_owner(update): return
    if not context.args: return await update.message.reply_text("Uso: /adddivulgar [ID]")
    if db.set_chat_active(context.args[0], 1):
        await update.message.reply_text(f"✅ Chat {context.args[0]} adicionado à divulgação.")

@error_handler
async def cmd_broadcast(update, context):
    if not await is_owner(update): return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text: return await update.message.reply_text("Use: /divulgar texto")
    
    rows = db.active_chats_for_broadcast()
    sent = 0
    for row in rows:
        try:
            await context.bot.send_message(row["chat_id"], text)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception: continue
    await update.message.reply_text(f"📢 Divulgação concluída: {sent} chats ativos.")

@error_handler
async def cmd_chats(update, context):
    if not await is_owner(update): return
    rows = db.execute("SELECT chat_id, title, active FROM chats").fetchall()
    if not rows: return await update.message.reply_text("Nenhum chat registrado.")
    lines = ["📡 <b>CHATS REGISTRADOS:</b>"]
    for row in rows:
        status = "✅" if row['active'] else "❌"
        lines.append(f"{status} {row['title']} — <code>{row['chat_id']}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

@error_handler
async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    chat_id = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(chat_id, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(chat_id, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")],
        [InlineKeyboardButton(f"Anti-Raid: {'✅' if db.get_setting(chat_id, 'antiraid', 1) else '❌'}", callback_data="toggle_antiraid")],
        [InlineKeyboardButton(f"Modo Noturno Auto: {'✅' if db.get_setting(chat_id, 'night_mode_auto', 0) else '❌'}", callback_data="toggle_night")]
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
            elif now == row['night_end']:
                perms = ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True)
                await context.bot.set_chat_permissions(chat_id, perms)
    except Exception as e:
        logger.error(f"Erro no checker noturno: {e}")

# --- HANDLERS ---
@error_handler
async def message_handler(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot: return

    db.register_chat(chat.id, chat.title, chat.type)
    db.remember_user(user)
    
    # Check Global Blacklist
    global_status = db.get_global_status(user.id)
    if global_status:
        if global_status['type'] == 'ban':
            try: await context.bot.ban_chat_member(chat.id, user.id)
            except: pass
            await safe_delete(msg)
            return
        elif global_status['type'] == 'black':
            await safe_delete(msg)
            return

    if await is_admin(update, context): return

    if db.get_setting(chat.id, "antilink", 0):
        if any(entity.type in ["url", "text_link"] for entity in msg.entities or []):
            if not db.is_link_whitelisted(chat.id, user.id):
                return await safe_delete(msg)

    if db.get_setting(chat.id, "antispam", 1):
        now = time.monotonic()
        bucket = spam_buckets[(chat.id, user.id)]
        while bucket and now - bucket[0] > SPAM_WINDOW: bucket.popleft()
        bucket.append(now)
        if len(bucket) > SPAM_LIMIT:
            return await safe_delete(msg)

@error_handler
async def on_callback(update, context):
    query = update.callback_query
    if not query: return
    await query.answer()
    chat_id = query.message.chat_id
    if not await is_admin(update, context): return

    if query.data == "toggle_antispam": db.set_setting(chat_id, "antispam", 0 if db.get_setting(chat_id, "antispam", 1) else 1)
    elif query.data == "toggle_antilink": db.set_setting(chat_id, "antilink", 0 if db.get_setting(chat_id, "antilink", 0) else 1)
    elif query.data == "toggle_antiraid": db.set_setting(chat_id, "antiraid", 0 if db.get_setting(chat_id, "antiraid", 1) else 1)
    elif query.data == "toggle_night": db.set_setting(chat_id, "night_mode_auto", 0 if db.get_setting(chat_id, "night_mode_auto", 0) else 1)
    
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(chat_id, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(chat_id, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")],
        [InlineKeyboardButton(f"Anti-Raid: {'✅' if db.get_setting(chat_id, 'antiraid', 1) else '❌'}", callback_data="toggle_antiraid")],
        [InlineKeyboardButton(f"Modo Noturno Auto: {'✅' if db.get_setting(chat_id, 'night_mode_auto', 0) else '❌'}", callback_data="toggle_night")]
    ]
    await query.edit_message_text("⚙️ <b>CONFIGURAÇÕES</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def post_init(app: Application):
    commands = [
        BotCommand("start", "Iniciar"), BotCommand("help", "Ajuda"),
        BotCommand("settings", "Configurações"), BotCommand("lock", "Fechar"),
        BotCommand("unlock", "Abrir"), BotCommand("purge", "Limpar"),
        BotCommand("ban", "Banir"), BotCommand("kick", "Expulsar"),
        BotCommand("mute", "Silenciar"), BotCommand("warn", "Advertir"),
        BotCommand("allowlink", "Autorizar links"), BotCommand("removelink", "Bloquear links")
    ]
    await app.bot.set_my_commands(commands)
    if app.job_queue:
        app.job_queue.run_repeating(night_mode_checker, interval=60, first=10)
    logger.info("MTH ADMIN BOT V3.1 ONLINE!")

def main():
    try:
        app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("settings", cmd_settings))
        app.add_handler(CommandHandler("lock", cmd_lock))
        app.add_handler(CommandHandler("unlock", cmd_unlock))
        app.add_handler(CommandHandler("purge", cmd_purge))
        app.add_handler(CommandHandler("ban", cmd_ban))
        app.add_handler(CommandHandler("kick", cmd_kick))
        app.add_handler(CommandHandler("mute", cmd_mute))
        app.add_handler(CommandHandler("warn", cmd_warn))
        app.add_handler(CommandHandler("rmdivulgar", cmd_rmdivulgar))
        app.add_handler(CommandHandler("adddivulgar", cmd_adddivulgar))
        app.add_handler(CommandHandler("divulgar", cmd_broadcast))
        app.add_handler(CommandHandler("chats", cmd_chats))
        app.add_handler(CommandHandler("allowlink", cmd_allowlink))
        app.add_handler(CommandHandler("removelink", cmd_removelink))
        app.add_handler(CommandHandler("allban", cmd_allban))
        app.add_handler(CommandHandler("allblack", cmd_allblack))
        app.add_handler(CallbackQueryHandler(on_callback))
        app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.critical(f"Erro fatal: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
