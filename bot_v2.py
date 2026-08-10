import logging
import os
import sqlite3
import time
import asyncio
import sys
import functools
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message, ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatType, ParseMode
from pyrogram.errors import RPCError

# --- CONFIGURAÇÕES INICIAIS ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("API_ID", "35026133"))
API_HASH = os.getenv("API_HASH", "f7a36b06a16942a3c7f2514f26a844b5")
OWNER_ID = int(os.getenv("OWNER_ID", "6822870889"))
SECOND_OWNER_ID = 6466326477

DB_PATH = DATA_DIR / "bot.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("jtzin-userbot")

# --- CACHE EM MEMÓRIA ---
class Cache:
    def __init__(self):
        self.global_blacklist = set()
        self.local_blacklist = defaultdict(set)
        self.local_banperm = defaultdict(set)
        self.shadow_ban = set()
        self.link_whitelist = defaultdict(set)
        self.authorized_users = set()
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

            cursor = db_conn.execute("SELECT user_id FROM authorized_users")
            self.authorized_users = {row[0] for row in cursor.fetchall()}
            
            cursor = db_conn.execute("SELECT chat_id, antispam, antilink, captcha_enabled FROM settings")
            for row in cursor.fetchall():
                self.settings[row[0]] = {
                    "antispam": row[1], "antilink": row[2], "captcha_enabled": row[3]
                }
            logger.info("Cache de performance do Userbot carregado com sucesso.")
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

    def add_authorized(self, user_id):
        self.execute("INSERT OR IGNORE INTO authorized_users(user_id, created_at) VALUES(?,?)", (int(user_id), int(time.time())), commit=True)
        cache.authorized_users.add(int(user_id))

    def remove_authorized(self, user_id):
        self.execute("DELETE FROM authorized_users WHERE user_id=?", (int(user_id),), commit=True)
        cache.authorized_users.discard(int(user_id))

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

# --- INICIALIZAÇÃO DO USERBOT ---
app = Client(
    "jtzin_userbot",
    api_id=API_ID,
    api_hash=API_HASH
)

# --- AUXILIARES ---
def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID]

def is_authorized(user_id: int) -> bool:
    return is_owner(user_id) or user_id in cache.authorized_users

async def get_target(client: Client, message: Message):
    if not message: return None
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    args = message.command
    if len(args) > 1:
        raw = args[1].strip()
        if raw.startswith("@"):
            try:
                user = await client.get_users(raw)
                return user.id
            except:
                return db.resolve_username(raw)
        if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
            return int(raw)
    return None

def get_reason(message: Message):
    args = message.command
    if message.reply_to_message:
        return " ".join(args[1:]) if len(args) > 1 else None
    return " ".join(args[2:]) if len(args) > 2 else None

def format_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d/%m/%Y %H:%M')

# --- FILTRO DE SEGURANÇA GLOBAL ---
@app.on_message(~filters.me & filters.group, group=-1)
async def global_security_filter(client: Client, message: Message):
    if not message.from_user: return
    user_id = message.from_user.id
    chat_id = message.chat.id

    db.register_chat(chat_id, message.chat.title, message.chat.type.value)
    db.remember_user(message.from_user)

    if is_owner(user_id): return

    is_banned_global = user_id in cache.global_blacklist
    is_shadow = user_id in cache.shadow_ban
    is_local_black = user_id in cache.local_blacklist[chat_id]
    is_local_banperm = user_id in cache.local_banperm[chat_id]

    if is_banned_global or is_shadow or is_local_black or is_local_banperm:
        try:
            await message.delete()
            if is_banned_global or is_local_banperm:
                await client.ban_chat_member(chat_id, user_id)
        except: pass
        message.stop_propagation()

    # Anti-Link
    if db.get_setting(chat_id, "antilink") and message.entities:
        has_link = any(e.type.name in ["URL", "TEXT_LINK"] for e in message.entities)
        if has_link and user_id not in cache.link_whitelist[chat_id]:
            # Verificar se é admin
            try:
                member = await client.get_chat_member(chat_id, user_id)
                if member.status.name not in ["ADMINISTRATOR", "CREATOR"]:
                    await message.delete()
                    message.stop_propagation()
            except:
                await message.delete()
                message.stop_propagation()

# --- COMANDOS DO USERBOT ---

async def reply_or_edit(message: Message, text: str, parse_mode=ParseMode.HTML):
    if message.from_user and message.from_user.is_self:
        try: return await message.edit_text(text, parse_mode=parse_mode)
        except: pass
    return await message.reply_text(text, parse_mode=parse_mode)

@app.on_message(filters.command("start") & (filters.me | filters.user(list(cache.authorized_users))))
async def cmd_start(client: Client, message: Message):
    text = (
        "🛡️ <b>Jtzin Userbot V1</b>\n\n"
        "Userbot de administração avançado operando diretamente na sua conta.\n"
        "Equipe Diamond — Segurança máxima."
    )
    await reply_or_edit(message, text)

