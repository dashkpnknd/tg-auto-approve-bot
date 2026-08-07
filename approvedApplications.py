import asyncio
import contextlib
import logging
import os
import random
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import unquote, urlparse

import socks
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
MAX_PHOTO_CAPTION_LENGTH = 1024
REPORT_TIMEZONE = ZoneInfo("Europe/Moscow")


class ClientBindingStore:
    """Durable client → sender-account mapping and an idempotency ledger."""

    def __init__(self, path=None):
        self.path = path or os.path.join(BASE_DIR, "client_bindings.sqlite3")
        self.lock = threading.Lock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS client_bindings (
                    user_id INTEGER PRIMARY KEY,
                    task_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source_message_id INTEGER,
                    second_sent_at TEXT,
                    third_sent_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS manual_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, reason, resolved)
                );
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def bind_if_absent(self, user_id, task_name, message_id=None):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO client_bindings(user_id,task_name,created_at,source_message_id) VALUES(?,?,?,?)",
                (user_id, task_name, datetime.utcnow().isoformat(), message_id),
            )
            return db.execute("SELECT * FROM client_bindings WHERE user_id=?", (user_id,)).fetchone()

    def binding(self, user_id):
        with self.lock, self._connect() as db:
            return db.execute("SELECT * FROM client_bindings WHERE user_id=?", (user_id,)).fetchone()

    def mark_sent_once(self, user_id, stage):
        column = "second_sent_at" if stage == 2 else "third_sent_at"
        with self.lock, self._connect() as db:
            row = db.execute("SELECT %s FROM client_bindings WHERE user_id=?" % column, (user_id,)).fetchone()
            if not row or row[0]:
                return False
            db.execute("UPDATE client_bindings SET %s=?,last_error=NULL WHERE user_id=?" % column, (datetime.utcnow().isoformat(), user_id))
            return True

    def queue_manual(self, user_id, reason, details=""):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO manual_queue(user_id,reason,details,created_at) VALUES(?,?,?,?)",
                (user_id, reason, details, datetime.utcnow().isoformat()),
            )


binding_store = ClientBindingStore()
active_workers = {}


def resolve_photo_path(photo_path):
    if not photo_path:
        return None

    candidates = []
    if os.path.isabs(photo_path):
        candidates.append(photo_path)
    else:
        candidates.append(os.path.join(BASE_DIR, photo_path))
        candidates.append(os.path.join(BASE_DIR, "downloads", photo_path))
        candidates.append(os.path.join(BASE_DIR, "downloads", os.path.basename(photo_path)))

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    basename = os.path.basename(photo_path).casefold()
    downloads_dir = os.path.join(BASE_DIR, "downloads")
    if os.path.isdir(downloads_dir):
        for file_name in os.listdir(downloads_dir):
            if file_name.casefold() == basename:
                return os.path.join(downloads_dir, file_name)

    return candidates[0]


def normalize_pause_range(pauses, min_key, max_key, default_min, default_max):
    try:
        min_seconds = max(0, int(pauses.get(min_key, default_min))) * 60
        max_seconds = max(0, int(pauses.get(max_key, default_max))) * 60
    except (TypeError, ValueError):
        min_seconds = default_min * 60
        max_seconds = default_max * 60

    if min_seconds > max_seconds:
        min_seconds, max_seconds = max_seconds, min_seconds

    return min_seconds, max_seconds


def parse_proxy_url(value):
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme not in ("socks5", "socks4", "http", "https"):
        raise ValueError("Неподдерживаемый тип прокси")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Неверный формат прокси")

    return {
        "scheme": "http" if scheme == "https" else scheme,
        "hostname": parsed.hostname,
        "port": int(parsed.port),
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else "",
    }


