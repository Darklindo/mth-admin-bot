import logging
import os
import re
import sqlite3
import time
import asyncio
import sys
import functools
from collections import defaultdict
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
        self.local_blacklist = defaultdict(set)
        self.local_banperm = defaultdict(set)
        self.shadow_ban = set()
        self.link_whitelist = defaultdict(set)
        self.settings = {}

    def load_all(self, db_conn):
        try:
            # Global
            cursor = db_conn.execute("SELECT user_id FROM global_blacklist")
            self.global_blacklist = {row[0] for row in cursor.fetchall()}
            
            # Local Blacklist
            cursor = db_conn.execute("SELECT chat_id, user_id FROM local_blacklist")
            for row in cursor.fetchall():
                self.local_blacklist[row[0]].add(row[1])
                
            # Local BanPerm
            cursor = db_conn.execute("SELECT chat_id, user_id FROM local_banperm")
            for row in cursor.fetchall():
                self.local_banperm[row[0]].add(row[1])

            cursor = db_conn.execute("SELECT user_id FROM shadow_ban")
            self.shadow_ban = {row[0] for row in cursor.fetchall()}
            
            cursor = db_conn.execute("SELECT chat_id, user_id FROM link_whitelist")
            for row in cursor.fetchall():
                self.link_whitelist[row[0]].add(row[1])
                
            cursor = db_conn.execute("SELECT chat_id, antispam, antilink, captcha_enabled FROM settings")
            for row in cursor.fetchall():
                self.settings[row[0]] = {
                    "antispam": row[1], "antilink": row[2], "captcha_enabled": row[3]
                }
        except Exception as e:
            logger.error(f"Erro ao carregar cache: {e}")

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

    # Punições Locais
    def add_local_banperm(self, chat_id, user_id, reason=None):
        self.execute("INSERT OR REPLACE INTO local_banperm(chat_id, user_id, reason, created_at) VALUES(?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time())), commit=True)
        cache.local_banperm[int(chat_id)].add(int(user_id))

    def remove_local_banperm(self, chat_id, user_id):
        self.execute("DELETE FROM local_banperm WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        if int(chat_id) in cache.local_banperm: cache.local_banperm[int(chat_id)].discard(int(user_id))

    def add_local_blacklist(self, chat_id, user_id, reason=None):
        self.execute("INSERT OR REPLACE INTO local_blacklist(chat_id, user_id, reason, created_at) VALUES(?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time())), commit=True)
        cache.local_blacklist[int(chat_id)].add(int(user_id))

    def remove_local_blacklist(self, chat_id, user_id):
        self.execute("DELETE FROM local_blacklist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        if int(chat_id) in cache.local_blacklist: cache.local_blacklist[int(chat_id)].discard(int(user_id))

    # Punições Globais
    def add_global_blacklist(self, user_id, type_name="ban", reason=None):
        self.execute("INSERT OR REPLACE INTO global_blacklist(user_id, type, reason, created_at) VALUES(?,?,?,?)", (int(user_id), type_name, reason, int(time.time())), commit=True)
        cache.global_blacklist.add(int(user_id))

    def remove_global_blacklist(self, user_id):
        self.execute("DELETE FROM global_blacklist WHERE user_id=?", (int(user_id),), commit=True)
        cache.global_blacklist.discard(int(user_id))

    def add_shadow_ban(self, user_id, reason=None):
        self.execute("INSERT OR REPLACE INTO shadow_ban(user_id, reason, created_at) VALUES(?,?,?)", (int(user_id), reason, int(time.time())), commit=True)
        cache.shadow_ban.add(int(user_id))

    def remove_shadow_ban(self, user_id):
        self.execute("DELETE FROM shadow_ban WHERE user_id=?", (int(user_id),), commit=True)
        cache.shadow_ban.discard(int(user_id))

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
        if int(chat_id) in cache.link_whitelist: cache.link_whitelist[int(chat_id)].discard(int(user_id))

    def resolve_username(self, username):
        username = username.lower().lstrip("@")
        row = self.execute("SELECT user_id FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
        return int(row["user_id"]) if row else None

    def get_user_info(self, user_id):
        row = self.execute("SELECT username, first_name FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if row: return f"@{row['username']}" if row['username'] else row['first_name']
        return str(user_id)

    def get_all_banned_list_detailed(self):
        shadow = self.execute("SELECT user_id, reason, created_at FROM shadow_ban ORDER BY created_at DESC").fetchall()
        glob = self.execute("SELECT user_id, type, reason, created_at FROM global_blacklist ORDER BY created_at DESC").fetchall()
        return shadow, glob

    def active_chats(self):
        return self.execute("SELECT chat_id FROM chats WHERE active=1 AND chat_type != 'private'").fetchall()

    def all_chats_detailed(self):
        return self.execute("SELECT chat_id, title, chat_type, active FROM chats").fetchall()

    def set_chat_active(self, chat_id, status):
        self.execute("UPDATE chats SET active=? WHERE chat_id=?", (status, int(chat_id)), commit=True)

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

def get_reason(update: Update):
    msg = update.effective_message
    args = msg.text.split()
    if msg.reply_to_message:
        return " ".join(args[1:]) if len(args) > 1 else None
    return " ".join(args[2:]) if len(args) > 2 else None

# --- FILTRO DE SEGURANÇA (PRIORIDADE MÁXIMA) ---
@error_handler
async def global_security_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot: return
    
    db.register_chat(msg.chat_id, msg.chat.title, msg.chat.type)
    db.remember_user(user)

    if is_owner(user.id): return

    # Verificação de Punições Globais e Locais
    is_banned = (
        user.id in cache.global_blacklist or 
        user.id in cache.shadow_ban or 
        user.id in cache.local_blacklist[msg.chat_id] or
        user.id in cache.local_banperm[msg.chat_id]
    )

    if is_banned:
        try:
            await msg.delete()
            if user.id in cache.global_blacklist or user.id in cache.local_banperm[msg.chat_id]:
                await context.bot.ban_chat_member(msg.chat_id, user.id)
        except: pass
        return True

    # Anti-Link
    if db.get_setting(msg.chat_id, "antilink") and any(e.type in ["url", "text_link"] for e in msg.entities or []):
        if user.id not in cache.link_whitelist[msg.chat_id]:
            try: await msg.delete()
            except: pass
            return True
    return False

# --- COMANDOS ---
@error_handler
async def cmd_start(update, context):
    text = (
        "🛡️ <b>Jtzin Administrator V1.4.1</b>\n\n"
        "O bot de administração definitivo para elevar o nível do seu grupo ou canal.\n\n"
        "💎 <b>Equipe Diamond</b> — <i>Excelência em Automação</i>\n\n"
        "🚀 <b>Recursos de Elite:</b>\n"
        "• Anti-Link e Anti-Spam inteligente\n"
        "• Punições Locais e Globais\n"
        "• Sistema de Shadow Ban\n"
        "• Moderação por Resposta e ID\n\n"
        "Use os botões abaixo para navegar."
    )
    keyboard = [
        [InlineKeyboardButton("💎 Canal Oficial Diamond", url="https://t.me/upadatesproxymodmenu")],
        [InlineKeyboardButton("🛡️ Adicionar ao Grupo!", url="https://t.me/Jtcaciadminbot?startgroup=true")],
        [InlineKeyboardButton("🐞 Feedbacks / Bugs", url="https://t.me/OnlyExaltarei")]
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

@error_handler
async def cmd_help(update, context):
    text = (
        "📖 <b>GUIA DE COMANDOS - Jtzin Admin</b>\n\n"
        "🛡️ <b>MODERAÇÃO LOCAL:</b>\n"
        "• <code>/banperm</code> - Bane permanentemente deste grupo.\n"
        "• <code>/blacklist</code> - Apaga mensagens deste usuário neste grupo.\n"
        "• <code>/ban</code> - Bane o usuário temporário.\n"
        "• <code>/mute</code> - Silencia o usuário.\n"
        "• <code>/purge</code> - Limpa mensagens.\n\n"
        "⚙️ <b>CONFIGURAÇÃO:</b>\n"
        "• <code>/settings</code> - Menu de configurações.\n"
        "• <code>/lock</code> / <code>/unlock</code> - Fecha/Abre grupo.\n\n"
        "🛠️ <b>UTILITÁRIOS:</b>\n"
        "• <code>/id</code> - Mostra o ID.\n"
        "<i>Comandos Nucleares são exclusivos para Donos.</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_id(update, context):
    target_id = get_target(update) or update.effective_user.id
    await update.message.reply_text(f"🆔 ID: <code>{target_id}</code>", parse_mode=ParseMode.HTML)

# PUNIÇÕES LOCAIS
@error_handler
async def cmd_banperm(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_local_banperm(update.effective_chat.id, target_id, get_reason(update))
    try: await context.bot.ban_chat_member(update.effective_chat.id, target_id)
    except: pass
    await update.message.reply_text(f"✅ {target_id} banido permanentemente deste grupo.")

@error_handler
async def cmd_blacklist(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_local_blacklist(update.effective_chat.id, target_id, get_reason(update))
    await update.message.reply_text(f"✅ {target_id} em blacklist local.")

# PUNIÇÕES GLOBAIS (SÓ DONOS)
@error_handler
async def cmd_allban(update, context):
    if not is_owner(update.effective_user.id): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'ban', get_reason(update))
    for chat in db.all_chats_detailed():
        if chat['chat_type'] != 'private':
            try: await context.bot.ban_chat_member(chat['chat_id'], target_id)
            except: continue
    await update.message.reply_text(f"☢️ {target_id} BANIDO GLOBALMENTE.")

@error_handler
async def cmd_allblack(update, context):
    if not is_owner(update.effective_user.id): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'black', get_reason(update))
    await update.message.reply_text(f"☢️ {target_id} em BLACKLIST GLOBAL.")

@error_handler
async def cmd_listdn(update, context):
    if not is_owner(update.effective_user.id): return
    shadow, glob = db.get_all_banned_list_detailed()
    text = "📋 <b>RELATÓRIO DE PUNIÇÕES GLOBAIS:</b>\n\n"
    for r in glob:
        text += f"• {db.get_user_info(r['user_id'])} (<code>{r['user_id']}</code>) [{r['type'].upper()}]\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# MODERAÇÃO COMUM
@error_handler
async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    await context.bot.ban_chat_member(update.effective_chat.id, target_id)
    await update.message.reply_text("🚫 Banido.")

@error_handler
async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    await context.bot.restrict_chat_member(update.effective_chat.id, target_id, permissions=ChatPermissions(can_send_messages=False))
    await update.message.reply_text("🔇 Mutado.")

@error_handler
async def cmd_purge(update, context):
    if not await is_admin(update, context): return
    msg = update.effective_message
    amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 0
    if not amount and msg.reply_to_message:
        amount = msg.message_id - msg.reply_to_message.message_id
    if not amount: return
    await msg.delete()
    for i in range(min(amount, 100)):
        try: await context.bot.delete_message(msg.chat_id, msg.message_id - i - 1)
        except: continue

@error_handler
async def cmd_msg(update, context):
    if not is_owner(update.effective_user.id): return
    msg = update.effective_message
    chats = db.active_chats()
    sent = 0
    for row in chats:
        try:
            if msg.reply_to_message:
                await context.bot.copy_message(row['chat_id'], msg.chat_id, msg.reply_to_message.message_id, caption=" ".join(context.args) or None)
            else:
                await context.bot.send_message(row['chat_id'], " ".join(context.args))
            sent += 1
            await asyncio.sleep(0.1)
        except: continue
    await msg.reply_text(f"📢 Transmissão: {sent} chats.")

@error_handler
async def cmd_chats(update, context):
    if not is_owner(update.effective_user.id): return
    rows = db.all_chats_detailed()
    text = "📡 <b>CHATS REGISTRADOS:</b>\n\n"
    for r in rows:
        if r['chat_type'] == 'private': continue
        status = "✅" if r['active'] else "❌"
        text += f"{status} {r['title']} (<code>{r['chat_id']}</code>)\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    cid = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(cid, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(cid, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES</b>", reply_markup=InlineKeyboardMarkup(keyboard))

@error_handler
async def on_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not await is_admin(update, context): return
    cid = query.message.chat_id
    if query.data == "toggle_antispam": db.set_setting(cid, "antispam", 1 - db.get_setting(cid, "antispam", 1))
    elif query.data == "toggle_antilink": db.set_setting(cid, "antilink", 1 - db.get_setting(cid, "antilink", 0))
    await cmd_settings(update, context)

async def post_init(app: Application):
    cache.load_all(db.conn)
    await app.bot.set_my_commands([
        BotCommand("start", "Iniciar"), BotCommand("help", "Ajuda"), BotCommand("id", "Ver ID"), 
        BotCommand("settings", "Configurações"), BotCommand("lock", "Fechar"), BotCommand("unlock", "Abrir"), 
        BotCommand("purge", "Limpar"), BotCommand("ban", "Banir"), BotCommand("mute", "Silenciar")
    ])
    logger.info("Jtzin Administrator V1.4.1 ONLINE!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.ALL, global_security_filter), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("banperm", cmd_banperm))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("allban", cmd_allban))
    app.add_handler(CommandHandler("allblack", cmd_allblack))
    app.add_handler(CommandHandler("listdn", cmd_listdn))
    app.add_handler(CommandHandler("msg", cmd_msg))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