@app.on_message(filters.command("autorizar") & (filters.me | filters.user([OWNER_ID, SECOND_OWNER_ID])))
async def cmd_autorizar(client: Client, message: Message):
    target_id = await get_target(client, message)
    if not target_id:
        await reply_or_edit(message, "❌ Especifique o usuário (respondendo ou ID/Username).")
        return
    db.add_authorized(target_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(message, f"✅ Usuário {user_info} (<code>{target_id}</code>) autorizado a usar o Userbot.")

@app.on_message(filters.command("help") & (filters.me | filters.user(list(cache.authorized_users) + [OWNER_ID, SECOND_OWNER_ID])))
async def cmd_help(client: Client, message: Message):
    text = (
        "📖 <b>GUIA DE COMANDOS — Jtzin Userbot V1</b>\n\n"
        "🛡️ <b>MODERAÇÃO:</b>\n"
        "• <code>/banperm</code> - Bane permanentemente do grupo.\n"
        "• <code>/blacklist</code> - Apaga mensagens do usuário no grupo.\n"
        "• <code>/ban</code> - Bane temporariamente.\n"
        "• <code>/mute</code> - Silencia o usuário.\n"
        "• <code>/shadow</code> - Shadow ban.\n"
        "• <code>/unshadow</code> - Remove Shadow ban.\n\n"
        "👑 <b>CONTROLE DE ACESSO:</b>\n"
        "• <code>/autorizar</code> - Autoriza usuário a usar o bot.\n"
        "• <code>/allban / allblack</code> - Exclusivo Donos.\n"
        "• <code>/msg</code> - Transmissão global (Donos).\n"
        "• <code>/chats</code> - Relatório de chats (Donos)."
    )
    await reply_or_edit(message, text)

@app.on_message(filters.command("id") & (filters.me | filters.user(list(cache.authorized_users))))
async def cmd_id(client: Client, message: Message):
    target_id = await get_target(client, message) or message.from_user.id
    await reply_or_edit(message, f"🆔 ID: <code>{target_id}</code>")

@app.on_message(filters.command("banperm") & (filters.me | filters.user(list(cache.authorized_users))))
async def cmd_banperm(client: Client, message: Message):
    target_id = await get_target(client, message)
    if not target_id or is_owner(target_id): return
    db.add_local_banperm(message.chat.id, target_id, get_reason(message))
    try: await client.ban_chat_member(message.chat.id, target_id)
    except: pass
    user_info = db.get_user_info(target_id)
    await reply_or_edit(message, f"✅ {user_info} (<code>{target_id}</code>) banido permanentemente deste grupo.")

@app.on_message(filters.command("blacklist") & (filters.me | filters.user(list(cache.authorized_users))))
async def cmd_blacklist(client: Client, message: Message):
    target_id = await get_target(client, message)
    if not target_id or is_owner(target_id): return
    db.add_local_blacklist(message.chat.id, target_id, get_reason(message))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(message, f"✅ {user_info} (<code>{target_id}</code>) em blacklist local.")

@app.on_message(filters.command("allban") & filters.user([OWNER_ID, SECOND_OWNER_ID]))
async def cmd_allban(client: Client, message: Message):
    target_id = await get_target(client, message)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'ban', get_reason(message))
    chats = db.all_chats_detailed()
    for chat in chats:
        if chat['chat_type'] != 'private':
            try:
                await client.ban_chat_member(chat['chat_id'], target_id)
                await asyncio.sleep(0.1)
            except: continue
    user_info = db.get_user_info(target_id)
    await reply_or_edit(message, f"☢️ {user_info} (<code>{target_id}</code>) BANIDO GLOBALMENTE.")

@app.on_message(filters.command("allblack") & filters.user([OWNER_ID, SECOND_OWNER_ID]))
async def cmd_allblack(client: Client, message: Message):
    target_id = await get_target(client, message)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'black', get_reason(message))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(message, f"✅ {user_info} (<code>{target_id}</code>) em blacklist global.")

@app.on_message(filters.command("listdn") & filters.user([OWNER_ID, SECOND_OWNER_ID]))
async def cmd_listdn(client: Client, message: Message):
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
        
    await reply_or_edit(message, text)

@app.on_message(filters.command("chats") & filters.user([OWNER_ID, SECOND_OWNER_ID]))
async def cmd_chats(client: Client, message: Message):
    rows = db.all_chats_detailed()
    grupos, canais, privados = [], [], []
    
    for r in rows:
        status = "✅" if r['active'] else "❌"
        chat_info = f"{status} {r['title']} (<code>{r['chat_id']}</code>)"
        if r['chat_type'] in ['group', 'supergroup']: grupos.append(chat_info)
        elif r['chat_type'] == 'channel': canais.append(chat_info)
        elif r['chat_type'] == 'private':
            user_info = db.get_user_info(r['chat_id'])
            privados.append(f"{status} {user_info} (<code>{r['chat_id']}</code>)")

    text = "📡 <b>RELATÓRIO DE CHATS</b>\n\n"
    if grupos: text += "👥 <b>GRUPOS:</b>\n" + "\n".join(grupos) + "\n\n"
    if canais: text += "📣 <b>CANAIS:</b>\n" + "\n".join(canais) + "\n\n"
    if privados: text += "👤 <b>USUÁRIOS NO PRIVADO:</b>\n" + "\n".join(privados) + "\n\n"
    
    ativos_msg = sum(1 for r in rows if r['active'] and r['chat_type'] != 'private')
    text += "📊 <b>RESUMO:</b>\n"
    text += f"• Grupos/Canais: {len(grupos) + len(canais)}\n"
    text += f"• Ativos p/ Msg: {ativos_msg}\n"
    text += f"• Usuários no Privado: {len(privados)}"
    await reply_or_edit(message, text)

# --- INICIALIZAÇÃO ---
async def start_userbot():
    cache.load_all(db.conn)
    logger.info("JTZIN USERBOT V2.2 INICIANDO (Compatibilidade Python 3.14)...")
    await app.start()
    logger.info("USERBOT ONLINE! Aguardando mensagens...")
    from pyrogram import idle
    await idle()
    await app.stop()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(start_userbot())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Erro fatal na execução: {e}")
