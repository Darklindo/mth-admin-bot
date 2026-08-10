import logging
import os
import sqlite3
import time
import asyncio
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import ChatAdminRights, ChannelParticipantsAdmins, Channel, User
from telethon.errors import RPCError, FloodWaitError

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
logger = logging.getLogger("jtzin-telethon")

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
            logger.info("Cache carregado com sucesso (V2.7).")
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

    def add_local_blacklist(self, chat_id, user_id, reason=None):
        self.execute("INSERT OR REPLACE INTO local_blacklist(chat_id, user_id, reason, created_at) VALUES(?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time())), commit=True)
        cache.local_blacklist[int(chat_id)].add(int(user_id))

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

    def all_chats_detailed(self):
        return self.execute("SELECT chat_id, title, chat_type, active FROM chats").fetchall()

    def remember_user(self, user_id, username, first_name):
        if not user_id: return
        username = (username or "").lower().lstrip("@") or None
        self.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (int(user_id), username, first_name or ""),
            commit=True
        )

db = Database(DB_PATH)

# --- CLIENTE TELETHON ---
client = TelegramClient("jtzin_session", API_ID, API_HASH)

def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID]

def is_authorized(user_id: int) -> bool:
    return is_owner(user_id) or user_id in cache.authorized_users

async def get_target_from_event(event):
    # Tenta pegar da resposta (Reply)
    reply = await event.get_reply_message()
    if reply:
        if reply.sender_id:
            return reply.sender_id
        if reply.forward:
            return reply.forward.sender_id
    
    # Tenta pegar do texto (ID ou Username)
    args = event.raw_text.split()
    if len(args) > 1:
        raw = args[1].strip()
        if raw.startswith("@"):
            try:
                user = await client.get_entity(raw)
                return user.id
            except:
                return db.resolve_username(raw)
        if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
            return int(raw)
    return None

def get_reason_from_event(event):
    args = event.raw_text.split()
    if event.is_reply:
        return " ".join(args[1:]) if len(args) > 1 else None
    return " ".join(args[2:]) if len(args) > 2 else None

async def reply_or_edit(event, text):
    try:
        if event.out:
            return await event.edit(text, parse_mode='html')
    except:
        pass
    return await event.reply(text, parse_mode='html')

# --- FILTRO DE SEGURANÇA GLOBAL ---
@client.on(events.NewMessage(incoming=True))
async def global_security_filter(event):
    if not event.is_group and not event.is_channel: return
    
    sender = await event.get_sender()
    user_id = event.sender_id
    if not user_id: return
    
    # Registrar chat e usuário
    try:
        chat = await event.get_chat()
        db.register_chat(event.chat_id, getattr(chat, 'title', 'Chat'), chat.__class__.__name__)
        if sender:
            db.remember_user(user_id, getattr(sender, 'username', None), getattr(sender, 'first_name', ''))
    except: pass

    if is_owner(user_id): return

    is_banned_global = user_id in cache.global_blacklist
    is_shadow = user_id in cache.shadow_ban
    is_local_black = user_id in cache.local_blacklist[event.chat_id]
    is_local_banperm = user_id in cache.local_banperm[event.chat_id]

    if is_banned_global or is_shadow or is_local_black or is_local_banperm:
        try:
            await event.delete()
            if is_banned_global or is_local_banperm:
                await client.edit_permissions(event.chat_id, user_id, view_messages=False)
        except: pass
        raise events.StopPropagation

    # Anti-Link
    if db.get_setting(event.chat_id, "antilink") and event.text:
        if any(x in event.text.lower() for x in ["http://", "https://", "t.me/"]):
            if user_id not in cache.link_whitelist[event.chat_id]:
                try:
                    perms = await client.get_permissions(event.chat_id, user_id)
                    if not perms.is_admin and not perms.is_creator:
                        await event.delete()
                        raise events.StopPropagation
                except:
                    await event.delete()
                    raise events.StopPropagation

