"""Independent multi-account campaign runtime.

Legacy tasks deliberately remain in approvedApplications.py.  New campaigns use
their own account registry and ledger so a migration can never alter legacy data.
"""
import asyncio
import contextlib
import json
import logging
import os
import random
import sqlite3
import tempfile
import threading
from datetime import datetime
from uuid import uuid4

from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.extensions import html
from telethon.sessions import StringSession

from config_local import API_HASH, API_ID

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
CAMPAIGNS_FILE = os.path.join(BASE_DIR, "campaigns.json")
DB_FILE = os.path.join(BASE_DIR, "campaign_bindings.sqlite3")


def _load(path):
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Cannot load %s", path)
        return {}


def _save(path, data):
    folder = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix="data_", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(temporary)
        raise


def accounts(): return _load(ACCOUNTS_FILE)
def campaigns(): return _load(CAMPAIGNS_FILE)
def save_accounts(data): _save(ACCOUNTS_FILE, data)
def save_campaigns(data): _save(CAMPAIGNS_FILE, data)
def new_id(): return uuid4().hex[:16]


class CampaignStore:
    def __init__(self):
        self.lock = threading.Lock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bindings (
                  campaign_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                  account_id TEXT NOT NULL, source_message_id INTEGER,
                  created_at TEXT NOT NULL, second_sent_at TEXT, third_sent_at TEXT,
                  PRIMARY KEY(campaign_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS manual_queue (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
                  user_id INTEGER NOT NULL, reason TEXT NOT NULL, details TEXT,
                  created_at TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(campaign_id, user_id, reason, resolved)
                );
            """)

    def connect(self):
        db = sqlite3.connect(DB_FILE, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def binding(self, campaign_id, user_id):
        with self.lock, self.connect() as db:
            return db.execute("SELECT * FROM bindings WHERE campaign_id=? AND user_id=?", (campaign_id, user_id)).fetchone()

    def bind(self, campaign_id, user_id, account_id, message_id=None):
        with self.lock, self.connect() as db:
            db.execute("INSERT OR IGNORE INTO bindings(campaign_id,user_id,account_id,source_message_id,created_at) VALUES(?,?,?,?,?)", (campaign_id, user_id, account_id, message_id, datetime.utcnow().isoformat()))
            return db.execute("SELECT * FROM bindings WHERE campaign_id=? AND user_id=?", (campaign_id, user_id)).fetchone()

    def mark_once(self, campaign_id, user_id, stage):
        column = "second_sent_at" if stage == 2 else "third_sent_at"
        with self.lock, self.connect() as db:
            result = db.execute(f"UPDATE bindings SET {column}=? WHERE campaign_id=? AND user_id=? AND {column} IS NULL", (datetime.utcnow().isoformat(), campaign_id, user_id)).rowcount
            return result == 1

    def queue(self, campaign_id, user_id, reason, details=""):
        if not user_id:
            return
        with self.lock, self.connect() as db:
            db.execute("INSERT OR IGNORE INTO manual_queue(campaign_id,user_id,reason,details,created_at) VALUES(?,?,?,?,?)", (campaign_id, user_id, reason, details, datetime.utcnow().isoformat()))

    def pending_count(self, campaign_id):
        with self.lock, self.connect() as db:
            return db.execute("SELECT count(*) FROM manual_queue WHERE campaign_id=? AND resolved=0", (campaign_id,)).fetchone()[0]


store = CampaignStore()
active_campaigns = {}


def proxy_tuple(proxy):
    if not proxy:
        return None
    import socks
    kinds = {"socks5": socks.SOCKS5, "socks4": socks.SOCKS4, "http": socks.HTTP}
    basic = (kinds.get(proxy.get("scheme"), socks.SOCKS5), proxy["hostname"], int(proxy["port"]), True)
    return basic + (proxy["username"], proxy.get("password") or "") if proxy.get("username") else basic


def client_for(account):
    return TelegramClient(StringSession(account.get("telethon_session", "")), API_ID, API_HASH, proxy=proxy_tuple(account.get("proxy")), connection_retries=5, request_retries=3, timeout=20)


async def send_campaign_message(client, chat_id, content):
    """Send plain text or Bot API HTML captured from a forwarded message."""
    if isinstance(content, dict) and content.get("html"):
        text, entities = html.parse(content["html"])
        return await client.send_message(chat_id, text, formatting_entities=entities)
    return await client.send_message(chat_id, content)


class CampaignWorker:
    def __init__(self, campaign_id, definition):
        self.id, self.data = campaign_id, definition
        self.accounts = accounts()
        self.approver = None
        self.senders = {}
        self.running = False
        self.tasks = []
        self.queue = asyncio.Queue()
        self.pending, self.processed, self.in_progress = set(), set(), set()
        self.last_approval = None
        self.target_peer = None

    @property
    def report_id(self): return self.data.get("report_channel_id")

    async def report(self, text):
        logger.info("[campaign:%s] %s", self.id, text)
        if not self.report_id or not self.approver:
            return
        with contextlib.suppress(Exception):
            await self.approver.send_message(self.report_id, text)

    async def resolve(self, client, value):
        try:
            return await client.get_input_entity(value)
        except Exception:
            async for dialog in client.iter_dialogs(limit=500):
                if utils.get_peer_id(dialog.entity) == value:
                    return await client.get_input_entity(dialog.entity)
        return None

    async def ensure_second(self, user_id, account_id, label="", peer=None):
        binding = store.binding(self.id, user_id)
        if not binding or binding["account_id"] != account_id or binding["second_sent_at"]:
            return
        account = self.accounts.get(account_id, {})
        messages = account.get("second_messages") or []
        if not messages:
            store.queue(self.id, user_id, "second_message_not_configured", account.get("name", account_id))
            await self.report(f"Ручная очередь\nКлиент: {label or user_id}\nПричина: у аккаунта «{account.get('name', account_id)}» не задано 2-е сообщение")
            return
        if user_id in self.in_progress:
            return
        self.in_progress.add(user_id)
        try:
            pauses = self.data.get("pauses") or {}
            reply_min = max(0, int(pauses.get("reply_min", 1)))
            reply_max = max(0, int(pauses.get("reply_max", 3)))
            await asyncio.sleep(random.randint(*sorted((reply_min, reply_max))) * 60)
            await send_campaign_message(self.senders[account_id], user_id, random.choice(messages))
            with contextlib.suppress(Exception):
                await self.senders[account_id].send_read_acknowledge(peer or user_id)
            if store.mark_once(self.id, user_id, 2):
                await self.report(f"Ответ обработан\nКлиент: {label or user_id}\n2-е сообщение: отправлено с «{account.get('name', account_id)}»")
        except Exception as exc:
            store.queue(self.id, user_id, "second_message_error", str(exc)[:300])
            await self.report(f"Ручная очередь\nКлиент: {label or user_id}\nПричина: не удалось отправить 2-е сообщение")
        finally:
            self.in_progress.discard(user_id)

    def message_handler(self, account_id):
        async def handler(event):
            if not self.running or not event.is_private or event.out:
                return
            chat = await event.get_chat()
            if not chat or getattr(chat, "bot", False):
                return
            user_id = event.chat_id
            binding = store.bind(self.id, user_id, account_id, event.message.id)
            if binding["account_id"] != account_id:
                store.queue(self.id, user_id, "multiple_sender_accounts", f"{binding['account_id']}, {account_id}")
                await self.report(f"Ручная очередь\nКлиент: {getattr(chat, 'first_name', '')} ({user_id})\nПричина: ответ найден в нескольких аккаунтах")
                return
            await self.ensure_second(user_id, account_id, getattr(chat, "first_name", ""), chat)
        return handler

    async def enqueue(self, peer, user_id):
        if user_id in self.pending or user_id in self.processed:
            return
        self.pending.add(user_id)
        await self.queue.put((peer, user_id))

    async def raw_handler(self, update):
        if not self.running:
            return
        if isinstance(update, types.UpdateBotChatInviteRequester):
            peer, users = update.peer, [update.user_id]
        elif isinstance(update, types.UpdatePendingJoinRequests):
            peer, users = update.peer, update.recent_requesters or []
        else:
            return
        if utils.get_peer_id(peer) != self.data["target_channel_id"]:
            return
        for user in users: await self.enqueue(peer, user)

    async def scan_requests(self):
        try:
            result = await self.approver(functions.messages.GetChatInviteImportersRequest(peer=self.target_peer, requested=True, offset_date=None, offset_user=types.InputUserEmpty(), limit=100))
            for importer in result.importers:
                await self.enqueue(self.target_peer, importer.user_id)
        except Exception:
            logger.exception("Campaign request scan failed: %s", self.id)

    async def request_watch(self):
        while self.running:
            await self.scan_requests()
            await asyncio.sleep(12)

    async def approve(self, peer, user_id):
        await self.approver(functions.messages.HideChatJoinRequestRequest(peer=peer, user_id=user_id, approved=True))

    async def send_third(self, user_id):
        binding = store.binding(self.id, user_id)
        if not binding:
            store.queue(self.id, user_id, "sender_not_bound")
            return "не отправлено: нет привязанного рассылочного аккаунта"
        account_id = binding["account_id"]
        account = self.accounts.get(account_id)
        sender = self.senders.get(account_id)
        if not account or not sender:
            store.queue(self.id, user_id, "sender_unavailable", account_id)
            return "не отправлено: привязанный аккаунт недоступен"
        messages = account.get("third_messages") or []
        if not messages:
            store.queue(self.id, user_id, "third_message_not_configured", account.get("name", account_id))
            return "не отправлено: не задано 3-е сообщение"
        if binding["third_sent_at"]:
            return "не отправлено: 3-е сообщение уже отправлено"
        try:
            pauses = self.data.get("pauses") or {}
            third_min = max(0, int(pauses.get("third_min", 1)))
            third_max = max(0, int(pauses.get("third_max", 3)))
            await asyncio.sleep(random.randint(*sorted((third_min, third_max))) * 60)
            await send_campaign_message(sender, user_id, random.choice(messages))
            with contextlib.suppress(Exception):
                await sender.send_read_acknowledge(user_id)
            return f"отправлено с «{account.get('name', account_id)}»" if store.mark_once(self.id, user_id, 3) else "не отправлено: уже было отправлено"
        except Exception as exc:
            store.queue(self.id, user_id, "third_message_error", str(exc)[:300])
            return "ошибка отправки, передано вручную"

    async def work(self):
        pauses = self.data.get("pauses") or {}
        first = (int(pauses.get("first_min", 0)) * 60, int(pauses.get("first_max", 0)) * 60)
        gap = (int(pauses.get("gap_min", 0)) * 60, int(pauses.get("gap_max", 0)) * 60)
        while self.running:
            try: peer, user_id = await asyncio.wait_for(self.queue.get(), 5)
            except asyncio.TimeoutError: continue
            try:
                wait = random.randint(*sorted(first)) if self.last_approval is None else max(0, random.randint(*sorted(gap)) - (datetime.utcnow()-self.last_approval).total_seconds())
                if wait: await asyncio.sleep(wait)
                await self.approve(peer, user_id)
                self.last_approval = datetime.utcnow(); self.processed.add(user_id)
                await self.report(f"Заявка одобрена\nКлиент: {user_id}\n3-е сообщение: {await self.send_third(user_id)}")
            except RPCError as exc:
                store.queue(self.id, user_id, "approval_error", str(exc)[:300])
                await self.report(f"Ручная очередь\nКлиент: {user_id}\nПричина: заявка не одобрена")
            except Exception:
                logger.exception("Campaign worker failed")
            finally:
                self.pending.discard(user_id); self.queue.task_done()

    async def start(self):
        approver_id = self.data.get("approver_account_id")
        approver_data = self.accounts.get(approver_id)
        sender_ids = self.data.get("sender_account_ids") or []
        if not approver_data or not approver_data.get("enabled", True): return False, "Аккаунт-одобритель недоступен или выключен"
        if not sender_ids: return False, "Не выбраны аккаунты рассылки"
        try:
            self.approver = client_for(approver_data); await self.approver.connect()
            if not await self.approver.is_user_authorized(): return False, "Сессия одобрителя не авторизована"
            for account_id in sender_ids:
                account = self.accounts.get(account_id)
                if not account or not account.get("enabled", True): continue
                # One account may both approve and conduct conversations. Reuse its
                # connection instead of opening two competing sessions.
                client = self.approver if account_id == approver_id else client_for(account)
                if client is not self.approver: await client.connect()
                if await client.is_user_authorized():
                    client.add_event_handler(self.message_handler(account_id), events.NewMessage)
                    self.senders[account_id] = client
                elif client is not self.approver: await client.disconnect()
            if not self.senders: return False, "Нет доступных включенных аккаунтов рассылки"
            self.target_peer = await self.resolve(self.approver, self.data["target_channel_id"])
            if not self.target_peer: return False, "Одобритель не видит канал кампании"
            self.approver.add_event_handler(self.raw_handler, events.Raw)
            self.running = True; active_campaigns[self.id] = self
            self.tasks = [asyncio.create_task(self.work()), asyncio.create_task(self.request_watch())]
            await self.report(f"Кампания «{self.data.get('name', self.id)}» запущена\nАккаунтов рассылки: {len(self.senders)}")
            return True, "Кампания запущена"
        except Exception as exc:
            logger.exception("Campaign start failed")
            await self.stop(); return False, f"Ошибка подключения: {exc}"

    async def stop(self):
        active_campaigns.pop(self.id, None); self.running = False
        for task in self.tasks: task.cancel()
        if self.tasks: await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        for client in list(self.senders.values()) + ([self.approver] if self.approver else []):
            with contextlib.suppress(Exception): await client.disconnect()
        self.senders = {}; self.approver = None

    async def process_old_replies(self, limit=200):
        found = 0
        for account_id, client in self.senders.items():
            async for dialog in client.iter_dialogs(limit=limit):
                if found >= limit: break
                entity = dialog.entity
                if not isinstance(entity, types.User) or getattr(entity, "bot", False) or not dialog.unread_count: continue
                user_id = entity.id
                binding = store.bind(self.id, user_id, account_id)
                if binding["account_id"] != account_id:
                    store.queue(self.id, user_id, "multiple_sender_accounts", "old reply scan")
                    continue
                await self.ensure_second(user_id, account_id, getattr(entity, "first_name", ""), entity); found += 1
        await self.report(f"Разовая обработка ответов завершена\nПроверено непрочитанных диалогов: {found}")

    async def process_old_requests(self):
        try:
            result = await self.approver(functions.messages.GetChatInviteImportersRequest(peer=self.target_peer, requested=True, offset_date=None, offset_user=types.InputUserEmpty(), limit=100))
        except Exception as exc:
            return await self.report(f"Не удалось получить старые заявки: {exc}")
        for importer in result.importers:
            candidates = []
            for account_id, client in self.senders.items():
                # Only an actual private dialog proves the source account. A cached
                # contact alone must not silently move a client between accounts.
                try:
                    async for dialog in client.iter_dialogs(limit=3000):
                        if isinstance(dialog.entity, types.User) and dialog.entity.id == importer.user_id:
                            candidates.append(account_id)
                            break
                except Exception:
                    logger.exception("Cannot inspect dialogs for old request")
            if len(candidates) != 1:
                store.queue(self.id, importer.user_id, "sender_not_unique", ",".join(candidates) or "not found")
                continue
            store.bind(self.id, importer.user_id, candidates[0]); await self.enqueue(self.target_peer, importer.user_id)
        await self.report("Старые заявки добавлены в очередь. Неоднозначные случаи переданы вручную.")