def read_env_value(key):
    value = os.getenv(key)
    if value:
        return value.strip()

    if not os.path.exists(ENV_FILE):
        return None

    try:
        with open(ENV_FILE, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, raw_value = line.split("=", 1)
                if name.strip() == key:
                    return raw_value.strip().strip('"').strip("'")
    except Exception:
        logger.exception("Не удалось прочитать .env")

    return None


def load_default_proxy():
    proxy_url = read_env_value("TELETHON_PROXY_URL")
    if not proxy_url:
        return None

    try:
        return parse_proxy_url(proxy_url)
    except Exception:
        logger.exception("Неверный TELETHON_PROXY_URL в .env")
        return None


def to_telethon_proxy(proxy):
    effective_proxy = proxy if proxy is not None else load_default_proxy()
    if not effective_proxy:
        return None

    proxy_type = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
    }.get(effective_proxy.get("scheme"), socks.SOCKS5)

    username = effective_proxy.get("username")
    password = effective_proxy.get("password") or ""
    if username:
        return (
            proxy_type,
            effective_proxy["hostname"],
            int(effective_proxy["port"]),
            True,
            username,
            password,
        )

    return (proxy_type, effective_proxy["hostname"], int(effective_proxy["port"]), True)


def peer_id(value):
    if value == "me":
        return "me"

    try:
        return utils.get_peer_id(value)
    except Exception:
        pass

    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def display_name(user):
    first_name = getattr(user, "first_name", None) or ""
    last_name = getattr(user, "last_name", None) or ""
    full_name = f"{first_name} {last_name}".strip()
    return full_name or getattr(user, "username", None) or ""


def user_report_label(user_id, user=None):
    name = display_name(user) if user else ""
    username = getattr(user, "username", None) if user else None

    details = [f"ID {user_id}"]
    if username:
        details.insert(0, f"@{username}")

    if name:
        return f"{name} ({', '.join(details)})"

    return " / ".join(details)


