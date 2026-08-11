import logging
import os
import sqlite3
import time
import asyncio
from collections import defaultdict
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.tl.types import ChatAdminRights, ChannelParticipantsAdmins, Channel, User
from telethon.errors import RPCError, FloodWaitError, ChatAdminRequiredError, UserAdminInvalidError

# --- CONFIGURAÇÕES INICIAIS ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("API_ID", "35026133"))
API_HASH = os.getenv("API_HASH", "f7a36b06a16942a3c7f2514f26a844b5")
OWNER_ID = int(os.getenv("OWNER_ID", "6822870889"))
SECOND_OWNER_ID = 6466326477
THIRD_OWNER_ID = 7916427095

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
            
            try:
                cursor = db_conn.execute("SELECT chat_id, antispam, antilink, captcha_enabled FROM settings")
                for row in cursor.fetchall():
                    self.settings[row[0]] = {
                        "antispam": row[1], "antilink": row[2], "captcha_enabled": row[3]
                    }
            except sqlite3.OperationalError:
                logger.warning("Tabela settings incompleta no cache. Rode migrate_db.py.")

            logger.info("Cache carregado com sucesso (V4.3).")
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
        cache.local_banperm[int(chat_id)].discard(int(user_id))

    def add_local_blacklist(self, chat_id, user_id, reason=None):
        self.execute("INSERT OR REPLACE INTO local_blacklist(chat_id, user_id, reason, created_at) VALUES(?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time())), commit=True)
        cache.local_blacklist[int(chat_id)].add(int(user_id))

    def remove_local_blacklist(self, chat_id, user_id):
        self.execute("DELETE FROM local_blacklist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        cache.local_blacklist[int(chat_id)].discard(int(user_id))

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
        return [dict(r) for r in shadow], [dict(r) for r in glob]

    def all_chats_detailed(self):
        rows = self.execute("SELECT chat_id, title, chat_type, active FROM chats").fetchall()
        return [dict(r) for r in rows]

    def remember_user(self, user_id, username, first_name):
        if not user_id: return
        username = (username or "").lower().lstrip("@") or None
        self.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (int(user_id), username, first_name or ""),
            commit=True
        )

    def add_deleted_log(self, chat_id, user_id, content, reason, admin_id=None):
        res = self.execute(
            "INSERT INTO deleted_logs(chat_id, user_id, admin_id, content, reason, created_at) VALUES(?,?,?,?,?,?)",
            (int(chat_id), int(user_id), admin_id, content or "[Ação de Sistema]", reason, int(time.time())),
            commit=True
        )
        if res is None:
            self.execute(
                "INSERT INTO deleted_logs(chat_id, user_id, content, reason, created_at) VALUES(?,?,?,?,?)",
                (int(chat_id), int(user_id), content or "[Ação de Sistema]", reason, int(time.time())),
                commit=True
            )

    def get_latest_logs(self, limit=10):
        res = self.execute("SELECT * FROM deleted_logs ORDER BY created_at DESC LIMIT ?", (limit,))
        if res:
            rows = res.fetchall()
            return [dict(r) for r in rows]
        return []

db = Database(DB_PATH)

# --- CLIENTE TELETHON ---
client = TelegramClient("jtzin_session", API_ID, API_HASH)

def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID, THIRD_OWNER_ID]

def is_authorized(user_id: int) -> bool:
    return is_owner(user_id) or user_id in cache.authorized_users

async def get_target_from_event(event):
    try:
        reply = await event.get_reply_message()
        if reply:
            if reply.sender_id: return reply.sender_id
            if reply.forward: return reply.forward.sender_id
        
        args = event.raw_text.split()
        if len(args) > 1:
            raw = args[1].strip()
            if raw.startswith("@"):
                try:
                    user = await client.get_entity(raw)
                    return user.id
                except: return db.resolve_username(raw)
            if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
                return int(raw)
    except Exception as e:
        logger.error(f"Erro ao extrair alvo: {e}")
    return None

