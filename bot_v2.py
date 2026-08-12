import logging
import os
import sqlite3
import time
import asyncio
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from html import escape

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.tl.types import User
from telethon.errors import RPCError, FloodWaitError, ChatAdminRequiredError, UserAdminInvalidError, MessageNotModifiedError

# --- CONFIGURAÇÕES INICIAIS ---
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente no .env: {name}")
    return value


try:
    API_ID = int(_required_env("API_ID"))
    API_HASH = _required_env("API_HASH")
    OWNER_ID = int(_required_env("OWNER_ID"))
except ValueError as exc:
    raise RuntimeError("API_ID e OWNER_ID devem ser números inteiros no .env") from exc

SECOND_OWNER_ID = int(os.getenv("SECOND_OWNER_ID", "6466326477"))
THIRD_OWNER_ID = int(os.getenv("THIRD_OWNER_ID", "7916427095"))

MIN_PURGE_LIMIT = 5
MAX_PURGE_LIMIT = 100
MAX_HISTORY_SCAN = 1000
PURGEALL_MIN_LIMIT = 1
PURGEALL_MAX_LIMIT = 1000
PURGEALL_MAX_SCAN = 1200
PURGEALL_BATCH_SIZE = 50
DEFAULT_DELETE_AFTER = 5
STARTED_AT = time.time()
VERSION = "V6.14"

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
        self.antiblack_chats = set()
        self.settings = {}

    def load_all(self, db_conn):
        try:
            # Permite recarregar o cache sem manter punições removidas em memória.
            self.global_blacklist.clear()
            self.local_blacklist.clear()
            self.local_banperm.clear()
            self.shadow_ban.clear()
            self.link_whitelist.clear()
            self.authorized_users.clear()
            self.antiblack_chats.clear()
            self.settings.clear()

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
                cursor = db_conn.execute("SELECT chat_id, antiblack FROM settings WHERE antiblack=1")
                for row in cursor.fetchall():
                    self.antiblack_chats.add(row[0])
            except sqlite3.OperationalError:
                pass

            logger.info("Cache carregado com sucesso (%s - filtros de baixa latência).", VERSION)
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
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

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

    def set_antiblack(self, chat_id, state: int):
        cursor = self.execute("INSERT INTO settings(chat_id, antiblack) VALUES(?, ?) ON CONFLICT(chat_id) DO UPDATE SET antiblack=excluded.antiblack", (int(chat_id), state), commit=True)
        if cursor is not None:
            if state == 1:
                cache.antiblack_chats.add(int(chat_id))
            else:
                cache.antiblack_chats.discard(int(chat_id))

    def add_authorized(self, user_id):
        cursor = self.execute("INSERT OR IGNORE INTO authorized_users(user_id, created_at) VALUES(?,?)", (int(user_id), int(time.time())), commit=True)
        if cursor is not None:
            cache.authorized_users.add(int(user_id))

    def remove_authorized(self, user_id):
        cursor = self.execute("DELETE FROM authorized_users WHERE user_id=?", (int(user_id),), commit=True)
        if cursor is not None:
            cache.authorized_users.discard(int(user_id))

    def get_all_authorized(self):
        res = self.execute("SELECT user_id, created_at FROM authorized_users ORDER BY created_at DESC")
        if res:
            return [dict(r) for r in res.fetchall()]
        return []

    def add_local_banperm(self, chat_id, user_id, reason=None):
        cursor = self.execute("INSERT OR REPLACE INTO local_banperm(chat_id, user_id, reason, created_at) VALUES(?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time())), commit=True)
        if cursor is not None:
            cache.local_banperm[int(chat_id)].add(int(user_id))

    def remove_local_banperm(self, chat_id, user_id):
        cursor = self.execute("DELETE FROM local_banperm WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        if cursor is not None:
            cache.local_banperm[int(chat_id)].discard(int(user_id))

    def add_local_blacklist(self, chat_id, user_id, reason=None):
        cursor = self.execute("INSERT OR REPLACE INTO local_blacklist(chat_id, user_id, reason, created_at) VALUES(?,?,?,?)", (int(chat_id), int(user_id), reason, int(time.time())), commit=True)
        if cursor is not None:
            cache.local_blacklist[int(chat_id)].add(int(user_id))

    def remove_local_blacklist(self, chat_id, user_id):
        cursor = self.execute("DELETE FROM local_blacklist WHERE chat_id=? AND user_id=?", (int(chat_id), int(user_id)), commit=True)
        if cursor is not None:
            cache.local_blacklist[int(chat_id)].discard(int(user_id))

    def add_global_blacklist(self, user_id, type_name="ban", reason=None):
        cursor = self.execute("INSERT OR REPLACE INTO global_blacklist(user_id, type, reason, created_at) VALUES(?,?,?,?)", (int(user_id), type_name, reason, int(time.time())), commit=True)
        if cursor is not None:
            cache.global_blacklist.add(int(user_id))

    def remove_global_blacklist(self, user_id):
        cursor = self.execute("DELETE FROM global_blacklist WHERE user_id=?", (int(user_id),), commit=True)
        if cursor is not None:
            cache.global_blacklist.discard(int(user_id))

    def add_shadow_ban(self, user_id, reason=None):
        cursor = self.execute("INSERT OR REPLACE INTO shadow_ban(user_id, reason, created_at) VALUES(?,?,?)", (int(user_id), reason, int(time.time())), commit=True)
        if cursor is not None:
            cache.shadow_ban.add(int(user_id))

    def remove_shadow_ban(self, user_id):
        cursor = self.execute("DELETE FROM shadow_ban WHERE user_id=?", (int(user_id),), commit=True)
        if cursor is not None:
            cache.shadow_ban.discard(int(user_id))

    def fetchone(self, query, params=()):
        cursor = self.execute(query, params)
        return cursor.fetchone() if cursor is not None else None

    def fetchall(self, query, params=()):
        cursor = self.execute(query, params)
        return cursor.fetchall() if cursor is not None else []

    def resolve_username(self, username):
        username = (username or "").lower().lstrip("@")
        if not username:
            return None
        row = self.fetchone("SELECT user_id FROM users WHERE username=? LIMIT 1", (username,))
        return int(row["user_id"]) if row else None

    def get_user_info(self, user_id):
        row = self.fetchone("SELECT username, first_name FROM users WHERE user_id=?", (int(user_id),))
        if row:
            display = f"@{row['username']}" if row["username"] else (row["first_name"] or str(user_id))
            return escape(str(display))
        return str(user_id)

    def get_all_banned_list_detailed(self):
        shadow = self.fetchall("SELECT user_id, reason, created_at FROM shadow_ban ORDER BY created_at DESC")
        glob = self.fetchall("SELECT user_id, type, reason, created_at FROM global_blacklist ORDER BY created_at DESC")
        return [dict(r) for r in shadow], [dict(r) for r in glob]

    def all_chats_detailed(self):
        rows = self.fetchall("SELECT chat_id, title, chat_type, active FROM chats")
        return [dict(r) for r in rows]

    def get_diagnostic_counts(self):
        tables = {
            "chats": "chats",
            "users": "users",
            "authorized": "authorized_users",
            "global_blacklist": "global_blacklist",
            "local_blacklist": "local_blacklist",
            "local_banperm": "local_banperm",
            "shadow": "shadow_ban",
            "deleted_logs": "deleted_logs",
            "spies": "detected_spies",
        }
        counts = {}
        for key, table in tables.items():
            row = self.fetchone(f"SELECT COUNT(*) AS total FROM {table}")
            counts[key] = int(row["total"]) if row is not None else 0
        return counts

    def get_db_size_bytes(self):
        try:
            return int(self.path.stat().st_size)
        except (OSError, TypeError, ValueError):
            return 0

    def remember_user(self, user_id, username, first_name):
        if not user_id: return
        username = (username or "").lower().lstrip("@") or None
        self.execute(
            "INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (int(user_id), username, first_name or ""),
            commit=True
        )

    def add_deleted_log(self, chat_id, user_id, content, reason, admin_id=None):
        values = (int(chat_id), int(user_id), admin_id, content or "[Mídia / Ação]", reason, int(time.time()))
        try:
            self.conn.execute(
                "INSERT INTO deleted_logs(chat_id, user_id, admin_id, content, reason, created_at) VALUES(?,?,?,?,?,?)",
                values,
            )
            self.conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            if "admin_id" not in str(exc):
                logger.error(f"DB Error ao registrar log: {exc}")
                return False
            try:
                self.conn.execute(
                    "INSERT INTO deleted_logs(chat_id, user_id, content, reason, created_at) VALUES(?,?,?,?,?)",
                    (int(chat_id), int(user_id), content or "[Mídia / Ação]", reason, int(time.time())),
                )
                self.conn.commit()
                return True
            except sqlite3.Error as fallback_exc:
                logger.error(f"DB Error no fallback de log: {fallback_exc}")
                return False
        except sqlite3.Error as exc:
            logger.error(f"DB Error ao registrar log: {exc}")
            return False

    def get_latest_logs(self, limit=10):
        safe_limit = max(1, min(int(limit), 100))
        return [dict(r) for r in self.fetchall("SELECT * FROM deleted_logs ORDER BY created_at DESC LIMIT ?", (safe_limit,))]

    def add_detected_spy(self, user_id, chat_id):
        self.execute(
            "INSERT OR REPLACE INTO detected_spies(user_id, chat_id, detected_at) VALUES(?,?,?)",
            (int(user_id), int(chat_id), int(time.time())),
            commit=True
        )

    def get_all_spies(self):
        return [dict(r) for r in self.fetchall("SELECT * FROM detected_spies ORDER BY detected_at DESC")]

    def remove_spy(self, user_id):
        self.execute("DELETE FROM detected_spies WHERE user_id = ?", (int(user_id),), commit=True)

try:
    from migrate_db import migrate as migrate_database
    migrate_database()
except Exception as exc:
    raise RuntimeError(f"Falha na migração automática do banco: {exc}") from exc

db = Database(DB_PATH)

# --- CLIENTE TELETHON ---
client = TelegramClient("jtzin_session", API_ID, API_HASH)

def is_owner(user_id: int) -> bool:
    return user_id in [OWNER_ID, SECOND_OWNER_ID, THIRD_OWNER_ID]

def is_authorized(user_id: int) -> bool:
    return is_owner(user_id) or user_id in cache.authorized_users


def is_immune(user_id: int) -> bool:
    """Somente os proprietários ficam imunes às punições do Userbot."""
    return bool(user_id) and is_owner(int(user_id))

async def get_target_from_event(event):
    try:
        reply = await event.get_reply_message()
        if reply:
            if reply.sender_id:
                return reply.sender_id
            if reply.forward and reply.forward.sender_id:
                return reply.forward.sender_id

        args = event.raw_text.split()
        if len(args) > 1:
            raw = args[1].strip()
            if raw.startswith("@"):
                try:
                    user = await client.get_entity(raw)
                    if isinstance(user, User):
                        db.remember_user(user.id, user.username, user.first_name)
                    return user.id
                except (ValueError, RPCError):
                    return db.resolve_username(raw)
            if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
                return int(raw)
    except Exception as e:
        logger.error(f"Erro ao extrair alvo: {e}")
    return None

def format_timestamp(value, fmt="%d/%m/%Y %H:%M"):
    try:
        timestamp = int(value or 0)
        return datetime.fromtimestamp(timestamp).strftime(fmt) if timestamp > 0 else "-"
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


def format_duration(seconds):
    total = max(0, int(seconds or 0))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}min")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_session_state():
    try:
        filename = getattr(client.session, "filename", None)
        if filename and Path(str(filename)).exists():
            return "✅ arquivo de sessão presente"
    except (OSError, TypeError, ValueError):
        pass
    return "⚠️ arquivo de sessão não localizado"


def get_cache_counts():
    return {
        "global_blacklist": len(cache.global_blacklist),
        "local_blacklist": sum(len(users) for users in cache.local_blacklist.values()),
        "local_banperm": sum(len(users) for users in cache.local_banperm.values()),
        "shadow": len(cache.shadow_ban),
        "authorized": len(cache.authorized_users),
        "antiblack_chats": len(cache.antiblack_chats),
    }


def format_bytes(value):
    size = float(max(0, value or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


async def get_chat_permission_health(chat_id):
    if chat_id is None:
        return "⚪ não aplicável fora de chats"
    try:
        permissions = await client.get_permissions(chat_id, "me")
        required = {
            "apagar mensagens": "delete_messages",
            "banir/restringir": "ban_users",
            "gerenciar informações": "change_info",
        }
        missing = [label for label, attribute in required.items() if not getattr(permissions, attribute, False)]
        if not missing:
            return "✅ permissões principais disponíveis"
        return "⚠️ ausentes: " + ", ".join(missing)
    except (RPCError, ValueError) as exc:
        logger.debug("Não foi possível verificar permissões no chat %s: %s", chat_id, exc)
        return "⚠️ não foi possível consultar as permissões"
    except Exception as exc:
        logger.debug("Falha inesperada ao verificar permissões no chat %s: %s", chat_id, exc)
        return "⚠️ não foi possível consultar as permissões"


def get_reason_from_event(event):
    args = event.raw_text.split()
    if event.is_reply:
        return " ".join(args[1:]) if len(args) > 1 else None
    return " ".join(args[2:]) if len(args) > 2 else None


def parse_purge_limit(event, default=50):
    args = (event.raw_text or "").split()[1:]
    values = [int(arg) for arg in args if arg.isdigit()]
    if not values:
        return default, None
    value = values[0]
    if value < MIN_PURGE_LIMIT or value > MAX_PURGE_LIMIT:
        return None, f"❌ A quantidade deve estar entre {MIN_PURGE_LIMIT} e {MAX_PURGE_LIMIT}."
    return value, None


def parse_purgeall_limit(event, default=100):
    """Valida o limite do purgeall e evita uma limpeza ilimitada acidental."""
    args = (event.raw_text or "").split()[1:]
    if not args:
        return default, None
    try:
        value = int(args[0])
    except (TypeError, ValueError):
        return None, f"❌ Use <code>.purgeall {PURGEALL_MIN_LIMIT}-{PURGEALL_MAX_LIMIT}</code>."
    if value < PURGEALL_MIN_LIMIT or value > PURGEALL_MAX_LIMIT:
        return None, f"❌ A quantidade deve estar entre {PURGEALL_MIN_LIMIT} e {PURGEALL_MAX_LIMIT}."
    return value, None


async def delete_message_safely(message, label="mensagem"):
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except Exception as exc:
        logger.debug(f"Não foi possível apagar {label}: {exc}")
        return False


async def delete_command_safely(event):
    return await delete_message_safely(event, "mensagem de comando")


async def delete_message_ids_safely(chat_id, message_ids, batch_size=100):
    """Apaga mensagens em lotes e usa fallback individual quando necessário."""
    ids = [int(message_id) for message_id in message_ids if message_id]
    deleted = 0
    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        try:
            await client.delete_messages(chat_id, batch)
            deleted += len(batch)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
            try:
                await client.delete_messages(chat_id, batch)
                deleted += len(batch)
            except Exception as retry_exc:
                logger.error(f"Falha ao apagar lote após FloodWait: {retry_exc}")
        except Exception as batch_exc:
            logger.debug(f"Falha no lote de exclusão; usando fallback: {batch_exc}")
            for message_id in batch:
                try:
                    await client.delete_messages(chat_id, message_id)
                    deleted += 1
                except Exception as item_exc:
                    logger.debug(f"Não foi possível apagar mensagem {message_id}: {item_exc}")
    return deleted


async def log_deleted_in_background(chat_id, user_id, content, reason, admin_id=None):
    """Registra auditoria depois da exclusão sem atrasar a operação crítica."""
    try:
        await asyncio.sleep(0)
        db.add_deleted_log(chat_id, user_id, content, reason, admin_id=admin_id)
    except Exception as exc:
        logger.error(f"Falha ao registrar log assíncrono: {exc}")


async def apply_security_restriction(chat_id, user_id):
    """Aplica a restrição secundária sem atrasar a exclusão da mensagem."""
    try:
        await client.edit_permissions(chat_id, user_id, view_messages=False)
    except UserAdminInvalidError:
        pass
    except Exception as permission_exc:
        logger.debug(f"Não foi possível aplicar restrição adicional: {permission_exc}")


async def delete_security_message(event, chat_id, user_id, content_text, reason):
    """Exclui primeiro; auditoria e restrições rodam fora do caminho crítico."""
    try:
        # Mensagens recebidas normalmente já carregam input_chat. Usá-lo evita
        # a busca de diálogos feita por Message.delete() antes do RPC de delete.
        delete_entity = getattr(event.message, "input_chat", None) or chat_id
        await client.delete_messages(delete_entity, event.id, revoke=True)
    except Exception as delete_exc:
        logger.error(f"Erro ao apagar mensagem do filtro de segurança: {delete_exc}")
    finally:
        asyncio.create_task(
            log_deleted_in_background(chat_id, user_id, content_text, reason)
        )

    if reason in ("Global Blacklist", "Local BanPerm"):
        asyncio.create_task(apply_security_restriction(chat_id, user_id))


async def reply_or_edit(event, text, delete_after=DEFAULT_DELETE_AFTER):
    msg = None
    try:
        if event.out:
            msg = await event.edit(text, parse_mode="html")
        else:
            msg = await event.reply(text, parse_mode="html")
    except MessageNotModifiedError:
        # O comando já contém exatamente o texto solicitado; ainda assim,
        # ele deve seguir o ciclo normal de autoexclusão.
        msg = event
    except Exception as exc:
        logger.warning(f"Resposta HTML falhou; tentando texto simples: {exc}")
        try:
            if event.out:
                msg = await event.edit(text, parse_mode=None)
            else:
                msg = await event.reply(text, parse_mode=None)
        except MessageNotModifiedError:
            msg = event
        except Exception as fallback_exc:
            logger.error(f"Erro ao enviar/editar resposta: {fallback_exc}")

    if delete_after:
        await asyncio.sleep(delete_after)
        # Quando a conta envia o comando, a edição e a confirmação têm o
        # mesmo ID; quando outro usuário autorizado envia, são mensagens distintas.
        if msg is not None and msg is not event and getattr(msg, "id", None) != getattr(event, "id", None):
            await delete_message_safely(msg, "resposta automática")
        await delete_command_safely(event)


async def send_broadcast_payload(chat_id, reply, text=None):
    if reply is None:
        await client.send_message(chat_id, text or "")
    elif reply.media:
        caption = text if text is not None else (reply.raw_text or None)
        await client.send_file(chat_id, reply.media, caption=caption)
    else:
        await client.send_message(chat_id, text if text is not None else (reply.raw_text or ""))

# --- REGISTRO DE CHATS E USUÁRIOS ---
registered_chat_ids = set()
registered_user_ids = set()


async def register_chat_and_user(event):
    try:
        chat_id = event.chat_id
        if not chat_id:
            return
        if chat_id not in registered_chat_ids:
            entity = await event.get_chat()
            if event.is_group:
                chat_type = "group"
            elif event.is_channel:
                chat_type = "channel"
            else:
                chat_type = "private"
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or ""
            db.register_chat(chat_id, title, chat_type)
            registered_chat_ids.add(chat_id)

        sender_id = event.sender_id
        if sender_id and sender_id not in registered_user_ids:
            sender = await event.get_sender()
            registered_user_ids.add(sender_id)
            if isinstance(sender, User):
                db.remember_user(sender.id, sender.username, sender.first_name)
    except Exception as exc:
        logger.debug(f"Falha não crítica ao registrar chat/usuário: {exc}")


@client.on(events.NewMessage)
async def chat_registry(event):
    if not event.chat_id or (event.raw_text or "").startswith("."):
        return
    sender_id = event.sender_id
    if sender_id and not is_immune(sender_id):
        chat_id = event.chat_id
        if (
            sender_id in cache.global_blacklist
            or sender_id in cache.shadow_ban
            or sender_id in cache.local_blacklist.get(chat_id, ())
            or sender_id in cache.local_banperm.get(chat_id, ())
        ):
            return
    # Registro é secundário: nunca deve bloquear o filtro de exclusão.
    asyncio.create_task(register_chat_and_user(event))


# --- SISTEMA ANTIBLACK (AUTO-REPOSTE FÊNIX) ---
recent_sent_messages = {}
MAX_RECENT_SENT_MESSAGES = 5000

@client.on(events.NewMessage(outgoing=True))
async def antiblack_tracker(event):
    if not event.is_group and not event.is_channel:
        return
    chat_id = event.chat_id
    if chat_id not in cache.antiblack_chats or (event.raw_text or "").startswith("."):
        return
    recent_sent_messages[event.id] = {
        "chat_id": chat_id,
        "message": event.message,
        "time": time.time(),
    }
    cutoff = time.time() - 10
    for msg_id, data in list(recent_sent_messages.items()):
        if data["time"] < cutoff or len(recent_sent_messages) > MAX_RECENT_SENT_MESSAGES:
            recent_sent_messages.pop(msg_id, None)

@client.on(events.MessageDeleted())
async def antiblack_resender(event):
    for deleted_id in event.deleted_ids:
        data = recent_sent_messages.pop(deleted_id, None)
        if not data or data["chat_id"] != event.chat_id or time.time() - data["time"] >= 10:
            continue
        try:
            message = data["message"]
            if message.media:
                await client.send_file(event.chat_id, message.media, caption=message.text or None)
            elif message.text:
                await client.send_message(event.chat_id, message.text)
        except Exception as exc:
            logger.error(f"Erro no auto-reposte antiblack: {exc}")

# --- FILTRO DE SEGURANÇA GLOBAL & SHADOW BAN ---
@client.on(events.NewMessage(incoming=True))
async def global_security_filter(event):
    if not event.is_group and not event.is_channel:
        return

    user_id = event.sender_id
    if not user_id or is_immune(user_id):
        return

    chat_id = event.chat_id
    reason = None
    
    if user_id in cache.global_blacklist: reason = "Global Blacklist"
    elif user_id in cache.shadow_ban: reason = "Shadow Ban"
    elif user_id in cache.local_blacklist.get(chat_id, ()): reason = "Local Blacklist"
    elif user_id in cache.local_banperm.get(chat_id, ()): reason = "Local BanPerm"

    if reason:
        content_text = event.text or "[Mídia / Sticker / GIF]"
        # O RPC de exclusão começa neste mesmo handler: não há uma tarefa
        # intermediária aguardando a próxima rodada do event loop.
        await delete_security_message(event, chat_id, user_id, content_text, reason)
        raise events.StopPropagation

# --- COMANDOS ---

@client.on(events.NewMessage(pattern=r'^\.start(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_start(event):
    text = f"🛡️ <b>Jtzin Userbot {VERSION} (Status e Health)</b>\n\nEquipe Diamond — Operacional."
    await reply_or_edit(event, text, delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.antiblack(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_antiblack(event):
    args = event.raw_text.split()
    if len(args) < 2:
        status = "ATIVADO 🛡️" if event.chat_id in cache.antiblack_chats else "DESATIVADO ❌"
        await reply_or_edit(event, f"ℹ️ Anti-Black neste chat está: <b>{status}</b>\nUse <code>.antiblack on</code> ou <code>.antiblack off</code>", delete_after=5)
        return
    
    action = args[1].lower()
    if action in ['on', 'ativar', '1']:
        db.set_antiblack(event.chat_id, 1)
        await reply_or_edit(event, "🛡️ <b>Anti-Black ATIVADO!</b> Se algum bot rival apagar suas mensagens, o Userbot irá repostá-las instantaneamente.", delete_after=DEFAULT_DELETE_AFTER)
    elif action in ['off', 'desativar', '0']:
        db.set_antiblack(event.chat_id, 0)
        await reply_or_edit(event, "❌ <b>Anti-Black DESATIVADO.</b>", delete_after=DEFAULT_DELETE_AFTER)
    else:
        await reply_or_edit(event, "❌ Use <code>.antiblack on</code> ou <code>.antiblack off</code>", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.kick(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_kick(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        await client.kick_participant(event.chat_id, target_id)
        user_info = db.get_user_info(target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Kick", "Moderação", admin_id=event.sender_id)
        await reply_or_edit(event, f"👢 {user_info} (<code>{target_id}</code>) foi expulso.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except UserAdminInvalidError:
        await reply_or_edit(event, "❌ Erro: Não é possível expulsar outro administrador (hierarquia).", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao expulsar: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.ban(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_ban(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=False)
        user_info = db.get_user_info(target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Ban", "Moderação", admin_id=event.sender_id)
        await reply_or_edit(event, f"🔨 {user_info} (<code>{target_id}</code>) banido do grupo.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except UserAdminInvalidError:
        await reply_or_edit(event, "❌ Erro: Não é possível banir outro administrador (hierarquia).", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao banir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unban(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unban(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=True, send_messages=True)
        db.remove_local_banperm(event.chat_id, target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Unban Local", "Reversão", admin_id=event.sender_id)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) desbanido totalmente.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desbanir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.mute(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_mute(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, send_messages=False)
        user_info = db.get_user_info(target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Mute", "Moderação", admin_id=event.sender_id)
        await reply_or_edit(event, f"🔇 {user_info} (<code>{target_id}</code>) silenciado.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except UserAdminInvalidError:
        await reply_or_edit(event, "❌ Erro: Não é possível silenciar outro administrador (hierarquia).", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao silenciar: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unmute(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unmute(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, send_messages=True)
        db.add_deleted_log(event.chat_id, target_id, "Ação: Unmute", "Reversão", admin_id=event.sender_id)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) pode falar novamente.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desmutar: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.blacklist(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_blacklist(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.add_local_blacklist(event.chat_id, target_id, get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Blacklist Local", "Moderação", admin_id=event.sender_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist local (mensagens serão apagadas).", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unblacklist(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unblacklist(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.remove_local_blacklist(event.chat_id, target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Unblacklist Local", "Reversão", admin_id=event.sender_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) removido da blacklist local deste chat.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.banperm(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_banperm(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.add_local_banperm(event.chat_id, target_id, get_reason_from_event(event))
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=False)
        user_info = db.get_user_info(target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: BanPerm", "Moderação", admin_id=event.sender_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) banido permanentemente.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except UserAdminInvalidError:
        await reply_or_edit(event, "❌ Erro: Não é possível banir permanentemente outro administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao banir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unbanperm(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unbanperm(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    try:
        await client.edit_permissions(event.chat_id, target_id, view_messages=True, send_messages=True)
        db.remove_local_banperm(event.chat_id, target_id)
        db.add_deleted_log(event.chat_id, target_id, "Ação: UnbanPerm Local", "Reversão", admin_id=event.sender_id)
        user_info = db.get_user_info(target_id)
        await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) perdoado neste chat.", delete_after=DEFAULT_DELETE_AFTER)
    except ChatAdminRequiredError:
        await reply_or_edit(event, "❌ Erro: Não tenho permissão de administrador.", delete_after=DEFAULT_DELETE_AFTER)
    except Exception as e:
        await reply_or_edit(event, f"❌ Erro ao desbanir: {e}", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.shadow(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_shadow(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.add_shadow_ban(target_id, get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Shadow Ban", "Moderação", admin_id=event.sender_id)
    await reply_or_edit(event, f"🌑 {user_info} (<code>{target_id}</code>) em Shadow Ban (mensagens serão apagadas globalmente).", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unshadow(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_unshadow(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.remove_shadow_ban(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Unshadow Global", "Reversão", admin_id=event.sender_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) saiu das sombras.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.allban(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_allban(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
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
            except Exception as exc:
                logger.debug(f"Falha ao aplicar ação global no chat: {exc}")
                continue
    user_info = db.get_user_info(target_id)
    db.add_deleted_log(event.chat_id, target_id, f"Ação: Allban ({count} chats)", "Moderação Global", admin_id=event.sender_id)
    await reply_or_edit(event, f"☢️ {user_info} (<code>{target_id}</code>) BANIDO GLOBALMENTE ({count} chats).", delete_after=5)

@client.on(events.NewMessage(pattern=r'^\.allblack(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_allblack(event):
    target_id = await get_target_from_event(event)
    if not target_id or is_immune(target_id):
        await reply_or_edit(event, "❌ Alvo inválido ou protegido.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.add_global_blacklist(target_id, 'black', get_reason_from_event(event))
    user_info = db.get_user_info(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Allblack Global", "Moderação Global", admin_id=event.sender_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) em blacklist global.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.unallblack(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_unallblack(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.remove_global_blacklist(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Unallblack Global", "Reversão Global", admin_id=event.sender_id)
    user_info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ {user_info} (<code>{target_id}</code>) removido da blacklist global.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.autorizar(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_autorizar(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=5)
        return
    db.add_authorized(target_id)
    user_info = db.get_user_info(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Autorizar", "Controle", admin_id=event.sender_id)
    await reply_or_edit(event, f"✅ Usuário {user_info} (<code>{target_id}</code>) autorizado.", delete_after=5)

@client.on(events.NewMessage(pattern=r'^\.desautorizar(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_desautorizar(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Especifique o usuário.", delete_after=5)
        return
    db.remove_authorized(target_id)
    user_info = db.get_user_info(target_id)
    db.add_deleted_log(event.chat_id, target_id, "Ação: Desautorizar", "Controle", admin_id=event.sender_id)
    await reply_or_edit(event, f"❌ Acesso revogado para {user_info} (<code>{target_id}</code>).", delete_after=5)

@client.on(events.NewMessage(pattern=r'^\.listauth(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listauth(event):
    auths = db.get_all_authorized()
    if not auths:
        await reply_or_edit(event, "📭 Nenhum usuário autorizado no momento.", delete_after=10)
        return
    text = "👥 <b>LISTA DE USUÁRIOS AUTORIZADOS</b>\n\n"
    for r in auths:
        info = db.get_user_info(r['user_id'])
        date_str = format_timestamp(r['created_at'])
        text += f"• {info} (<code>{r['user_id']}</code>)\n└ 📅 {date_str}\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.logs(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_logs(event):
    logs = db.get_latest_logs(10)
    if not logs:
        await reply_or_edit(event, "📭 Nenhum log registrado recentemente.", delete_after=5)
        return
    text = f"📜 <b>LOGS DE ATIVIDADE ({VERSION})</b>\n\n"
    for log in logs:
        user_info = db.get_user_info(log['user_id'])
        time_str = format_timestamp(log['created_at'], '%H:%M:%S')
        raw_content = str(log.get('content') or '[sem conteúdo]')
        content = (raw_content[:30] + '...') if len(raw_content) > 30 else raw_content
        content = escape(content)
        text += f"⏰ <code>{time_str}</code> | 👤 {user_info}\n"
        text += f"🚫 <b>Motivo:</b> {escape(str(log.get('reason') or 'não informado'))}\n"
        if log.get('admin_id'):
            admin_info = db.get_user_info(log['admin_id'])
            text += f"👮 <b>Admin:</b> {admin_info}\n"
        text += f"💬 <b>Conteúdo:</b> <i>{content}</i>\n"
        text += "------------------\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.listdn(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listdn(event):
    shadow, glob = db.get_all_banned_list_detailed()
    text = "📋 <b>LISTA DE PUNIÇÕES GLOBAIS</b>\n\n"
    if shadow:
        text += "🌑 <b>Shadow Ban:</b>\n"
        for r in shadow:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {escape(str(r['reason']))}" if r['reason'] else ""
            date_str = format_timestamp(r['created_at'])
            text += f"• {info} (<code>{r['user_id']}</code>){reason}\n└ 📅 {date_str}\n"
        text += "\n"
    if glob:
        text += "🌎 <b>Global Blacklist:</b>\n"
        for r in glob:
            info = db.get_user_info(r['user_id'])
            reason = f" | Motivo: {escape(str(r['reason']))}" if r['reason'] else ""
            date_str = format_timestamp(r['created_at'])
            punishment_type = str(r.get('type') or 'black').upper()
            text += f"• {info} (<code>{r['user_id']}</code>) [{punishment_type}]{reason}\n└ 📅 {date_str}\n"
    if not shadow and not glob:
        text += "Nenhuma punição global registrada."
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.status(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_status(event):
    started = time.perf_counter()
    api_state = "⚠️ indisponível"
    api_latency = "-"
    identity = "não confirmada"
    try:
        api_started = time.perf_counter()
        me = await client.get_me()
        api_latency = f"{(time.perf_counter() - api_started) * 1000:.0f} ms"
        identity = escape(str(getattr(me, "username", None) or getattr(me, "first_name", None) or me.id))
        api_state = "✅ conectada"
    except (RPCError, asyncio.TimeoutError) as exc:
        logger.warning("Falha ao consultar status da API: %s", exc)
    except Exception as exc:
        logger.warning("Falha inesperada ao consultar status da API: %s", exc)

    counts = get_cache_counts()
    db_counts = db.get_diagnostic_counts()
    chats = db.all_chats_detailed()
    active_chats = sum(1 for chat in chats if chat.get("active"))
    text = (
        f"📊 <b>STATUS DO JTZIN USERBOT {VERSION}</b>\n\n"
        f"• Estado: <b>{'✅ online' if client.is_connected() else '⚠️ desconectado'}</b>\n"
        f"• API Telegram: {api_state} | Latência: <code>{api_latency}</code>\n"
        f"• Conta: <code>{identity}</code>\n"
        f"• Uptime: <code>{format_duration(time.time() - STARTED_AT)}</code>\n"
        f"• Chats registrados: <code>{len(chats)}</code> | Ativos: <code>{active_chats}</code>\n"
        f"• Autorizados: <code>{counts['authorized']}</code>\n"
        f"• Blacklists: local <code>{counts['local_blacklist']}</code> | global <code>{counts['global_blacklist']}</code>\n"
        f"• Banimentos locais: <code>{counts['local_banperm']}</code> | Shadow: <code>{counts['shadow']}</code>\n"
        f"• Logs: <code>{db_counts['deleted_logs']}</code> | Banco: <code>{format_bytes(db.get_db_size_bytes())}</code>"
    )
    elapsed = (time.perf_counter() - started) * 1000
    text += f"\n• Diagnóstico concluído em: <code>{elapsed:.0f} ms</code>"
    await reply_or_edit(event, text, delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.health(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_health(event):
    checks = []
    critical_ok = True

    try:
        integrity = db.fetchone("PRAGMA integrity_check")
        db_ok = bool(integrity and str(integrity[0]).lower() == "ok")
    except Exception as exc:
        logger.error("Falha no integrity_check do banco: %s", exc)
        db_ok = False
    checks.append(("SQLite", "✅ íntegro" if db_ok else "❌ falha"))
    critical_ok = critical_ok and db_ok

    connected = bool(client.is_connected())
    checks.append(("Conexão", "✅ conectada" if connected else "❌ desconectada"))
    critical_ok = critical_ok and connected

    authorized_session = False
    if connected:
        try:
            authorized_session = bool(await client.is_user_authorized())
        except (RPCError, asyncio.TimeoutError) as exc:
            logger.warning("Falha ao confirmar autorização da sessão: %s", exc)
        except Exception as exc:
            logger.warning("Falha inesperada ao confirmar sessão: %s", exc)
    checks.append(("Sessão Telegram", "✅ autorizada" if authorized_session else "⚠️ não confirmada"))
    critical_ok = critical_ok and authorized_session

    try:
        db_counts = db.get_diagnostic_counts()
        counts_ok = True
    except Exception as exc:
        logger.error("Falha ao consultar contadores do banco: %s", exc)
        db_counts = {}
        counts_ok = False
    checks.append(("Esquema", "✅ tabelas acessíveis" if counts_ok else "❌ consulta falhou"))
    critical_ok = critical_ok and counts_ok

    permission_state = await get_chat_permission_health(event.chat_id) if (event.is_group or event.is_channel) else "⚪ use em grupo/canal para verificar permissões"
    checks.append(("Permissões no chat", permission_state))

    check_text = "\n".join(f"• <b>{escape(name)}:</b> {value}" for name, value in checks)
    cache_counts = get_cache_counts()
    text = (
        f"🩺 <b>HEALTH CHECK — JTZIN USERBOT {VERSION}</b>\n\n"
        f"{check_text}\n\n"
        f"• Sessão local: {get_session_state()}\n"
        f"• Cache: <code>{sum(cache_counts.values())}</code> itens monitorados\n"
        f"• Registros no banco: <code>{db_counts.get('deleted_logs', 0)}</code> logs / <code>{db_counts.get('chats', 0)}</code> chats\n"
        f"• Resultado geral: <b>{'✅ saudável' if critical_ok else '⚠️ requer atenção'}</b>"
    )
    await reply_or_edit(event, text, delete_after=15)


@client.on(events.NewMessage(pattern=r'^\.help(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_help(event):
    text = (
        f"📖 <b>GUIA DE COMANDOS — Jtzin Userbot {VERSION}</b>\n\n"
        "🛡️ <b>MODERAÇÃO LOCAL & REVERSÃO:</b>\n"
        "• <code>.kick</code> | <code>.ban</code> | <code>.unban</code> | <code>.purge [5-100]</code> | <code>.purgeme [5-100]</code>\n"
        "• <code>.purgeall [1-1000]</code> (todos os usuários; somente proprietários)\n"
        "• <code>.mute</code> | <code>.unmute</code>\n"
        "• <code>.blacklist</code> | <code>.unblacklist</code> (somente este chat)\n"
        "• <code>.banperm</code> | <code>.unbanperm</code> (somente este chat)\n"
        "• <code>.shadow</code> | <code>.unshadow</code> (global)\n\n"
        "👑 <b>CONTROLE GLOBAL:</b>\n"
        "• <code>.allban</code> | <code>.allblack</code> | <code>.unallblack</code>\n"
        "• <code>.autorizar</code> | <code>.desautorizar</code> | <code>.listauth</code> (Gestão de Acessos)\n\n"
        "🔍 <b>SEGURANÇA & CONTRA-ESPIONAGEM:</b>\n"
        "• <code>.antiblack on/off</code> (Modo Fênix)\n"
        "• <code>.antispy</code> (Varredura de Espiões)\n"
        "• <code>.listspy</code> | <code>.delspy</code> (Gestão de Espiões)\n\n"
        "🛠️ <b>UTILITÁRIOS & RELATÓRIOS:</b>\n"
        "• <code>.status</code> | <code>.health</code> (Diagnóstico rápido)\n"
        "• <code>.msg</code> (Broadcast Global)\n"
        "• <code>.chats</code> (Lista de Chats)\n"
        "• <code>.listdn</code> (Punições Globais)\n"
        "• <code>.logs</code> (Auditoria de Deleções)\n"
        "• <code>.id</code> | <code>.help</code>"
    )
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.antispy(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_antispy(event):
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    bait_msg = await event.respond("🕵️‍♂️ [AntiSpy] Varrendo o chat em busca de espiões... Analisando logs de moderação...")
    await asyncio.sleep(5)
    try:
        result = await client(functions.channels.GetAdminLogRequest(
            channel=event.chat_id,
            q='',
            events_filter=types.ChannelAdminLogEventsFilter(delete=True, edit=True, ban=True, unban=True, kick=True, unkick=True),
            admins=None, max_id=0, min_id=0, limit=15
        ))
        spies = set()
        for entry in result.events:
            uid = entry.user_id
            if uid and uid not in [OWNER_ID, SECOND_OWNER_ID, THIRD_OWNER_ID] and uid not in cache.authorized_users:
                spies.add(uid)
        if spies:
            spy_list = []
            for uid in spies:
                info = db.get_user_info(uid)
                db.add_detected_spy(uid, event.chat_id)
                spy_list.append(f"• {info} (<code>{uid}</code>)")
            text = "🚨 <b>ESPIÕES/BOTS DETECTADOS E SALVOS NA LISTA!</b>\n\n" + "\n".join(spy_list)
        else:
            text = "✅ <b>Nenhum espião novo detectado neste grupo.</b>"
        await bait_msg.edit(text, parse_mode='html')
        await asyncio.sleep(15)
        await bait_msg.delete()
    except ChatAdminRequiredError:
        await bait_msg.edit("❌ Erro: Preciso ser Administrador com acesso ao Log de Auditoria para detectar espiões.", parse_mode='html')
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await bait_msg.delete()
    except Exception as e:
        await bait_msg.edit(f"❌ Erro na varredura AntiSpy: {e}", parse_mode='html')
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await bait_msg.delete()
    await delete_command_safely(event)

@client.on(events.NewMessage(pattern=r'^\.listspy(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_listspy(event):
    spies = db.get_all_spies()
    if not spies:
        await reply_or_edit(event, "✅ <b>Nenhum espião registrado no banco de dados.</b>", delete_after=5)
        return
    text = "🕵️‍♂️ <b>LISTA DE ESPIÕES DETECTADOS (.listspy)</b>\n\n"
    for s in spies:
        info = db.get_user_info(s['user_id'])
        date_str = datetime.fromtimestamp(s['detected_at']).strftime('%d/%m/%Y %H:%M')
        text += f"• {info} (<code>{s['user_id']}</code>)\n└ 🕒 {date_str} | Chat: <code>{s['chat_id']}</code>\n"
    await reply_or_edit(event, text, delete_after=15)

@client.on(events.NewMessage(pattern=r'^\.delspy(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_delspy(event):
    target_id = await get_target_from_event(event)
    if not target_id:
        await reply_or_edit(event, "❌ Responda à mensagem do espião, ou digite o ID/Username após .delspy", delete_after=DEFAULT_DELETE_AFTER)
        return
    db.remove_spy(target_id)
    info = db.get_user_info(target_id)
    await reply_or_edit(event, f"✅ <b>{info} (<code>{target_id}</code>) removido da lista de espiões.</b>", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.purgeall(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_purgeall(event):
    """Apaga mensagens recentes de todos os remetentes no chat atual."""
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return

    limit, limit_error = parse_purgeall_limit(event)
    if limit_error:
        await reply_or_edit(event, limit_error, delete_after=DEFAULT_DELETE_AFTER)
        return

    status_msg = await event.respond(
        f"🧹 [PurgeAll] Apagando até {limit} mensagens recentes de todos os usuários..."
    )
    message_ids = []
    try:
        # Não usamos deleteHistory: somente os IDs coletados nesta janela
        # são removidos, mantendo o alcance previsível e reversível no código.
        scan_limit = min(PURGEALL_MAX_SCAN, limit + 2)
        async for msg in client.iter_messages(event.chat_id, limit=scan_limit):
            if msg.id in {event.id, status_msg.id}:
                continue
            message_ids.append(msg.id)
            if len(message_ids) >= limit:
                break

        deleted_count = await delete_message_ids_safely(
            event.chat_id, message_ids, batch_size=PURGEALL_BATCH_SIZE
        )
        await status_msg.edit(
            f"✅ <b>PurgeAll concluído!</b> {deleted_count} de {limit} mensagens foram apagadas.",
            parse_mode="html",
        )
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await delete_message_safely(status_msg, "status do purgeall")
    except FloodWaitError as exc:
        logger.warning("FloodWait no .purgeall por %s segundos", exc.seconds)
        await asyncio.sleep(exc.seconds)
        await delete_message_safely(status_msg, "status do purgeall")
    except Exception as exc:
        logger.error("Erro ao executar .purgeall: %s", exc)
        try:
            await status_msg.edit("❌ Não foi possível concluir o .purgeall.", parse_mode="html")
            await asyncio.sleep(DEFAULT_DELETE_AFTER)
        except Exception:
            pass
        await delete_message_safely(status_msg, "status do purgeall")

    await delete_command_safely(event)


@client.on(events.NewMessage(pattern=r'^\.purge(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_purge(event):
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    
    target_id = await get_target_from_event(event)
    limit, limit_error = parse_purge_limit(event)
    if limit_error:
        await reply_or_edit(event, limit_error, delete_after=DEFAULT_DELETE_AFTER)
        return

    if not target_id:
        await reply_or_edit(event, "❌ Responda à mensagem do usuário ou informe @username / ID junto com a quantidade. Ex: <code>.purge 10</code>", delete_after=DEFAULT_DELETE_AFTER)
        return

    info = db.get_user_info(target_id)
    status_msg = await event.respond(f"🧹 [Purge] Apagando até {limit} mensagens (qualquer tipo) de {info}...")
    
    message_ids = []
    try:
        # Primeiro coleta os IDs; depois envia a exclusão em lotes para reduzir
        # chamadas individuais sem alterar o limite de 5–100 mensagens.
        scan_limit = min(MAX_HISTORY_SCAN, limit + 2)
        async for msg in client.iter_messages(event.chat_id, limit=scan_limit, from_user=target_id):
            if msg.id != event.id:
                message_ids.append(msg.id)
                if len(message_ids) >= limit:
                    break
        deleted_count = await delete_message_ids_safely(event.chat_id, message_ids)
        await status_msg.edit(f"✅ <b>Purge concluído!</b> {deleted_count} mensagens de {info} foram apagadas.", parse_mode='html')
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit(f"❌ Erro ao executar .purge: {e}", parse_mode='html')
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await status_msg.delete()

    await delete_command_safely(event)

@client.on(events.NewMessage(pattern=r'^\.purgeme(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_purgeme(event):
    if not event.is_group and not event.is_channel:
        await reply_or_edit(event, "❌ Este comando só pode ser usado em grupos ou canais.", delete_after=DEFAULT_DELETE_AFTER)
        return
    
    limit, limit_error = parse_purge_limit(event)
    if limit_error:
        await reply_or_edit(event, limit_error, delete_after=DEFAULT_DELETE_AFTER)
        return

    status_msg = await event.respond(f"🧹 [PurgeMe] Apagando suas últimas {limit} mensagens...")
    
    me_id = event.sender_id
    message_ids = []
    try:
        scan_limit = min(MAX_HISTORY_SCAN, limit + 2)
        async for msg in client.iter_messages(event.chat_id, limit=scan_limit, from_user=me_id):
            if msg.id != status_msg.id and msg.id != event.id:
                message_ids.append(msg.id)
                if len(message_ids) >= limit:
                    break
        deleted_count = await delete_message_ids_safely(event.chat_id, message_ids)
        await status_msg.edit(f"✅ <b>PurgeMe concluído!</b> {deleted_count} mensagens suas foram apagadas.", parse_mode='html')
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit(f"❌ Erro ao executar .purgeme: {e}", parse_mode='html')
        await asyncio.sleep(DEFAULT_DELETE_AFTER)
        await status_msg.delete()

    await delete_command_safely(event)

@client.on(events.NewMessage(pattern=r'^\.id(?:\s|$)', func=lambda e: is_authorized(e.sender_id)))
async def cmd_id(event):
    target_id = await get_target_from_event(event) or event.sender_id
    await reply_or_edit(event, f"🆔 ID: <code>{target_id}</code>", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.msg(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
async def cmd_msg(event):
    reply = await event.get_reply_message()
    command_args = event.raw_text.split(maxsplit=1)
    text_arg = command_args[1] if len(command_args) > 1 else None
    if reply is None and not text_arg:
        await reply_or_edit(event, "❌ Digite a mensagem ou responda a uma mídia.", delete_after=DEFAULT_DELETE_AFTER)
        return
    chats = db.all_chats_detailed()
    success = 0
    for chat in chats:
        if chat['active'] and chat['chat_type'] not in ['private', 'User']:
            try:
                await send_broadcast_payload(chat['chat_id'], reply, text_arg)
                success += 1
                await asyncio.sleep(0.1)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as exc:
                logger.debug(f"Falha ao transmitir para {chat['chat_id']}: {exc}")
                continue
    await reply_or_edit(event, f"📢 Transmissão concluída: {success} chats receberam.", delete_after=DEFAULT_DELETE_AFTER)

@client.on(events.NewMessage(pattern=r'^\.chats(?:\s|$)', func=lambda e: is_owner(e.sender_id)))
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
    text = f"📡 <b>RELATÓRIO DE CHATS {VERSION}</b>\n\n"
    if grupos: text += "👥 <b>GRUPOS:</b>\n" + "\n".join(grupos) + "\n\n"
    if canais: text += "📣 <b>CANAIS:</b>\n" + "\n".join(canais) + "\n\n"
    if privados: text += "👤 <b>USUÁRIOS NO PRIVADO:</b>\n" + "\n".join(privados) + "\n\n"
    text += "📊 <b>RESUMO:</b>\n"
    text += f"• Grupos/Canais: {len(grupos) + len(canais)}\n• Usuários: {len(privados)}"
    await reply_or_edit(event, text, delete_after=15)

# --- INICIALIZAÇÃO ---
if __name__ == "__main__":
    cache.load_all(db.conn)
    logger.info("JTZIN USERBOT %s (STATUS E HEALTH) INICIANDO...", VERSION)
    client.start()
    logger.info("USERBOT TELETHON ONLINE!")
    client.run_until_disconnected()