# --- COMANDOS DO TELETHON ---

@client.on(events.NewMessage(pattern=r'^\.start', incoming=True, outgoing=True))
async def cmd_start(event):
    if not is_authorized(event.sender_id): return
    logger.info(f"Comando recebido: .start de {event.sender_id}")
    text = (
        "🛡️ <b>Jtzin Userbot V2.7 (Telethon)</b>\n\n"
        "Userbot de administração avançado operando com estabilidade máxima.\n"
        "Equipe Diamond — Segurança total."
    )
    await reply_or_edit(event, text)

@client.on(events.NewMessage(pattern=r'^\.autorizar', incoming=True, outgoing=True))
async def cmd_autorizar(event):
    if not is_owner(event.sender_id): return
    logger.info(f"Comando recebido: .autorizar de {event.sender_id}")
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário (respondendo ou ID/Username).")
        return
    db.add_authorized(target_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ Usuário {user_info} (<code>{target_id}</code>) autorizado a usar o Userbot.")

@client.on(events.NewMessage(pattern=r'^\.help', incoming=True, outgoing=True))
async def cmd_help(event):
    if not is_authorized(event.sender_id): return
    logger.info(f"Comando recebido: .help de {event.sender_id}")
    text = (
        "📖 <b>GUIA DE COMANDOS — Jtzin Userbot V2.7</b>\n\n"
        "🛡️ <b>MODERAÇÃO:</b>\n"
        "• <code>.banperm</code> - Bane permanentemente do grupo.\n"
        "• <code>.blacklist</code> - Apaga mensagens do usuário no grupo.\n"
        "• <code>.ban</code> - Bane temporariamente.\n"
        "• <code>.mute</code> - Silencia o usuário.\n"
        "• <code>.shadow</code> - Shadow ban.\n"
        "• <code>.unshadow</code> - Remove Shadow ban.\n\n"
        "👑 <b>CONTROLE DE ACESSO:</b>\n"
        "• <code>.autorizar</code> - Autoriza usuário a usar o bot.\n"
        "• <code>.allban / .allblack</code> - Exclusivo Donos.\n"
        "• <code>.msg</code> - Transmissão global (Donos).\n"
        "• <code>.chats</code> - Relatório de chats (Donos)."
    )
    await reply_or_edit(event, text)

@client.on(events.NewMessage(pattern=r'^\.id', incoming=True, outgoing=True))
async def cmd_id(event):
    if not is_authorized(event.sender_id): return
    logger.info(f"Comando recebido: .id de {event.sender_id}")
    target_id = await get_target_from_event(event) or event.sender_id
    await reply_or_edit(event, f"🆔 ID: <code>{target_id}</code>")

@client.on(events.NewMessage(pattern=r'^\.banperm', incoming=True, outgoing=True))
async def cmd_banperm(event):
    if not is_authorized(event.sender_id): return
    logger.info(f"Comando recebido: .banperm de {event.sender_id}")
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id): return
    db.add_local_banperm(event.chat_id, target_id, get_reason_from_event(event))
    try: await client.edit_permissions(event.chat_id, target_id, view_messages=False)
    except: pass
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) banido permanentemente deste grupo.")

@client.on(events.NewMessage(pattern=r'^\.blacklist', incoming=True, outgoing=True))
async def cmd_blacklist(event):
    if not is_authorized(event.sender_id): return
    logger.info(f"Comando recebido: .blacklist de {event.sender_id}")
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id): return
    db.add_local_blacklist(event.chat_id, target_id, get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist local.")

@client.on(events.NewMessage(pattern=r'^\.allban', incoming=True, outgoing=True))
async def cmd_allban(event):
    if not is_owner(event.sender_id): return
    logger.info(f"Comando recebido: .allban de {event.sender_id}")
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'ban', get_reason_from_event(event))
    chats = db.all_chats_detailed()
    for chat in chats:
        if chat['chat_type'] not in ['private', 'User']:
            try:
                await client.edit_permissions(chat['chat_id'], target_id, view_messages=False)
                await asyncio.sleep(0.1)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except: continue
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"☢️ {user_info} (<code>{target_id}</code>) BANIDO GLOBALMENTE.")