def get_reason_from_event(event):
    args = event.raw_text.split()
    if event.is_reply: return " ".join(args[1:]) if len(args) > 1 else None
    return " ".join(args[2:]) if len(args) > 2 else None

async def reply_or_edit(event, text, delete_after=2):
    try:
        msg = None
        if event.out:
            msg = await event.edit(text, parse_mode='html')
        else:
            msg = await event.reply(text, parse_mode='html')
        
        if delete_after and msg:
            await asyncio.sleep(delete_after)
            await msg.delete()
    except Exception as e:
        logger.error(f"Erro ao enviar/editar resposta: {e}")

# --- FILTRO DE SEGURANÇA GLOBAL ---
@client.on(events.NewMessage(incoming=True))
async def global_security_filter(event):
    if event.raw_text and event.raw_text.startswith("."): return
    if not event.is_group and not event.is_channel: return
    
    user_id = event.sender_id
    if not user_id or is_owner(user_id): return
    
    chat_id = event.chat_id
    reason = None
    
    if user_id in cache.global_blacklist: reason = "Global Blacklist"
    elif user_id in cache.shadow_ban: reason = "Shadow Ban"
    elif user_id in cache.local_blacklist[chat_id]: reason = "Local Blacklist"
    elif user_id in cache.local_banperm[chat_id]: reason = "Local BanPerm"

    if reason:
        try:
            db.add_deleted_log(chat_id, user_id, event.text, reason)
            await event.delete()
            if reason in ["Global Blacklist", "Local BanPerm"]:
                await client.edit_permissions(chat_id, user_id, view_messages=False)
        except: pass
        raise events.StopPropagation

    # Anti-Link
    if db.get_setting(chat_id, "antilink") and event.text:
        if any(x in event.text.lower() for x in ["http://", "https://", "t.me/"]):
            if user_id not in cache.link_whitelist[chat_id]:
                try:
                    perms = await client.get_permissions(chat_id, user_id)
                    if not perms.is_admin and not perms.is_creator:
                        db.add_deleted_log(chat_id, user_id, event.text, "Anti-Link")
                        await event.delete()
                        raise events.StopPropagation
                except:
                    db.add_deleted_log(chat_id, user_id, event.text, "Anti-Link")
                    await event.delete()
                    raise events.StopPropagation

# --- COMANDOS ---

