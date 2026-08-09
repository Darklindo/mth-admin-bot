import logging
import os
import re
import sqlite3
import time
import asyncio
import sys
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

# Configuração de Logs Silenciosa
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.WARNING,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger("mth-admin")
logger.setLevel(logging.INFO)

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
                    chat_type=excluded.chat_type
                """,
                (int(chat_id), title or "", chat_type, 1, int(time.time())),
            )
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (int(chat_id),))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro ao registrar chat: {e}")

    def set_chat_active(self, chat_id, active):
        try:
            self.conn.execute("UPDATE chats SET active=? WHERE chat_id=?", (int(active), int(chat_id)))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Erro ao mudar status do chat: {e}")
            return False

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

    def remove_link_whitelist(self, chat_id, user_id):
        try:
            self.conn.execute("DELETE FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Erro ao remover da whitelist: {e}")

    def is_link_whitelisted(self, chat_id, user_id):
        try:
            row = self.conn.execute("SELECT 1 FROM link_whitelist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id))).fetchone()
            return row is not None
        except:
            return False

    def active_chats_for_broadcast(self):
        try:
            return self.conn.execute("SELECT chat_id, title FROM chats WHERE active=1").fetchall()
        except:
            return []

    def active_chats_with_night_mode(self):
        try:
            return self.conn.execute("SELECT chat_id, night_start, night_end FROM settings WHERE night_mode_auto=1").fetchall()
        except:
            return []

    def resolve_username(self, username):
        try:
            username = username.lower().lstrip("@")
            row = self.conn.execute("SELECT user_id FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
            return int(row["user_id"]) if row else None
        except: return None

    def remember_user(self, user):
        if not user: return
        try:
            username = (user.username or "").lower().lstrip("@") or None
            self.conn.execute(
                "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
                (user.id, username, user.first_name or ""),
            )
            self.conn.commit()
        except: pass

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
        if re.fullmatch(r"-?\d+", raw): return int(raw)
    except: pass
    return None

# --- COMANDOS ---
async def cmd_start(update, context):
    keyboard = [[InlineKeyboardButton("📚 Ajuda", callback_data="help_main")]]
    await update.message.reply_text("🛡️ <b>MTH ADMIN BOT V2.2.4</b>\n\nMenu de comandos corrigido!", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

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

async def cmd_purge(update, context):
    if not await is_admin(update, context): return
    msg = update.effective_message
    chat_id = update.effective_chat.id
    
    if context.args and context.args[0].isdigit():
        amount = int(context.args[0])
        if amount > 100: amount = 100
        await safe_delete(msg)
        count = 0
        current_id = msg.message_id
        for i in range(amount):
            try:
                await context.bot.delete_message(chat_id, current_id - i - 1)
                count += 1
            except: continue
        status = await context.bot.send_message(chat_id, f"🧹 {count} mensagens limpas!")
        await asyncio.sleep(3)
        await safe_delete(status)
        return

    if msg.reply_to_message:
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
        return

    await msg.reply_text("Uso: /purge [quantidade] ou responda a uma mensagem.")

async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target:
        await update.message.reply_text("Uso: /ban @user ou responda a uma mensagem.")
        return
    uid = target.id if hasattr(target, 'id') else target
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text(f"🚫 Usuário banido.")
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_kick(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target:
        await update.message.reply_text("Uso: /kick @user ou responda a uma mensagem.")
        return
    uid = target.id if hasattr(target, 'id') else target
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid)
        await update.message.reply_text(f"👢 Usuário expulso.")
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

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
    except Exception as e:
        await update.message.reply_text(f"Erro: {e}")

async def cmd_warn(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target:
        await update.message.reply_text("Uso: /warn @user ou responda a uma mensagem.")
        return
    await update.message.reply_text(f"⚠️ Usuário advertido.")

async def cmd_allowlink(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target:
        await update.message.reply_text("Uso: /allowlink @user ou responda a uma mensagem.")
        return
    uid = target.id if hasattr(target, 'id') else target
    db.add_link_whitelist(update.effective_chat.id, uid)
    await update.message.reply_text("✅ Usuário agora pode enviar links!")

async def cmd_removelink(update, context):
    if not await is_admin(update, context): return
    target = target_from_update(update)
    if not target:
        await update.message.reply_text("Uso: /removelink @user ou responda a uma mensagem.")
        return
    uid = target.id if hasattr(target, 'id') else target
    db.remove_link_whitelist(update.effective_chat.id, uid)
    await update.message.reply_text("❌ Usuário não pode mais enviar links.")

async def cmd_rmdivulgar(update, context):
    if not await is_owner(update): return
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /rmdivulgar [ID do chat]")
        return
    chat_id = args[0]
    if db.set_chat_active(chat_id, 0):
        await update.message.reply_text(f"❌ Chat <code>{chat_id}</code> removido da divulgação.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Erro ao processar o ID.")

async def cmd_adddivulgar(update, context):
    if not await is_owner(update): return
    args = context.args
    if not args:
        await update.message.reply_text("Uso: /adddivulgar [ID do chat]")
        return
    chat_id = args[0]
    if db.set_chat_active(chat_id, 1):
        await update.message.reply_text(f"✅ Chat <code>{chat_id}</code> adicionado à divulgação.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("Erro ao processar o ID.")

async def cmd_broadcast(update, context):
    if not await is_owner(update): return
    text = update.effective_message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Use: /divulgar texto")
        return
    
    rows = db.active_chats_for_broadcast()
    sent = 0
    for row in rows:
        try:
            await context.bot.send_message(row["chat_id"], text)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Falha ao enviar para {row['chat_id']}: {e}")
            continue
    await update.message.reply_text(f"📢 Divulgação concluída: {sent} chats ativos receberam.")

async def cmd_chats(update, context):
    if not await is_owner(update): return
    try:
        rows = db.conn.execute("SELECT chat_id, title, active FROM chats").fetchall()
        if not rows:
            await update.message.reply_text("Nenhum chat registrado.")
            return
        lines = ["📡 <b>CHATS REGISTRADOS:</b>"]
        for row in rows:
            status = "✅" if row['active'] else "❌"
            lines.append(f"{status} {row['title']} — <code>{row['chat_id']}</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Erro ao listar chats: {e}")

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
                logger.info(f"Modo Noturno ativado em {chat_id}")
            elif now == row['night_end']:
                await context.bot.set_chat_permissions(chat_id, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True))
                logger.info(f"Modo Noturno desativado em {chat_id}")
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
        db.remember_user(user)
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
        try:
            await query.edit_message_text("⚙️ <b>CONFIGURAÇÕES</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise e
    except Exception as e:
        logger.error(f"Erro no callback: {e}")

async def post_init(app: Application):
    try:
        # COMANDOS QUE APARECERÃO NO MENU "/"
        commands = [
            BotCommand("start", "Iniciar o bot"),
            BotCommand("help", "Ver ajuda"),
            BotCommand("settings", "Configurações"),
            BotCommand("lock", "Fechar grupo"),
            BotCommand("unlock", "Abrir grupo"),
            BotCommand("purge", "Limpar mensagens"),
            BotCommand("ban", "Banir usuário"),
            BotCommand("kick", "Expulsar usuário"),
            BotCommand("mute", "Silenciar usuário"),
            BotCommand("warn", "Advertir usuário"),
            BotCommand("allowlink", "Autorizar links"),
            BotCommand("removelink", "Remover autorização"),
        ]
        await app.bot.set_my_commands(commands)
        if app.job_queue:
            app.job_queue.run_repeating(night_mode_checker, interval=60, first=10)
        logger.info("MTH ADMIN BOT V2.2.4 ONLINE!")
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
        
        app.add_handler(CallbackQueryHandler(on_callback))
        app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, message_handler))
        
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"ERRO FATAL NA INICIALIZAÇÃO: {e}")
        logger.critical(f"Erro fatal: {e}")

if __name__ == "__main__":
    main()