@client.on(events.NewMessage(pattern=r'^\.allblack', incoming=True, outgoing=True))
async def cmd_allblack(event):
    if not is_owner(event.sender_id): return
    logger.info(f"Comando recebido: .allblack de {event.sender_id}")
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id): return
    db.add_global_blacklist(target_id, 'black', get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist global.")

@client.on(events.NewMessage(pattern=r'^\.listdn', incoming=True, outgoing=True))
async def cmd_listdn(event):
    if not is_owner(event.sender_id): return
    logger.info(f"Comando recebido: .listdn de {event.sender_id}")
    shadow, glob = db.get_all_banned_list_detailed()
    text = "📋 <b>LISTA DE PUNIÇÕES GLOBAIS</b>\n\n"
    
    if shadow:
        text += "🌑 <b>Shadow Ban:</b>\n"
        for r in shadow:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {r['reason']}" if r['reason'] else ""
            text += f"• {info} (<code>{r['user_id']}</code>){reason}\n└ 📅 {datetime.fromtimestamp(r['created_at']).strftime('%d/%m/%Y %H:%M')}\n"
        text += "\n"

    if glob:
        text += "🌎 <b>Global Blacklist:</b>\n"
        for r in glob:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {r['reason']}" if r['reason'] else ""
            text += f"• {info} (<code>{r['user_id']}</code>) [{r['type'].upper()}]{reason}\n└ 📅 {datetime.fromtimestamp(r['created_at']).strftime('%d/%m/%Y %H:%M')}\n"
    
    if not shadow and not glob:
        text += "Nenhuma punição global registrada."
        
    await reply_or_edit(event, text)

@client.on(events.NewMessage(pattern=r'^\.chats', incoming=True, outgoing=True))
async def cmd_chats(event):
    if not is_owner(event.sender_id): return
    logger.info(f"Comando recebido: .chats de {event.sender_id}")
    rows = db.all_chats_detailed()
    grupos, canais, privados = [], [], []
    
    for r in rows:
        status = "✅" if r['active'] else "❌"
        chat_info = f"{status} {r['title']} (<code>{r['chat_id']}</code>)"
        if r['chat_type'] in ['group', 'supergroup', 'Chat']: grupos.append(chat_info)
        elif r['chat_type'] in ['channel', 'Channel']: canais.append(chat_info)
        elif r['chat_type'] in ['private', 'User']:
            user_info = db.get_user_info(r['chat_id'])
            privados.append(f"{status} {user_info} (<code>{r['chat_id']}</code>)")

    text = "📡 <b>RELATÓRIO DE CHATS</b>\n\n"
    if grupos: text += "👥 <b>GRUPOS:</b>\n" + "\n".join(grupos) + "\n\n"
    if canais: text += "📣 <b>CANAIS:</b>\n" + "\n".join(canais) + "\n\n"
    if privados: text += "👤 <b>USUÁRIOS NO PRIVADO:</b>\n" + "\n".join(privados) + "\n\n"
    
    ativos_msg = sum(1 for r in rows if r['active'] and r['chat_type'] not in ['private', 'User'])
    text += "📊 <b>RESUMO:</b>\n"
    text += f"• Grupos/Canais: {len(grupos) + len(canais)}\n"
    text += f"• Ativos p/ Msg: {ativos_msg}\n"
    text += f"• Usuários no Privado: {len(privados)}"
    await reply_or_edit(event, text)

# --- INICIALIZAÇÃO ---
if __name__ == "__main__":
    cache.load_all(db.conn)
    logger.info("JTZIN USERBOT V2.7 (TELETHON) INICIANDO...")
    client.start()
    logger.info("USERBOT TELETHON ONLINE E OPERACIONAL!")
    client.run_until_disconnected()
