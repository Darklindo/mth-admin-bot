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
    ApplicationHandlerStop,
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

# Configuração de Logs - Reduzido para focar em erros e uso importante
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("mth-admin")
logger.setLevel(logging.INFO)

# --- CACHE EM MEMÓRIA ---
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
            cursor = db_conn.execute("SELECT user_id FROM global_blacklist")
            self.global_blacklist = {row[0] for row in cursor.fetchall()}
            
            cursor = db_conn.execute("SELECT chat_id, user_id FROM local_blacklist")
            for row in cursor.fetchall():
                self.local_blacklist[row[0]].add(row[1])
                
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
            logger.info("Cache de performance carregado.")
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
            logger.error(f"DB Error: {e} | Query: {query}")
            return None

    def register_chat(self, chat_id, title, chat_type):
        self.execute(
            "INSERT INTO chats(chat_id,title,chat_type,active,created_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, chat_type=excluded.chat_type",
            (int(chat_id), title or "", str(chat_type), 1, int(time.time())),
            commit=True
        )
        if int(chat_id) not in cache.settings:
            self.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (int(chat_id),), commit=True)
            cache.settings[int(chat_id)] = {"antispam": 1, "antilink": 0, "captcha_enabled": 0}

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
        except ApplicationHandlerStop: raise
        except Exception as e:
            if "Message is not modified" in str(e): return
            logger.error(f"Erro em {func.__name__}: {e}")
            if update.effective_message:
                try: await update.effective_message.reply_text(f"❌ Erro interno: {str(e)[:100]}")
                except: pass
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

def format_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d/%m/%Y %H:%M')

# --- FILTRO DE SEGURANÇA (BLOQUEIO TOTAL DE BANNED) ---
@error_handler
async def global_security_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot: return
    
    db.register_chat(msg.chat_id, msg.chat.title, msg.chat.type)
    db.remember_user(user)

    if is_owner(user.id): return

    # Verificação de Punições
    is_banned_global = user.id in cache.global_blacklist
    is_shadow = user.id in cache.shadow_ban
    is_local_black = user.id in cache.local_blacklist[msg.chat_id]
    is_local_banperm = user.id in cache.local_banperm[msg.chat_id]

    if is_banned_global or is_shadow or is_local_black or is_local_banperm:
        try:
            await msg.delete()
            # Se for ban global ou banperm local, tenta banir se ainda não estiver
            if is_banned_global or is_local_banperm:
                await context.bot.ban_chat_member(msg.chat_id, user.id)
        except: pass
        raise ApplicationHandlerStop() # Impede que comandos e outros handlers rodem

    # Anti-Link (Bloqueia comandos com link também)
    if db.get_setting(msg.chat_id, "antilink") and any(e.type in ["url", "text_link"] for e in msg.entities or []):
        if user.id not in cache.link_whitelist[msg.chat_id]:
            if not await is_admin(update, context):
                try: await msg.delete()
                except: pass
                raise ApplicationHandlerStop()
    
    return