class AutoApproveWorker:
    def __init__(
        self,
        task_name,
        api_id,
        api_hash,
        proxy,
        target_channel_id,
        report_channel_id,
        messages,
        second_messages=None,
        photo_path=None,
        session_file=None,
        pauses=None,
        telethon_session=None,
    ):
        self.task_name = task_name
        self.target_channel_id = target_channel_id
        self.report_channel_id = report_channel_id
        self.messages = messages or []
        self.second_messages = second_messages or []
        self.photo_path = resolve_photo_path(photo_path)
        self.session_name = session_file if session_file else task_name
        self.session_string = telethon_session or ""

        pauses = pauses or {}
        self.first_min_seconds, self.first_max_seconds = normalize_pause_range(
            pauses,
            "first_min",
            "first_max",
            3,
            10,
        )
        self.gap_min_seconds, self.gap_max_seconds = normalize_pause_range(
            pauses,
            "gap_min",
            "gap_max",
            25,
            40,
        )

        self.request_queue = asyncio.Queue()
        self.pending_users = set()
        self.processed_users = set()
        self.last_approval_time = None
        self.daily_count = 0
        self.known_dialog_users = set()
        self.second_messages_in_progress = set()
        self.is_running = False
        self.background_tasks = []
        self.target_input_peer = None

        self.app = TelegramClient(
            StringSession(self.session_string),
            api_id,
            api_hash,
            proxy=to_telethon_proxy(proxy),
            connection_retries=5,
            request_retries=3,
            timeout=20,
        )
        self.app.add_event_handler(self.handle_raw_update, events.Raw)
        self.app.add_event_handler(self.handle_dialog_message, events.NewMessage)

    async def handle_dialog_message(self, event):
        """Legacy one-account tasks never send a second message on a reply."""
        if not event.is_private:
            return
        try:
            peer = await event.get_chat()
            if not peer or getattr(peer, "bot", False):
                return
            user_id = event.chat_id
            if event.out:
                # Remember only the owner of the dialog for the old-task flow.
                binding_store.bind_if_absent(user_id, self.task_name, event.message.id)
        except Exception:
            logger.exception("[%s] Не удалось запомнить диалог", self.task_name)

    async def resolve_peer(self, chat_id):
        if chat_id == "me":
            return "me"

        try:
            return await self.app.get_input_entity(chat_id)
        except Exception:
            logger.debug("Не удалось получить peer напрямую: %s", chat_id, exc_info=True)

        try:
            async for dialog in self.app.iter_dialogs(limit=3000):
                if utils.get_peer_id(dialog.entity) == chat_id:
                    return await self.app.get_input_entity(dialog.entity)
        except Exception:
            logger.exception("Не удалось определить peer через список диалогов: %s", chat_id)

        return None

    async def send_message_safe(self, chat_id, text):
        try:
            return await self.app.send_message(chat_id, text)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            return await self.app.send_message(chat_id, text)

    async def send_photo_safe(self, chat_id, photo_path, caption=None):
        try:
            return await self.app.send_file(chat_id, photo_path, caption=caption)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            return await self.app.send_file(chat_id, photo_path, caption=caption)

    async def load_known_dialog_users(self):
        self.known_dialog_users.clear()

        try:
            async for dialog in self.app.iter_dialogs(limit=3000):
                entity = dialog.entity
                if isinstance(entity, types.User) and not getattr(entity, "bot", False):
                    self.known_dialog_users.add(entity.id)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            await self.load_known_dialog_users()
        except Exception:
            logger.exception("[%s] Не удалось загрузить список личных диалогов", self.task_name)

    async def has_private_dialog(self, user_id):
        if user_id in self.known_dialog_users:
            return True

        try:
            async for dialog in self.app.iter_dialogs(limit=3000):
                entity = dialog.entity
                if isinstance(entity, types.User) and not getattr(entity, "bot", False):
                    self.known_dialog_users.add(entity.id)
                    if entity.id == user_id:
                        return True
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            return await self.has_private_dialog(user_id)
        except Exception:
            logger.exception(
                "[%s] Не удалось проверить диалог с пользователем %s",
                self.task_name,
                user_id,
            )
            return True

        return False

    async def send_greeting_if_needed(self, user_id, text):
        if await self.has_private_dialog(user_id):
            logger.info(
                "[%s] Диалог с пользователем %s уже есть, сообщение не отправлено",
                self.task_name,
                user_id,
            )
            return False

        if self.photo_path and os.path.exists(self.photo_path):
            if len(text) <= MAX_PHOTO_CAPTION_LENGTH:
                await self.send_photo_safe(user_id, self.photo_path, caption=text)
            else:
                await self.send_photo_safe(user_id, self.photo_path)
                await self.send_message_safe(user_id, text)
        else:
            if self.photo_path:
                logger.warning(
                    "[%s] Фото не найдено: %s",
                    self.task_name,
                    self.photo_path,
                )
            await self.send_message_safe(user_id, text)

        self.known_dialog_users.add(user_id)
        return True

    async def report(self, text):
        logger.info("[%s] %s", self.task_name, text)

        if not self.report_channel_id:
            return

        try:
            await self.send_message_safe(self.report_channel_id, text)
        except Exception:
            logger.exception("[%s] Не удалось отправить сообщение в канал отчетов", self.task_name)

    async def daily_report_loop(self):
        while self.is_running:
            now = datetime.now(REPORT_TIMEZONE)
            next_run = (now + timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            await asyncio.sleep((next_run - now).total_seconds())

            report_date = (datetime.now(REPORT_TIMEZONE) - timedelta(days=1)).strftime("%d.%m.%Y")
            await self.report(
                "Ежедневный отчет\n"
                f"Дата: {report_date}\n"
                "Период: 00:00-23:59 МСК\n"
                f"Задача: {self.task_name}\n"
                f"Одобрено заявок: {self.daily_count}"
            )
            self.daily_count = 0
            await asyncio.sleep(60)

    async def enqueue_request(self, chat_peer, user_id, user_label):
        if user_id in self.pending_users or user_id in self.processed_users:
            return False

        self.pending_users.add(user_id)
        await self.request_queue.put((chat_peer, user_id, user_label))
        return True

    async def get_user_report_label(self, user_id):
        with contextlib.suppress(Exception):
            user = await self.app.get_entity(user_id)
            return user_report_label(user_id, user)

        return user_report_label(user_id)

    async def handle_raw_update(self, update):
        if not self.is_running:
            return

        chat_peer = None
        requester_ids = []

        if isinstance(update, types.UpdateBotChatInviteRequester):
            chat_peer = update.peer
            requester_ids = [update.user_id]
        elif isinstance(update, types.UpdatePendingJoinRequests):
            chat_peer = update.peer
            requester_ids = list(update.recent_requesters or [])
        else:
            return

        if peer_id(chat_peer) != self.target_channel_id:
            return

        for user_id in requester_ids:
            user_label = await self.get_user_report_label(user_id)

            if await self.enqueue_request(chat_peer, user_id, user_label):
                await self.report(
                    "Новая заявка\n"
                    f"Пользователь: {user_label}\n"
                    "Статус: добавлена в очередь"
                )

    async def approve_join_request(self, chat_peer, user_id):
        peer = await self.resolve_peer(peer_id(chat_peer))
        if peer is None:
            peer = self.target_input_peer
        if peer is None:
            raise RuntimeError("Не удалось определить основной канал")

        try:
            await self.app(
                functions.messages.HideChatJoinRequestRequest(
                    peer=peer,
                    user_id=user_id,
                    approved=True,
                )
            )
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 1)
            await self.app(
                functions.messages.HideChatJoinRequestRequest(
                    peer=peer,
                    user_id=user_id,
                    approved=True,
                )
            )

    async def send_third_from_bound_account(self, user_id, user_label):
        binding = binding_store.binding(user_id)
        if not binding:
            candidates = [worker for worker in active_workers.values() if user_id in worker.known_dialog_users]
            if len(candidates) == 1:
                binding = binding_store.bind_if_absent(user_id, candidates[0].task_name)
                logger.info("Клиент %s автоматически привязан к аккаунту %s по существующему диалогу", user_id, candidates[0].task_name)
            else:
                details = "нет диалога" if not candidates else "несколько аккаунтов: " + ", ".join(worker.task_name for worker in candidates)
                binding_store.queue_manual(user_id, "sender_not_bound", details)
                return "не отправлено: нет однозначной привязки к рассылочному аккаунту, передано вручную"

        sender = active_workers.get(binding["task_name"])
        if not sender or not sender.is_running:
            binding_store.queue_manual(user_id, "sender_account_unavailable", binding["task_name"])
            return "не отправлено: привязанный аккаунт недоступен, передано вручную"
        if not sender.messages:
            binding_store.queue_manual(user_id, "third_message_not_configured", binding["task_name"])
            return "не отправлено: для привязанного аккаунта не задано 3-е сообщение"
        if binding["third_sent_at"]:
            return "не отправлено: 3-е сообщение уже отправлялось"

        try:
            sent = await sender.send_greeting_if_needed(user_id, random.choice(sender.messages))
            if not sent:
                return "не отправлено: с клиентом уже есть личный диалог"
            if binding_store.mark_sent_once(user_id, 3):
                return f"3-е сообщение отправлено с аккаунта {binding['task_name']}"
            return "не отправлено: 3-е сообщение уже отправлялось"
        except Exception:
            logger.exception("[%s] Не удалось отправить 3-е сообщение пользователю %s", sender.task_name, user_id)
            binding_store.queue_manual(user_id, "third_message_error", sender.task_name)
            return "ошибка отправки, передано вручную"

    async def scan_pending_requests(self, is_startup=False):
        count = 0

        try:
            result = await self.app(
                functions.messages.GetChatInviteImportersRequest(
                    peer=self.target_input_peer,
                    requested=True,
                    offset_date=None,
                    offset_user=types.InputUserEmpty(),
                    limit=100,
                )
            )

            for importer in result.importers:
                user_id = importer.user_id
                user_label = await self.get_user_report_label(user_id)

                if await self.enqueue_request(self.target_input_peer, user_id, user_label):
                    count += 1
        except Exception:
            logger.exception("[%s] Ошибка при сканировании заявок", self.task_name)
            return

        if count > 0:
            await self.report(
                "Проверка заявок\n"
                f"Найдено новых заявок: {count}\n"
                "Статус: добавлены в очередь"
            )
        elif is_startup:
            await self.report(
                "Проверка заявок\n"
                "Статус: очередь пуста"
            )

    async def watch_requests_loop(self):
        while self.is_running:
            await self.scan_pending_requests()
            await asyncio.sleep(10)

    async def worker_loop(self):
        while self.is_running:
            chat_peer = None
            user_id = None
            user_label = None

            try:
                chat_peer, user_id, user_label = await asyncio.wait_for(
                    self.request_queue.get(),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                continue

            try:
                now = datetime.now()

                if self.last_approval_time is None:
                    wait_time = random.randint(
                        self.first_min_seconds,
                        self.first_max_seconds,
                    )
                else:
                    elapsed = (now - self.last_approval_time).total_seconds()
                    gap = random.randint(self.gap_min_seconds, self.gap_max_seconds)
                    wait_time = max(0, int(gap - elapsed))

                if wait_time > 0:
                    minutes = max(1, int(wait_time // 60))
                    await self.report(
                        "Заявка в очереди\n"
                        f"Пользователь: {user_label}\n"
                        f"Ожидание: {minutes} мин."
                    )
                    await asyncio.sleep(wait_time)

                try:
                    await self.approve_join_request(chat_peer, user_id)
                except RPCError as exc:
                    message = str(exc).lower()
                    if "already" not in message and "participant" not in message:
                        raise
                    logger.info(
                        "[%s] Пользователь уже состоит в канале: %s",
                        self.task_name,
                        user_id,
                    )

                self.last_approval_time = datetime.now()
                self.daily_count += 1
                self.processed_users.add(user_id)

                message_status = await self.send_third_from_bound_account(user_id, user_label)

                await self.report(
                    "Заявка одобрена\n"
                    f"Пользователь: {user_label}\n"
                    "Статус: принят в канал\n"
                    f"Сообщение: {message_status}"
                )

            except Exception:
                logger.exception(
                    "[%s] Ошибка при обработке пользователя %s",
                    self.task_name,
                    user_id,
                )
            finally:
                if user_id is not None:
                    self.pending_users.discard(user_id)
                self.request_queue.task_done()

    async def start(self):
        if not self.session_string:
            return False, "У задачи нет Telethon-сессии. Создайте задачу заново через QR или номер."

        try:
            await self.app.connect()
            if not await self.app.is_user_authorized():
                await self.app.disconnect()
                return False, "Сессия аккаунта не авторизована. Создайте задачу заново."
        except Exception:
            logger.exception("[%s] Не удалось запустить клиент аккаунта", self.task_name)
            return False, "Не удалось подключить аккаунт задачи. Проверьте сессию и авторизацию."

        self.target_input_peer = await self.resolve_peer(self.target_channel_id)
        if self.target_input_peer is None:
            await self.app.disconnect()
            return False, "Не удалось найти основной канал. Проверьте доступ аккаунта."

        if self.report_channel_id != "me":
            report_peer = await self.resolve_peer(self.report_channel_id)
            if report_peer is None:
                await self.app.disconnect()
                return False, "Не удалось найти чат отчетов. Проверьте доступ аккаунта."

        await self.load_known_dialog_users()

        try:
            await self.send_message_safe(self.report_channel_id, "Задача запущена")
        except Exception:
            logger.exception("[%s] Не удалось отправить стартовое сообщение", self.task_name)
            await self.app.disconnect()
            return False, "Не удалось отправить сообщение о запуске в чат отчетов"

        self.is_running = True
        active_workers[self.task_name] = self
        self.background_tasks = [
            asyncio.create_task(self.worker_loop()),
            asyncio.create_task(self.scan_pending_requests(is_startup=True)),
            asyncio.create_task(self.watch_requests_loop()),
            asyncio.create_task(self.daily_report_loop()),
        ]

        return True, "Задача успешно запущена"

    async def stop(self):
        active_workers.pop(self.task_name, None)
        self.is_running = False

        for task in self.background_tasks:
            task.cancel()

        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

        self.background_tasks.clear()
        with contextlib.suppress(Exception):
            await self.app.disconnect()