@client.on(events.NewMessage(pattern=r'^\.start', func=lambda e: is_authorized(e.sender_id)))
async def cmd_start(event):
    text = "🛡️ <b>Jtzin Userbot V4.3 (Estabilidade Final)</b>\n\nEquipe Diamond — Blindado."
    await reply_or_edit(event, text, delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.unban', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unban(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=True, send_messages=True)
        db.remove_local_banperm(event.chat_id, target_id)
        db.remove_global_blacklist(target_id)
        db.remove_shadow_ban(target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Unban em Cascata", "Reversão", admin_id=event.sender_id)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) desbanido totalmente.", delete_after=2)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=2)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desbanir: {e}", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.unmute', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unmute(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, send_messages=True)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Unmute", "Reversão", admin_id=event.sender_id)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) pode falar novamente.", delete_after=2)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=2)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desmutar: {e}", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.unblacklist', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unblacklist(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    db.remove_local_blacklist(event.chat_id, target_id)
    db.remove_global_blacklist(target_id)
    db.remove_shadow_ban(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Unblacklist em Cascata", "Reversão", admin_id=event.sender_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) removido de todas as blacklists.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.unallblack', func=lambda e: is_owner(e.sender_id)))
async def cmd_unallblack(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    db.remove_global_blacklist(target_id)
    db.remove_shadow_ban(target_id)
    db.add_deleted_log(0, target_id, "Ação: Unallblack Global", "Reversão", admin_id=event.sender_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) removido da blacklist global.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.unbanperm', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unbanperm(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    db.remove_local_banperm(event.chat_id, target_id)
    db.remove_global_blacklist(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Unbanperm em Cascata", "Reversão", admin_id=event.sender_id)
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=True)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) totalmente perdoado.", delete_after=2)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=2)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desbanir: {e}", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.unshadow', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unshadow(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    db.remove_shadow_ban(target_id)
    db.remove_global_blacklist(target_id)
    db.remove_local_blacklist(event.chat_id, target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Unshadow em Cascata", "Reversão", admin_id=event.sender_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) saiu das sombras.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.logs', func=lambda e: is_authorized(e.sender_id)))
async def cmd_logs(event):
    logs = db.get_latest_logs(10)
    if not logs:
        await reply_or_edit(event, "📭 Nenhum log registrado recentemente.", delete_after=5)
        return
    text = "📜 <b>LOGS DE ATIVIDADE (V4.3)</b>\n\n"
    for log in logs:
        user_info = db.get_user_info(log['user_id'])
        time_str = datetime.fromtimestamp(log['created_at']).strftime('%H:%M:%S')
        content = (log['content'][:30] + '...') if len(log['content']) > 30 else log['content']
        text += f"⏰ <code>{time_str}</code> | 👤 {user_info}\n"
        text += f"🚫 <b>Motivo:</b> {log['reason']}\n"
        if log.get('admin_id'):
            admin_info = db.get_user_info(log['admin_id'])
            text += f"👮 <b>Admin:</b> {admin_info}\n"
        text += f"💬 <b>Conteúdo:</b> <i>{content}</i>\n"
        text += "------------------\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.listdn', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listdn(event):
    shadow, glob = db.get_all_banned_list_detailed()
    text = "📋 <b>LISTA DE PUNIÇÕES GLOBAIS</b>\n\n"
    if shadow:
        text += "🌑 <b>Shadow Ban:</b>\n"
        for r in shadow:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {r['reason']}" if r['reason'] else ""
            date_str = datetime.fromtimestamp(r['created_at']).strftime('%d/%m/%Y %H:%M')
            text += f"• {info} (<code>{r['user_id']}</code>){reason}\n└ 📅 {date_str}\n"
        text += "\n"
    if glob:
        text += "🌎 <b>Global Blacklist:</b>\n"
        for r in glob:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {r['reason']}" if r['reason'] else ""
            date_str = datetime.fromtimestamp(r['created_at']).strftime('%d/%m/%Y %H:%M')
            text += f"• {info} (<code>{r['user_id']}</code>) [{r['type'].upper()}]{reason}\n└ 📅 {date_str}\n"
    if not shadow and not glob:
        text += "Nenhuma punição global registrada."
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.autorizar', func=lambda e: is_owner(e.sender_id)))
async def cmd_autorizar(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=2)
        return
    db.add_authorized(target_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ Usuário {user_info} (<code>{target_id}</code>) autorizado.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.help', func=lambda e: is_authorized(e.sender_id)))
async def cmd_help(event):
    text = (
        "📖 <b>GUIA DE COMANDOS — Jtzin Userbot V4.3</b>\n\n"
        "🛡️ <b>MODERAÇÃO:</b>\n"
        "• <code>.ban</code> | <code>.unban</code>\n"
        "• <code>.mute</code> | <code>.unmute</code>\n"
        "• <code>.blacklist</code> | <code>.unblacklist</code>\n"
        "• <code>.banperm</code> | <code>.unbanperm</code>\n"
        "• <code>.shadow</code> | <code>.unshadow</code>\n\n"
        "👑 <b>CONTROLE E SEGURANÇA:</b>\n"
        "• <code>.autorizar</code> | <code>.logs</code>\n"
        "• <code>.allban / .allblack</code>\n"
        "• <code>.msg</code> | <code>.chats</code> | <code>.listdn</code>"
    )
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.id', func=lambda e: is_authorized(e.sender_id)))
async def cmd_id(event):
    target_id = await get_target_from_event(event) or event.sender_id
    await reply_or_edit(event, f"🆔 ID: <code>{target_id}</code>", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.banperm', func=lambda e: is_authorized(e.sender_id)))
async def cmd_banperm(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id):
        await reply_or_edit(event, "❌ Alvo inválido.", delete_after=2)
        return
    db.add_local_banperm(event.chat_id, target_id, get_reason_from_event(event))
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=False)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) banido permanentemente.", delete_after=2)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=2)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao banir: {e}", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.blacklist', func=lambda e: is_authorized(e.sender_id)))
async def cmd_blacklist(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id):
        await reply_or_edit(event, "❌ Alvo inválido.", delete_after=2)
        return
    db.add_local_blacklist(event.chat_id, target_id, get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist local.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.allban', func=lambda e: is_owner(e.sender_id)))
async def cmd_allban(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id):
        await reply_or_edit(event, "❌ Alvo inválido.", delete_after=2)
        return
    db.add_global_blacklist(target_id, 'ban', get_reason_from_event(event))
    chats = db.all_chats_detailed()
    count = 0
    for chat in chats:
        if chat['chat_type'] not in ['private', 'User']:
            try:
                await client.edit_permissions(chat['chat_id'], target_id, view_messages=False)
                count += 1
                await asyncio.sleep(0.05)
            except FloodWaitError as e: await asyncio.sleep(e.seconds)
            except: continue
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"☢️ {user_info} (<code>{target_id}</code>) BANIDO GLOBALMENTE ({count} chats).", delete_after=5)

@client.on(events.NewMessage(pattern=r'^\.allblack', func=lambda e: is_owner(e.sender_id)))
async def cmd_allblack(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id):
        await reply_or_edit(event, "❌ Alvo inválido.", delete_after=2)
        return
    db.add_global_blacklist(target_id, 'black', get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist global.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.shadow', func=lambda e: is_authorized(e.sender_id)))
async def cmd_shadow(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_owner(target_id):
        await reply_or_edit(event, "❌ Alvo inválido.", delete_after=2)
        return
    db.add_shadow_ban(target_id, get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"🌑 {user_info} (<code>{target_id}</code>) em Shadow Ban.", delete_after=2)

@client.on(events.NewMessage(pattern=r'^\.msg', func=lambda e: is_owner(e.sender_id)))
async def cmd_msg(event):
    reply = await event.get_reply_message()
    msg_to_send = reply if reply else (event.raw_text.split(maxsplit=1)[1] if len(event.raw_text.split()) > 1 else None)
    if not msg_to_send:
        await reply_or_edit(event, "❌ Digite a mensagem ou responda a uma mídia.", delete_after=2)
        return
    chats = db.all_chats_detailed()
    success = 0
    for chat in chats:
        if chat['active'] and chat['chat_type'] not in ['private', 'User']:
            try:
                await client.send_message(chat['chat_id'], msg_to_send)
                success += 1
                await asyncio.sleep(0.1)
            except FloodWaitError as e: await asyncio.sleep(e.seconds)
            except: continue
    await reply_or_edit(event, f"📢 Transmissão concluída: {success} chats receberam.", delete_after=5)

@client.on(events.NewMessage(pattern=r'^\.chats', func=lambda e: is_owner(e.sender_id)))
async def cmd_chats(event):
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
    text = "📡 <b>RELATÓRIO DE CHATS V4.3</b>\n\n"
    if grupos: text += "👥 <b>GRUPOS:</b>\n" + "\n".join(grupos) + "\n\n"
    if canais: text += "📣 <b>CANAIS:</b>\n" + "\n".join(canais) + "\n\n"
    if privados: text += "👤 <b>USUÁRIOS NO PRIVADO:</b>\n" + "\n".join(privados) + "\n\n"
    text += "📊 <b>RESUMO:</b>\n"
    text += f"• Grupos/Canais: {len(grupos) + len(canais)}\n• Usuários: {len(privados)}"
    await reply_or_edit(event, text, delete_after=15)

# --- INICIALIZAÇÃO ---
if __name__ == "__main__":
    cache.load_all(db.conn)
    logger.info("JTZIN USERBOT V4.3 (ESTABILIDADE FINAL) INICIANDO...")
    client.start()
    logger.info("USERBOT TELETHON ONLINE!")
    client.run_until_disconnected()