# --- COMANDOS ---
@error_handler
async def cmd_start(update, context):
    text = (
        "🛡️ <b>Jtzin Administrator V1</b>\n\n"
        "Bot de administração avançado para seus grupos e canais, sempre conte com a equipe Diamond.\n\n"
        "Use os botões abaixo para navegar."
    )
    keyboard = [
        [InlineKeyboardButton("💎 Canal Oficial Diamond", url="https://t.me/upadatesproxymodmenu")],
        [InlineKeyboardButton("🛡️ Coloque o bot no seu canal ou Grupo!", url="https://t.me/Jtcaciadminbot?startgroup=true")],
        [InlineKeyboardButton("🐞 Feedbacks / Bugs", url="https://t.me/OnlyExaltarei")]
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

@error_handler
async def cmd_help(update, context):
    text = (
        "📖 <b>GUIA DE COMANDOS — Jtzin Administrator V1</b>\n\n"
        "🛡️ <b>MODERAÇÃO LOCAL:</b>\n"
        "• <code>/banperm [id/@/resposta]</code> - Bane permanentemente deste grupo.\n"
        "• <code>/blacklist [id/@/resposta]</code> - Coloca na blacklist local (apaga mensagens).\n"
        "• <code>/ban [id/@/resposta]</code> - Bane temporariamente do grupo.\n"
        "• <code>/mute [id/@/resposta]</code> - Silencia o usuário no grupo.\n"
        "• <code>/purge [qtd/resposta]</code> - Limpa mensagens em massa.\n"
        "• <code>/shadow [id/@/resposta]</code> - Aplica Shadow Ban no usuário.\n"
        "• <code>/unshadow [id/@/resposta]</code> - Remove Shadow Ban.\n\n"
        "🔗 <b>GERENCIAMENTO DE LINKS:</b>\n"
        "• <code>/allowlink [id/@/resposta]</code> - Autoriza o usuário a mandar links.\n"
        "• <code>/removelink [id/@/resposta]</code> - Remove autorização de links.\n\n"
        "⚙️ <b>CONFIGURAÇÃO & CONTROLE:</b>\n"
        "• <code>/settings</code> - Menu interativo de configurações.\n"
        "• <code>/lock</code> - Fecha o chat (impede envio de mensagens).\n"
        "• <code>/unlock</code> - Abre o chat.\n\n"
        "🛠️ <b>UTILITÁRIOS:</b>\n"
        "• <code>/id [id/@/resposta]</code> - Mostra o ID do usuário.\n"
        "• <code>/listdn</code> - Lista todas as punições e bans globais.\n\n"
        "👑 <i>Nota: Comandos globais (/allban, /allblack, /msg, /chats) são exclusivos para os Donos e omitidos por segurança.</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_id(update, context):
    target_id = get_target(update) or update.effective_user.id
    await update.message.reply_text(f"🆔 ID: <code>{target_id}</code>", parse_mode=ParseMode.HTML)

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
    await update.message.reply_text(f"✅ {target_id} em blacklist global.")

@error_handler
async def cmd_unblacklist(update, context):
    if not is_owner(update.effective_user.id): return
    target_id = get_target(update)
    if not target_id: return
    db.remove_global_blacklist(target_id)
    await update.message.reply_text(f"✅ {target_id} removido da blacklist global.")

@error_handler
async def cmd_listdn(update, context):
    if not is_owner(update.effective_user.id): return
    shadow, glob = db.get_all_banned_list_detailed()
    text = "📋 <b>LISTA DE PUNIÇÕES GLOBAIS</b>\n\n"
    
    if shadow:
        text += "🌑 <b>Shadow Ban:</b>\n"
        for r in shadow:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {r['reason']}" if r['reason'] else ""
            text += f"• {info} (<code>{r['user_id']}</code>){reason}\n└ 📅 {format_date(r['created_at'])}\n"
        text += "\n"

    if glob:
        text += "🌎 <b>Global Blacklist:</b>\n"
        for r in glob:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {r['reason']}" if r['reason'] else ""
            text += f"• {info} (<code>{r['user_id']}</code>) [{r['type'].upper()}]{reason}\n└ 📅 {format_date(r['created_at'])}\n"
    
    if not shadow and not glob:
        text += "Nenhuma punição global registrada."
        
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_ban(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target_id)
        await update.message.reply_text("🚫 Usuário banido.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao banir: {e}")

@error_handler
async def cmd_mute(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, target_id, permissions=ChatPermissions(can_send_messages=False))
        await update.message.reply_text("🔇 Usuário mutado.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao mutar: {e}")

@error_handler
async def cmd_shadow(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id or is_owner(target_id): return
    db.add_shadow_ban(target_id, get_reason(update))
    await update.message.reply_text("🌑 Shadow Ban ativado.")

@error_handler
async def cmd_unshadow(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return
    db.remove_shadow_ban(target_id)
    await update.message.reply_text("✅ Shadow Ban removido.")

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
    await msg.reply_text(f"📢 Transmissão concluída: {sent} chats.")

@error_handler
async def cmd_chats(update, context):
    if not is_owner(update.effective_user.id): return
    rows = db.all_chats_detailed()
    
    grupos = []
    canais = []
    privados = []
    
    for r in rows:
        status = "✅" if r['active'] else "❌"
        chat_info = f"{status} {r['title']} (<code>{r['chat_id']}</code>)"
        
        if r['chat_type'] in ['group', 'supergroup']:
            grupos.append(chat_info)
        elif r['chat_type'] == 'channel':
            canais.append(chat_info)
        elif r['chat_type'] == 'private':
            user_info = db.get_user_info(r['chat_id'])
            privados.append(f"{status} {user_info} (<code>{r['chat_id']}</code>)")

    text = "📡 <b>RELATÓRIO DE CHATS</b>\n\n"
    
    if grupos:
        text += "👥 <b>GRUPOS:</b>\n" + "\n".join(grupos) + "\n\n"
    
    if canais:
        text += "📣 <b>CANAIS:</b>\n" + "\n".join(canais) + "\n\n"
        
    if privados:
        text += "👤 <b>USUÁRIOS NO PRIVADO:</b>\n" + "\n".join(privados) + "\n\n"
        
    ativos_msg = sum(1 for r in rows if r['active'] and r['chat_type'] != 'private')
    
    text += "📊 <b>RESUMO:</b>\n"
    text += f"• Grupos/Canais: {len(grupos) + len(canais)}\n"
    text += f"• Ativos p/ Msg: {ativos_msg}\n"
    text += f"• Usuários no Privado: {len(privados)}"
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

@error_handler
async def cmd_adddivulgar(update, context):
    if not is_owner(update.effective_user.id): return
    target = context.args[0] if context.args else None
    if not target: return
    db.set_chat_active(target, 1)
    await update.message.reply_text(f"✅ Chat {target} ativado para divulgação.")

@error_handler
async def cmd_rmdivulgar(update, context):
    if not is_owner(update.effective_user.id): return
    target = context.args[0] if context.args else None
    if not target: return
    db.set_chat_active(target, 0)
    await update.message.reply_text(f"❌ Chat {target} removido da divulgação.")

@error_handler
async def cmd_allowlink(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return
    db.add_link_whitelist(update.effective_chat.id, target_id)
    await update.message.reply_text(f"✅ {target_id} autorizado a enviar links.")

@error_handler
async def cmd_removelink(update, context):
    if not await is_admin(update, context): return
    target_id = get_target(update)
    if not target_id: return
    db.remove_link_whitelist(update.effective_chat.id, target_id)
    await update.message.reply_text(f"❌ {target_id} desautorizado a enviar links.")

@error_handler
async def cmd_lock(update, context):
    if not await is_admin(update, context): return
    try:
        await context.bot.set_chat_permissions(update.effective_chat.id, ChatPermissions(can_send_messages=False))
        await update.message.reply_text("🔒 Grupo Fechado.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao fechar: {e}")

@error_handler
async def cmd_unlock(update, context):
    if not await is_admin(update, context): return
    try:
        perms = ChatPermissions(
            can_send_messages=True, 
            can_send_audios=True, 
            can_send_documents=True, 
            can_send_photos=True, 
            can_send_videos=True, 
            can_send_other_messages=True, 
            can_add_web_page_previews=True
        )
        await context.bot.set_chat_permissions(update.effective_chat.id, perms)
        await update.message.reply_text("🔓 Grupo Aberto.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao abrir: {e}")

@error_handler
async def cmd_settings(update, context):
    if not await is_admin(update, context): return
    cid = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(cid, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(cid, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")]
    ]
    await update.message.reply_text("⚙️ <b>CONFIGURAÇÕES DO CHAT</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

@error_handler
async def on_callback(update, context):
    query = update.callback_query
    if not await is_admin(update, context):
        await query.answer("Apenas administradores!", show_alert=True)
        return
    
    await query.answer()
    cid = query.message.chat_id
    if query.data == "toggle_antispam":
        new_val = 1 - db.get_setting(cid, "antispam", 1)
        db.set_setting(cid, "antispam", new_val)
    elif query.data == "toggle_antilink":
        new_val = 1 - db.get_setting(cid, "antilink", 0)
        db.set_setting(cid, "antilink", new_val)
    
    # Atualiza o menu
    keyboard = [
        [InlineKeyboardButton(f"Anti-Spam: {'✅' if db.get_setting(cid, 'antispam', 1) else '❌'}", callback_data="toggle_antispam")],
        [InlineKeyboardButton(f"Anti-Link: {'✅' if db.get_setting(cid, 'antilink', 0) else '❌'}", callback_data="toggle_antilink")]
    ]
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

async def post_init(app: Application):
    cache.load_all(db.conn)
    await app.bot.set_my_commands([
        BotCommand("start", "Iniciar bot"),
        BotCommand("help", "Guia de comandos"),
        BotCommand("id", "Ver ID"),
        BotCommand("settings", "Configurações"),
        BotCommand("lock", "Fechar grupo"),
        BotCommand("unlock", "Abrir grupo"),
        BotCommand("purge", "Limpar mensagens"),
        BotCommand("ban", "Banir temporário"),
        BotCommand("banperm", "Banir permanente"),
        BotCommand("blacklist", "Blacklist local"),
        BotCommand("mute", "Silenciar"),
        BotCommand("shadow", "Shadow ban"),
        BotCommand("unshadow", "Remover shadow ban"),
        BotCommand("allowlink", "Permitir link"),
        BotCommand("removelink", "Remover link"),
        BotCommand("listdn", "Listar punições")
    ])
    logger.info("MTH ADMIN BOT V1.4.4 ONLINE!")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Filtro de Segurança em primeiro lugar no grupo 0 para bloquear tudo de banned
    app.add_handler(MessageHandler(filters.ALL, global_security_filter), group=0)
    
    # Comandos em seguida (ainda no grupo 0)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("banperm", cmd_banperm))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("allban", cmd_allban))
    app.add_handler(CommandHandler("allblack", cmd_allblack))
    app.add_handler(CommandHandler("unblacklist", cmd_unblacklist))
    app.add_handler(CommandHandler("listdn", cmd_listdn))
    app.add_handler(CommandHandler("msg", cmd_msg))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("adddivulgar", cmd_adddivulgar))
    app.add_handler(CommandHandler("rmdivulgar", cmd_rmdivulgar))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("purge", cmd_purge))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("shadow", cmd_shadow))
    app.add_handler(CommandHandler("unshadow", cmd_unshadow))
    app.add_handler(CommandHandler("allowlink", cmd_allowlink))
    app.add_handler(CommandHandler("removelink", cmd_removelink))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("unlock", cmd_unlock))
    
    app.add_handler(CallbackQueryHandler(on_callback))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
