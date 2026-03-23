import asyncio
import logging
import os
import random
from datetime import datetime, timedelta

from pyrogram import Client
from pyrogram.errors import ChatWriteForbidden, FloodWait, PeerIdInvalid, UserAlreadyParticipant
from pyrogram.handlers import ChatJoinRequestHandler
from pyrogram.types import ChatJoinRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


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
        photo_path=None,
        session_file=None,
        pauses=None,
    ):
        self.task_name = task_name
        self.target_channel_id = target_channel_id
        self.report_channel_id = report_channel_id
        self.messages = messages or []
        self.photo_path = photo_path
        self.session_name = session_file if session_file else task_name

        pauses = pauses or {}
        self.first_min_seconds = int(pauses.get("first_min", 3)) * 60
        self.first_max_seconds = int(pauses.get("first_max", 10)) * 60
        self.gap_min_seconds = int(pauses.get("gap_min", 25)) * 60
        self.gap_max_seconds = int(pauses.get("gap_max", 40)) * 60

        self.request_queue = asyncio.Queue()
        self.pending_users = set()
        self.last_approval_time = None
        self.daily_count = 0
        self.is_running = False
        self.background_tasks = []

        self.app = Client(
            name=f"session_{self.session_name}",
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy,
            device_model="Desktop",
        )

        self.app.add_handler(ChatJoinRequestHandler(self.handle_join_request))

    async def resolve_peer(self, chat_id):
        if chat_id == "me":
            return True

        try:
            await self.app.get_chat(chat_id)
            return True
        except Exception:
            logger.debug("Не удалось получить чат напрямую: %s", chat_id, exc_info=True)

        try:
            async for dialog in self.app.get_dialogs(limit=3000):
                if dialog.chat.id == chat_id:
                    return True
        except Exception:
            logger.exception("Не удалось определить peer через список диалогов: %s", chat_id)

        return False

    async def send_message_safe(self, chat_id, text):
        try:
            return await self.app.send_message(chat_id, text)
        except PeerIdInvalid:
            resolved = await self.resolve_peer(chat_id)
            if resolved:
                return await self.app.send_message(chat_id, text)
            raise
        except FloodWait as exc:
            await asyncio.sleep(exc.value + 1)
            return await self.app.send_message(chat_id, text)

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
            now = datetime.now()
            next_run = (now + timedelta(days=1)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            await asyncio.sleep((next_run - now).total_seconds())

            report_date = (datetime.now() - timedelta(minutes=1)).strftime("%d.%m.%Y")
            await self.report(
                f"Отчет за {report_date}\n\nОдобрено заявок: {self.daily_count}"
            )
            self.daily_count = 0
            await asyncio.sleep(60)

    async def enqueue_request(self, chat_id, user_id, first_name):
        if user_id in self.pending_users:
            return

        self.pending_users.add(user_id)
        await self.request_queue.put((chat_id, user_id, first_name))

    async def handle_join_request(self, client, request: ChatJoinRequest):
        if request.chat.id != self.target_channel_id:
            return

        user_id = request.from_user.id
        first_name = request.from_user.first_name or "Пользователь"

        await self.enqueue_request(self.target_channel_id, user_id, first_name)
        await self.report(f"Новая заявка: {first_name}")

    async def scan_pending_requests(self, is_startup=False):
        count = 0

        try:
            async for request in self.app.get_chat_join_requests(self.target_channel_id):
                user_id = request.user.id
                first_name = request.user.first_name or "Пользователь"

                if user_id not in self.pending_users:
                    await self.enqueue_request(self.target_channel_id, user_id, first_name)
                    count += 1
        except Exception:
            logger.exception("[%s] Ошибка при сканировании заявок", self.task_name)
            return

        if count > 0:
            await self.report(f"Найдено заявок: {count}")
        elif is_startup:
            await self.report("Очередь пуста")

    async def watch_requests_loop(self):
        while self.is_running:
            try:
                async for request in self.app.get_chat_join_requests(self.target_channel_id):
                    user_id = request.user.id
                    first_name = request.user.first_name or "Пользователь"

                    if user_id not in self.pending_users:
                        await self.enqueue_request(self.target_channel_id, user_id, first_name)
                        await self.report(f"Новая заявка: {first_name}")
            except Exception:
                logger.exception("[%s] Ошибка в цикле отслеживания заявок", self.task_name)

            await asyncio.sleep(5)

    async def worker_loop(self):
        while self.is_running:
            chat_id = None
            user_id = None
            first_name = None

            try:
                chat_id, user_id, first_name = await asyncio.wait_for(
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
                    await self.report(f"{first_name}: ожидание {minutes} мин.")
                    await asyncio.sleep(wait_time)

                try:
                    await self.app.approve_chat_join_request(chat_id, user_id)
                except UserAlreadyParticipant:
                    logger.info(
                        "[%s] Пользователь уже состоит в канале: %s",
                        self.task_name,
                        user_id,
                    )
                except FloodWait as exc:
                    await asyncio.sleep(exc.value + 1)
                    await self.app.approve_chat_join_request(chat_id, user_id)

                self.last_approval_time = datetime.now()
                self.daily_count += 1

                if self.messages:
                    try:
                        text = random.choice(self.messages)
                        if self.photo_path and os.path.exists(self.photo_path):
                            await self.app.send_photo(user_id, self.photo_path, caption=text)
                        else:
                            await self.app.send_message(user_id, text)
                    except Exception:
                        logger.exception(
                            "[%s] Не удалось отправить сообщение пользователю %s",
                            self.task_name,
                            user_id,
                        )

                await self.report(f"Заявка одобрена: {first_name}")

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
        await self.app.start()

        await self.resolve_peer(self.report_channel_id)
        await self.resolve_peer(self.target_channel_id)

        try:
            await self.send_message_safe(self.report_channel_id, "Задача запущена")
        except ChatWriteForbidden:
            await self.app.stop()
            return False, "Нет прав на отправку сообщений в канал отчетов"
        except Exception:
            logger.exception("[%s] Не удалось отправить стартовое сообщение", self.task_name)
            await self.app.stop()
            return False, "Не удалось отправить сообщение о запуске"

        self.is_running = True
        self.background_tasks = [
            asyncio.create_task(self.worker_loop()),
            asyncio.create_task(self.scan_pending_requests(is_startup=True)),
            asyncio.create_task(self.watch_requests_loop()),
            asyncio.create_task(self.daily_report_loop()),
        ]

        return True, "Задача успешно запущена"

    async def stop(self):
        self.is_running = False

        for task in self.background_tasks:
            task.cancel()

        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

        self.background_tasks.clear()
        await self.app.stop()