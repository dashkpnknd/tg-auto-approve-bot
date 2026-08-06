import asyncio
import contextlib
import json
import logging
import os
import tempfile
from urllib.parse import unquote, urlparse

import qrcode
import socks
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient, functions, types, utils
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from approvedApplications import AutoApproveWorker
from campaign_engine import (
    CampaignWorker, accounts as load_accounts, campaigns as load_campaigns,
    save_accounts, save_campaigns, active_campaigns, store as campaign_store,
    new_id as new_campaign_id,
)
from config_local import API_HASH, API_ID, BOT_TOKEN, TASKS_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
ENV_FILE = os.path.join(BASE_DIR, ".env")
TELEGRAM_TIMEOUT_SECONDS = 45
QR_LOGIN_TIMEOUT_SECONDS = 120

admin_bot = Bot(BOT_TOKEN)
dp = Dispatcher()

active_tasks = {}
user_states = {}
temp_data = {}
temp_clients = {}
qr_login_flows = {}


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def load_config():
    if not os.path.exists(TASKS_FILE):
        save_config({})
        return {}

    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("Не удалось загрузить файл конфигурации: %s", TASKS_FILE)
        return {}


def save_config(data):
    directory = os.path.dirname(os.path.abspath(TASKS_FILE)) or "."
    fd, temp_path = tempfile.mkstemp(prefix="tasks_", suffix=".json", dir=directory)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(data, temp_file, indent=4, ensure_ascii=False)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, TASKS_FILE)
    except Exception:
        logger.exception("Не удалось сохранить файл конфигурации")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            logger.warning("Не удалось удалить временный файл конфигурации: %s", temp_path)
        raise


def set_task_enabled(task_name, enabled):
    config = load_config()
    if task_name not in config:
        return

    config[task_name]["enabled"] = enabled
    save_config(config)


def get_unique_sessions():
    config = load_config()
    sessions = {}

    for task_name, data in config.items():
        session_key = data.get("session_file", task_name)
        if session_key not in sessions:
            sessions[session_key] = data

    return sessions


def sanitize_task_name(value: str) -> str:
    allowed = []
    for char in value.strip():
        if char.isalnum() or char in ("_", "-", "."):
            allowed.append(char)
        elif char == " ":
            allowed.append("_")

    result = "".join(allowed).strip("._-")
    return result[:50]


def parse_proxy(text):
    value = text.strip()
    if "://" in value:
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

    parts = value.split(":")
    if len(parts) not in (3, 5):
        raise ValueError("Неверный формат прокси")

    scheme = parts[0].lower()
    if scheme not in ("socks5", "socks4", "http", "https"):
        raise ValueError("Неподдерживаемый тип прокси")

    proxy = {
        "scheme": "http" if scheme == "https" else scheme,
        "hostname": parts[1],
        "port": int(parts[2]),
        "username": None,
        "password": "",
    }

    if len(parts) == 5:
        proxy["username"] = parts[3]
        proxy["password"] = parts[4]

    return proxy


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
        return parse_proxy(proxy_url)
    except Exception:
        logger.exception("Неверный TELETHON_PROXY_URL в .env")
        return None


def proxy_format_hint():
    return (
        "Форматы прокси:\n"
        "http://user:pass@ip:port\n"
        "http:ip:port:user:pass\n"
        "socks5:ip:port:user:pass\n"
        "Если прокси без логина: http:ip:port"
    )


def redact_proxy(proxy):
    if not proxy:
        return "без прокси"

    auth = " с логином" if proxy.get("username") else ""
    return f"{proxy.get('scheme')}://{proxy.get('hostname')}:{proxy.get('port')}{auth}"


def describe_route(proxy):
    if proxy is False:
        return "без прокси"
    if proxy:
        return redact_proxy(proxy)

    default_proxy = load_default_proxy()
    if default_proxy:
        return f"серверный прокси ({redact_proxy(default_proxy)})"

    return "без прокси"


def to_telethon_proxy(proxy):
    if proxy is False:
        return None
    if not proxy:
        return None

    proxy_type = {
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
        "http": socks.HTTP,
    }.get(proxy.get("scheme"), socks.SOCKS5)

    username = proxy.get("username")
    password = proxy.get("password") or ""
    if username:
        return (
            proxy_type,
            proxy["hostname"],
            int(proxy["port"]),
            True,
            username,
            password,
        )

    return (proxy_type, proxy["hostname"], int(proxy["port"]), True)


def create_user_client(proxy=None, session_string=""):
    effective_proxy = proxy if proxy is not None else load_default_proxy()
    return TelegramClient(
        StringSession(session_string or ""),
        API_ID,
        API_HASH,
        proxy=to_telethon_proxy(effective_proxy),
        connection_retries=5,
        request_retries=3,
        timeout=20,
    )


def get_auth_method_markup():
    return markup([
        [button("Войти по QR", "auth_qr")],
        [button("Войти по номеру", "auth_phone")],
    ])


def get_proxy_markup():
    label = "Серверный прокси" if load_default_proxy() else "Без прокси"
    return markup([[button(label, "proxy_no")]])


async def show_auth_method_menu(message: Message, user_id):
    user_states[user_id] = "SELECT_AUTH_METHOD"
    proxy = temp_data.get(user_id, {}).get("proxy")
    await message.answer(
        "Выберите способ авторизации аккаунта:\n\n"
        f"Подключение: {describe_route(proxy)}",
        reply_markup=get_auth_method_markup(),
    )


def create_qr_login_image(login_url, user_id):
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    path = os.path.join(DOWNLOADS_DIR, f"qr_login_{user_id}.png")

    image = qrcode.make(login_url)
    image.save(path)
    return path


async def cleanup_qr_login_flow(user_id: int):
    flow = qr_login_flows.pop(user_id, None)
    if not flow:
        return

    task = flow.get("task")
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    client_acc = flow.get("client")
    if client_acc and client_acc not in temp_clients.values():
        with contextlib.suppress(Exception):
            await client_acc.disconnect()


async def cleanup_user(user_id: int):
    await cleanup_qr_login_flow(user_id)
    user_states.pop(user_id, None)
    temp_data.pop(user_id, None)

    if user_id in temp_clients:
        try:
            client_acc = temp_clients[user_id]
            if isinstance(client_acc, TelegramClient):
                await client_acc.disconnect()
            elif getattr(client_acc, "is_initialized", False):
                await client_acc.stop()
            elif getattr(client_acc, "is_connected", False):
                await client_acc.disconnect()
        except Exception:
            logger.exception("Не удалось отключить временный клиент для пользователя %s", user_id)
        finally:
            temp_clients.pop(user_id, None)


async def save_new_account(user_id, message):
    """Persist a completed account-login flow without coupling it to a task."""
    data = temp_data.get(user_id, {})
    client = temp_clients.get(user_id)
    if not client:
        await cleanup_user(user_id)
        await message.answer("Данные подключения потеряны. Начните добавление аккаунта снова.")
        return
    try:
        profile = await client.get_me()
        full_name = " ".join(part for part in [getattr(profile, "first_name", ""), getattr(profile, "last_name", "")] if part).strip()
        username = getattr(profile, "username", None)
        base_name = full_name or username or f"ID {profile.id}"
        account_name = f"{base_name} (@{username})" if username else f"{base_name} (без username)"
    except Exception:
        await cleanup_user(user_id)
        await message.answer("Не удалось получить профиль Telegram. Подключите аккаунт ещё раз.")
        return
    registry = load_accounts()
    account_id = data.get("account_id") or new_campaign_id()
    registry[account_id] = {
        "id": account_id, "name": account_name,
        "profile_name": base_name, "username": username,
        "telethon_session": client.session.save(), "proxy": data.get("proxy"),
        "enabled": True, "second_messages": [], "third_messages": [],
        "created_at": __import__("datetime").datetime.utcnow().isoformat(),
    }
    save_accounts(registry)
    # QR completion runs inside its own waiter; do not make cleanup cancel itself.
    flow = qr_login_flows.get(user_id)
    if flow and flow.get("task") is asyncio.current_task():
        qr_login_flows.pop(user_id, None)
    await cleanup_user(user_id)
    await message.answer(f"Аккаунт «{account_name}» подключён. Настройте 2-е и 3-е сообщения в его карточке.")


async def after_authorized(user_id, message):
    if temp_data.get(user_id, {}).get("flow") == "account":
        await save_new_account(user_id, message)
    else:
        await list_channels(user_id, message, "target")


async def show_accounts_menu(obj):
    registry = load_accounts()
    rows = [[button("Подключить аккаунт", "account_add")]]
    for account_id, account in registry.items():
        status = "🟢" if account.get("enabled", True) else "⚪️"
        rows.append([button(f"{status} {account.get('name', account_id)}", f"account_view_{account_id}")])
    rows.append([button("Назад", "back_main")])
    text = f"Аккаунты\nПодключено: {len(registry)}\n🟢 включен · ⚪️ выключен"
    if isinstance(obj, CallbackQuery): await obj.message.edit_text(text, reply_markup=markup(rows))
    else: await obj.answer(text, reply_markup=markup(rows))


async def show_campaigns_menu(obj):
    data = load_campaigns()
    rows = [[button("Создать кампанию", "campaign_add")]]
    for campaign_id, campaign in data.items():
        status = "🟢" if campaign_id in active_campaigns else "🔴"
        rows.append([button(f"{status} {campaign.get('name', campaign_id)}", f"campaign_view_{campaign_id}")])
    rows.append([button("Назад", "back_main")])
    text = f"Кампании\nВсего: {len(data)} · Активно: {len(active_campaigns)}"
    if isinstance(obj, CallbackQuery): await obj.message.edit_text(text, reply_markup=markup(rows))
    else: await obj.answer(text, reply_markup=markup(rows))


def account_selection_rows(prefix, selected=None, include_done=False):
    rows = []
    for account_id, account in load_accounts().items():
        if not account.get("enabled", True): continue
        tick = "✓ " if selected and account_id in selected else ""
        rows.append([button(f"{tick}{account.get('name', account_id)}", f"{prefix}{account_id}")])
    if include_done: rows.append([button("Готово", "campaign_senders_done")])
    return rows


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await cleanup_user(message.from_user.id)
    await show_main_menu(message)


async def show_main_menu(message_or_callback):
    config = load_config()
    rows = [
        [button("Аккаунты", "accounts_menu"), button("Кампании", "campaigns_menu")],
        [button("Старые задачи", "legacy_menu")],
        [button("Создать задачу: 1 аккаунт + 1 канал", "add_new_task")],
    ]

    for task_name in config.keys():
        status = "🟢" if task_name in active_tasks else "🔴"
        rows.append([button(f"{status} {task_name}", f"view_{task_name}")])

    rows.append([button("Обновить список", "refresh")])
    text = f"Панель управления\nСтарых задач активно: {len(active_tasks)}\nКампаний активно: {len(active_campaigns)}"

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=markup(rows))
    else:
        await message_or_callback.answer(text, reply_markup=markup(rows))


@dp.callback_query(F.data == "add_new_task")
async def start_add_task(callback: CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id] = "WAIT_NAME"
    temp_data[user_id] = {
        "api_id": API_ID,
        "api_hash": API_HASH,
    }
    await callback.answer()
    await callback.message.edit_text("Шаг 1\n\nОтправьте название задачи одним сообщением.")


@dp.message(F.text)
async def process_wizard(message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    text = (message.text or "").strip()

    if not state or text.startswith("/"):
        return

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await message.answer("Временные данные были очищены. Начните заново через /start")
        return

    if state == "ACCOUNT_PROXY":
        try:
            # False is an explicit direct route. None retains legacy behaviour
            # where an old single-account task may use the server default.
            temp_data[user_id]["proxy"] = False if text.lower() in ("нет", "no", "-") else parse_proxy(text)
        except Exception:
            await message.answer("Неверный прокси. Повторите ввод или отправьте «нет».")
            return
        user_states[user_id] = "ACCOUNT_AUTH"
        await message.answer("Выберите способ подключения:", reply_markup=markup([
            [button("Войти по QR", "account_auth_qr")],
            [button("Войти по номеру", "account_auth_phone")],
            [button("Вставить готовую сессию", "account_auth_session")],
        ]))

    elif state == "ACCOUNT_SESSION":
        session = text.strip()
        if len(session) < 20:
            await message.answer("Похоже, строка сессии неполная. Вставьте её целиком.")
            return
        client = create_user_client(proxy=temp_data[user_id].get("proxy"), session_string=session)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect(); await message.answer("Эта сессия не авторизована."); return
            temp_clients[user_id] = client
            await save_new_account(user_id, message)
        except Exception:
            with contextlib.suppress(Exception): await client.disconnect()
            await message.answer("Не удалось проверить сессию. Проверьте строку и прокси.")

    elif state in ("ACCOUNT_SECOND", "ACCOUNT_THIRD"):
        # A forwarded Telegram message keeps quotes, links and formatting as HTML.
        # Plain text remains available for quick variants separated by '|'.
        if getattr(message, "forward_origin", None) and message.html_text:
            variants = [{"html": message.html_text}]
        else:
            variants = [part.strip() for part in text.split("|") if part.strip()]
        if not variants:
            await message.answer("Укажите хотя бы один вариант текста."); return
        registry = load_accounts(); account_id = temp_data[user_id].get("account_id")
        if account_id not in registry:
            await message.answer("Аккаунт не найден."); return
        registry[account_id]["second_messages" if state == "ACCOUNT_SECOND" else "third_messages"] = variants
        save_accounts(registry); await cleanup_user(user_id)
        await message.answer("Сообщение сохранено.")

    elif state == "ACCOUNT_EDIT_PROXY":
        account_id = temp_data[user_id].get("account_id"); registry = load_accounts()
        if account_id not in registry:
            await cleanup_user(user_id); await message.answer("Аккаунт не найден."); return
        try:
            registry[account_id]["proxy"] = None if text.lower() in ("нет", "no", "-") else parse_proxy(text)
        except Exception:
            await message.answer("Неверный формат прокси. Повторите ввод."); return
        save_accounts(registry); await cleanup_user(user_id)
        await message.answer("Прокси сохранён. Он будет применён при следующем запуске кампании.")

    elif state == "CAMPAIGN_NAME":
        if not text or len(text) > 80:
            await message.answer("Укажите название кампании до 80 символов."); return
        temp_data[user_id]["campaign_name"] = text
        user_states[user_id] = "CAMPAIGN_TARGET"
        await message.answer("Отправьте ID или ссылку на новый канал с заявками.")

    elif state == "CAMPAIGN_TARGET":
        temp_data[user_id]["target_channel_id"] = text
        user_states[user_id] = "CAMPAIGN_REPORT"
        await message.answer("Отправьте ID или ссылку на канал отчётов (либо «me» для Избранного).")

    elif state == "CAMPAIGN_REPORT":
        temp_data[user_id]["report_channel_id"] = text
        user_states[user_id] = "CAMPAIGN_PAUSES"
        await message.answer("Укажите паузы одобрения: первая|между заявками, например 1-3|25-40. Или «по умолчанию».\n\nСообщения после ответа и после одобрения уходят с паузой 1–3 минуты.")

    elif state == "CAMPAIGN_PAUSES":
        normalized = text.lower().replace(" ", "")
        try:
            if normalized in ("поумолчанию", "default", ""):
                pauses = {"reply_min": 1, "reply_max": 3, "third_min": 1, "third_max": 3, "first_min": 1, "first_max": 3, "gap_min": 25, "gap_max": 40}
            else:
                a, b = normalized.split("|", 1); amin, amax = a.split("-", 1); bmin, bmax = b.split("-", 1)
                pauses = {"reply_min": 1, "reply_max": 3, "third_min": 1, "third_max": 3, "first_min": int(amin), "first_max": int(amax), "gap_min": int(bmin), "gap_max": int(bmax)}
                if min(pauses.values()) < 0: raise ValueError
        except Exception:
            await message.answer("Неверный формат. Пример: 1-3|25-40."); return
        temp_data[user_id]["pauses"] = pauses
        user_states[user_id] = "CAMPAIGN_APPROVER"
        rows = account_selection_rows("campaign_approver_")
        await message.answer("Выберите один аккаунт-одобритель:", reply_markup=markup(rows or [[button("Нет включенных аккаунтов", "accounts_menu")]]))

    elif state == "WAIT_NAME":
        config = load_config()
        task_name = sanitize_task_name(text)

        if not task_name:
            await message.answer("Некорректное название задачи. Используйте буквы, цифры, дефис или нижнее подчеркивание.")
            return

        if task_name in config:
            await message.answer("Такое название уже занято. Отправьте другое.")
            return

        temp_data[user_id]["name"] = task_name
        sessions = get_unique_sessions()
        rows = [[button("Подключить новый аккаунт", "acc_new")]]

        for session_name in sessions.keys():
            rows.append([button(f"Аккаунт: {session_name}", f"acc_use_{session_name}")])

        user_states[user_id] = "SELECT_ACCOUNT"
        await message.answer("Шаг 2\nВыберите аккаунт", reply_markup=markup(rows))

    elif state == "WAIT_PROXY":
        proxy = None

        if text.lower() not in ["нет", "-", "no"]:
            try:
                proxy = parse_proxy(text)
            except Exception:
                await message.answer(
                    "Неверный формат прокси.\n\n"
                    f"{proxy_format_hint()}\n\n"
                    "Или нажмите кнопку ниже, чтобы использовать серверный маршрут.",
                    reply_markup=get_proxy_markup(),
                )
                return

        temp_data[user_id]["proxy"] = proxy
        temp_data[user_id]["session_file"] = temp_data[user_id]["name"]
        await show_auth_method_menu(message, user_id)

    elif state == "WAIT_PHONE":
        client_acc = temp_clients.get(user_id)
        if not client_acc:
            await message.answer("Временный клиент аккаунта не найден. Начните заново.")
            return

        phone = text.replace(" ", "").replace("-", "")
        if not phone.startswith("+"):
            phone = "+" + phone

        temp_data[user_id]["phone"] = phone

        try:
            sent = await asyncio.wait_for(
                client_acc.send_code_request(phone),
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
            temp_data[user_id]["phone_hash"] = sent.phone_code_hash
            user_states[user_id] = "WAIT_CODE"
            await message.answer("Код отправлен. Введите код:")
        except asyncio.TimeoutError:
            await message.answer("Telegram долго не отвечает. Попробуйте еще раз или подключите аккаунт через прокси.")
        except Exception:
            logger.exception("Не удалось отправить код для пользователя %s", user_id)
            await message.answer("Не удалось отправить код подтверждения.")

    elif state == "WAIT_CODE":
        client_acc = temp_clients.get(user_id)
        if not client_acc:
            await message.answer("Временный клиент аккаунта не найден. Начните заново.")
            return

        try:
            await asyncio.wait_for(
                client_acc.sign_in(
                    phone=temp_data[user_id]["phone"],
                    code=text.strip(),
                    phone_code_hash=temp_data[user_id]["phone_hash"],
                ),
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
            temp_data[user_id]["telethon_session"] = client_acc.session.save()
            await after_authorized(user_id, message)
        except SessionPasswordNeededError:
            user_states[user_id] = "WAIT_PASSWORD"
            await message.answer("Введите пароль двухфакторной защиты:")
        except asyncio.TimeoutError:
            await message.answer("Telegram долго не отвечает. Отправьте код еще раз.")
        except Exception:
            logger.exception("Не удалось выполнить вход для пользователя %s", user_id)
            await message.answer("Не удалось выполнить вход в аккаунт.")

    elif state == "WAIT_PASSWORD":
        client_acc = temp_clients.get(user_id)
        if not client_acc:
            await message.answer("Временный клиент аккаунта не найден. Начните заново.")
            return

        try:
            await asyncio.wait_for(
                client_acc.sign_in(password=text),
                timeout=TELEGRAM_TIMEOUT_SECONDS,
            )
            temp_data[user_id]["telethon_session"] = client_acc.session.save()
            await after_authorized(user_id, message)
        except asyncio.TimeoutError:
            await message.answer("Telegram долго не отвечает. Отправьте пароль еще раз.")
        except Exception:
            logger.exception("Не удалось проверить пароль для пользователя %s", user_id)
            await message.answer("Не удалось подтвердить пароль.")

    elif state == "WAIT_TARGET_MANUAL":
        await find_and_set_channel(user_id, message, text, "target_channel_id")

    elif state == "WAIT_REPORT_MANUAL":
        await find_and_set_channel(user_id, message, text, "report_channel_id")

    elif state in ("WAIT_SECOND_MSG", "WAIT_THIRD_MSG"):
        messages = [item.strip() for item in text.split("|") if item.strip()]
        task_name = temp_data[user_id].get("editing_message_task")
        message_key = "second_messages" if state == "WAIT_SECOND_MSG" else "messages"
        message_number = "2-е" if state == "WAIT_SECOND_MSG" else "3-е"
        config = load_config()
        if not messages:
            await message.answer("Укажите хотя бы один вариант. Разделитель вариантов: |")
            return
        if not task_name or task_name not in config:
            await cleanup_user(user_id)
            await message.answer("Задача не найдена. Откройте её карточку ещё раз.")
            return
        config[task_name][message_key] = messages
        save_config(config)
        await cleanup_user(user_id)
        await message.answer(f"✅ {message_number} сообщение сохранено для задачи «{task_name}». Вариантов: {len(messages)}")

    elif state == "WAIT_MSG":
        messages = [item.strip() for item in text.split("|") if item.strip()]
        if not messages:
            await message.answer("Нужно указать хотя бы одно сообщение. Разделитель: |")
            return

        temp_data[user_id]["messages"] = messages
        user_states[user_id] = "WAIT_PAUSES"
        await message.answer(
            "Шаг 6\nУкажите паузы в минутах\n"
            "Формат: 3-10|25-40\n"
            "первая задержка | интервал между одобрениями\n"
            "Или отправьте: по умолчанию"
        )

    elif state == "WAIT_PAUSES":
        normalized = text.lower().replace(" ", "")

        if normalized in ["default", "поумолчанию", "по_умолчанию", "стандарт", ""]:
            temp_data[user_id]["pauses"] = {
                "first_min": 3,
                "first_max": 10,
                "gap_min": 25,
                "gap_max": 40,
            }
        else:
            try:
                first_part, gap_part = normalized.split("|", 1)
                first_min, first_max = first_part.split("-", 1)
                gap_min, gap_max = gap_part.split("-", 1)
                temp_data[user_id]["pauses"] = {
                    "first_min": int(first_min),
                    "first_max": int(first_max),
                    "gap_min": int(gap_min),
                    "gap_max": int(gap_max),
                }
            except Exception:
                await message.answer("Неверный формат. Пример: 3-10|25-40 или по умолчанию.")
                return

        user_states[user_id] = "WAIT_PHOTO"
        await message.answer("Шаг 7\nОтправьте фото или напишите 'нет'.")

    elif state == "WAIT_PHOTO":
        if text.lower() in ["нет", "-", "no"]:
            await finish_wizard(message, None)


@dp.callback_query(F.data.startswith("acc_"))
async def handle_account_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await callback.message.edit_text("Временные данные были очищены. Начните заново через /start")
        return

    await callback.answer()

    if data == "acc_new":
        user_states[user_id] = "WAIT_PROXY"
        await callback.message.edit_text(
            "Сначала выберите подключение к Telegram.\n\n"
            "Введите прокси или нажмите кнопку ниже для серверного маршрута.\n"
            "После этого появится выбор: QR или номер телефона.\n\n"
            f"{proxy_format_hint()}",
            reply_markup=get_proxy_markup(),
        )

    elif data.startswith("acc_use_"):
        session_name = data.split("acc_use_", 1)[1]
        old_data = get_unique_sessions().get(session_name)

        if not old_data:
            await callback.message.edit_text("Сохраненная сессия не найдена.")
            return

        temp_data[user_id]["session_file"] = session_name
        temp_data[user_id]["proxy"] = old_data.get("proxy")
        temp_data[user_id]["telethon_session"] = old_data.get("telethon_session")
        await callback.message.edit_text(f"Выбран аккаунт: {session_name}. Подключение...")

        if not old_data.get("telethon_session"):
            await callback.message.answer(
                "Этот аккаунт сохранен в старом формате. Создайте задачу заново через QR или номер."
            )
            return

        new_client = create_user_client(
            proxy=old_data.get("proxy"),
            session_string=old_data.get("telethon_session"),
        )

        try:
            await asyncio.wait_for(new_client.connect(), timeout=TELEGRAM_TIMEOUT_SECONDS)
            if not await new_client.is_user_authorized():
                await new_client.disconnect()
                await callback.message.answer("Сессия аккаунта не авторизована. Подключите аккаунт заново.")
                return

            temp_clients[user_id] = new_client
            await list_channels(user_id, callback, "target")
        except Exception:
            logger.exception("Не удалось подключить сохраненную сессию %s", session_name)
            await callback.message.answer("Не удалось подключить выбранный аккаунт.")


@dp.callback_query(F.data == "proxy_no")
async def handle_no_proxy(callback: CallbackQuery):
    user_id = callback.from_user.id

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await callback.message.edit_text("Временные данные были очищены. Начните заново через /start")
        return

    temp_data[user_id]["proxy"] = None
    temp_data[user_id]["session_file"] = temp_data[user_id]["name"]
    await callback.answer("Серверный маршрут")
    await show_auth_method_menu(callback.message, user_id)


@dp.callback_query(F.data.startswith("auth_"))
async def handle_auth_selection(callback: CallbackQuery):
    user_id = callback.from_user.id

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await callback.message.edit_text("Временные данные были очищены. Начните заново через /start")
        return

    await callback.answer("Готово")

    if callback.data == "auth_phone":
        await connect_and_request_phone(callback.message, user_id)
    elif callback.data == "auth_qr":
        await start_qr_login(callback.message, user_id)


@dp.callback_query(F.data.startswith("sel_"))
async def handle_channel_selection(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await callback.message.edit_text("Временные данные были очищены. Начните заново через /start")
        return

    if data.startswith("sel_target_"):
        if data.endswith("manual"):
            user_states[user_id] = "WAIT_TARGET_MANUAL"
            await callback.message.edit_text("Отправьте ID или ссылку на основной канал:")
        else:
            temp_data[user_id]["target_channel_id"] = int(data.split("_")[2])
            await callback.answer("Готово")
            await list_channels(user_id, callback, "report")

    elif data.startswith("sel_report_"):
        if data.endswith("manual"):
            user_states[user_id] = "WAIT_REPORT_MANUAL"
            await callback.message.edit_text("Отправьте ID или ссылку на канал отчетов:")
            return

        chat_id_raw = data.split("_", 2)[2]
        chat_id = "me" if chat_id_raw == "me" else int(chat_id_raw)
        client_acc = temp_clients.get(user_id)

        if not client_acc:
            await callback.message.edit_text("Временный клиент аккаунта не найден. Начните заново.")
            return

        await callback.message.edit_text("Проверяю доступ на отправку сообщений...")

        try:
            test_message = await client_acc.send_message(chat_id, "Проверка доступа")
            await test_message.delete()
            temp_data[user_id]["report_channel_id"] = chat_id
            await callback.answer("Готово")
            user_states[user_id] = "WAIT_MSG"
            await callback.message.edit_text("Шаг 5\nВведите текст сообщения. Разделитель: |")
        except Exception:
            logger.exception("Не удалось проверить право записи для пользователя %s", user_id)
            await callback.message.edit_text("Нет доступа на отправку сообщений в выбранный чат.")
            await list_channels(user_id, callback, "report")


async def start_qr_login(message: Message, user_id):
    proxy = temp_data[user_id]["proxy"]
    status_message = await message.answer("Подключаюсь к Telegram для QR-входа...")

    await cleanup_qr_login_flow(user_id)
    new_client = create_user_client(proxy=proxy)

    try:
        await asyncio.wait_for(new_client.connect(), timeout=TELEGRAM_TIMEOUT_SECONDS)
        qr_login = await asyncio.wait_for(new_client.qr_login(), timeout=TELEGRAM_TIMEOUT_SECONDS)
        qr_path = create_qr_login_image(qr_login.url, user_id)
        qr_message = await admin_bot.send_photo(
            message.chat.id,
            FSInputFile(qr_path),
            caption=(
                "Отсканируйте QR-код в Telegram:\n"
                "Настройки -> Устройства -> Подключить устройство.\n\n"
                "После сканирования бот сам перейдет к выбору каналов."
            ),
        )

        await status_message.edit_text(
            "QR-код отправлен. Ожидаю подтверждение до 2 минут..."
        )
        task = asyncio.create_task(
            wait_for_qr_login(
                message=message,
                status_message=status_message,
                qr_message=qr_message,
                user_id=user_id,
                client_acc=new_client,
                qr_login=qr_login,
            )
        )
        qr_login_flows[user_id] = {"client": new_client, "task": task}
        logger.info("QR-вход ожидает подтверждения для пользователя %s", user_id)
    except Exception:
        logger.exception("Не удалось выполнить QR-вход для пользователя %s", user_id)

        with contextlib.suppress(Exception):
            await new_client.disconnect()

        user_states[user_id] = "SELECT_AUTH_METHOD"
        await status_message.edit_text(
            "Не удалось выполнить QR-вход.\n\n"
            "Подключение к Telegram через выбранный маршрут не прошло. "
            "Попробуйте рабочий HTTP-прокси или вход по номеру.",
            reply_markup=get_auth_method_markup(),
        )


async def wait_for_qr_login(message, status_message, qr_message, user_id, client_acc, qr_login):
    keep_client = False

    try:
        await asyncio.wait_for(qr_login.wait(), timeout=QR_LOGIN_TIMEOUT_SECONDS)

        if user_id not in temp_data:
            await status_message.edit_text("QR подтвержден, но мастер уже был сброшен. Начните заново через /start.")
            return

        temp_clients[user_id] = client_acc
        temp_data[user_id]["telethon_session"] = client_acc.session.save()
        keep_client = True
        await status_message.edit_text("QR-вход выполнен. Загружаю список каналов...")
        await message.answer("QR-вход выполнен. Загружаю список каналов...")
        logger.info("QR-вход подтвержден для пользователя %s", user_id)
        await after_authorized(user_id, message)

        if qr_message:
            with contextlib.suppress(Exception):
                await qr_message.delete()
    except SessionPasswordNeededError:
        if user_id not in temp_data:
            await status_message.edit_text("QR подтвержден, но мастер уже был сброшен. Начните заново через /start.")
            return

        temp_clients[user_id] = client_acc
        keep_client = True
        user_states[user_id] = "WAIT_PASSWORD"
        logger.info("QR-вход подтвержден, требуется 2FA для пользователя %s", user_id)
        await status_message.edit_text("QR подтвержден. Введите пароль двухфакторной защиты:")
        await message.answer("QR подтвержден. Введите пароль двухфакторной защиты:")
    except asyncio.TimeoutError:
        user_states[user_id] = "SELECT_AUTH_METHOD"
        logger.warning("QR-вход истек для пользователя %s", user_id)
        await status_message.edit_text(
            "QR-код истек или не был подтвержден.\n\n"
            "Нажмите «Войти по QR» еще раз или выберите вход по номеру.",
            reply_markup=get_auth_method_markup(),
        )
        if qr_message:
            with contextlib.suppress(Exception):
                await qr_message.delete()
    except Exception:
        logger.exception("Ошибка ожидания QR-входа для пользователя %s", user_id)
        user_states[user_id] = "SELECT_AUTH_METHOD"
        await status_message.edit_text(
            "Не удалось завершить QR-вход.\n\n"
            "Попробуйте еще раз или используйте вход по номеру.",
            reply_markup=get_auth_method_markup(),
        )
    finally:
        flow = qr_login_flows.get(user_id)
        if flow and flow.get("client") is client_acc:
            qr_login_flows.pop(user_id, None)

        if not keep_client:
            with contextlib.suppress(Exception):
                await client_acc.disconnect()


async def connect_and_request_phone(message: Message, user_id):
    proxy = temp_data[user_id]["proxy"]
    status_message = await message.answer("Подключаюсь к Telegram...")
    new_client = create_user_client(proxy=proxy)

    try:
        await asyncio.wait_for(new_client.connect(), timeout=TELEGRAM_TIMEOUT_SECONDS)
        temp_clients[user_id] = new_client
        user_states[user_id] = "WAIT_PHONE"
        await status_message.edit_text("Шаг 3\nВведите номер телефона:")
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            await new_client.disconnect()

        user_states[user_id] = "SELECT_AUTH_METHOD"
        await status_message.edit_text(
            "Не удалось подключиться к Telegram за 45 секунд.\n\n"
            "Попробуйте QR-вход, вход по номеру еще раз или другой прокси.",
            reply_markup=get_auth_method_markup(),
        )
    except Exception:
        logger.exception("Не удалось подключить временный клиент для пользователя %s", user_id)
        with contextlib.suppress(Exception):
            await new_client.disconnect()

        user_states[user_id] = "SELECT_AUTH_METHOD"
        await status_message.edit_text(
            "Не удалось подключить клиент аккаунта.\n\n"
            "Попробуйте QR-вход, вход по номеру еще раз или другой прокси.",
            reply_markup=get_auth_method_markup(),
        )


async def list_channels(user_id, obj, mode):
    is_callback = isinstance(obj, CallbackQuery)
    base_message = obj.message if is_callback else obj
    client_acc = temp_clients.get(user_id)

    if not client_acc:
        await base_message.answer("Временный клиент аккаунта не найден.")
        return

    if mode == "target":
        text = (
            "Шаг 3\n"
            "Выберите основной канал с заявками.\n\n"
            "В этом канале бот будет принимать заявки на вступление."
        )
        manual_text = "Ввести основной канал вручную"
    elif mode == "report":
        text = (
            "Шаг 4\n"
            "Выберите чат или канал для отчетов.\n\n"
            "Сюда бот будет писать о запуске задачи, новых заявках и одобрениях."
        )
        manual_text = "Ввести чат отчетов вручную"
    else:
        text = "Выберите канал:"
        manual_text = "Ввести вручную"

    rows = [[button(manual_text, f"sel_{mode}_manual")]]

    if mode == "report":
        rows[0].append(button("Избранное", "sel_report_me"))

    try:
        async for dialog in client_acc.iter_dialogs(limit=200):
            entity = dialog.entity
            if dialog.is_channel or dialog.is_group:
                title = getattr(entity, "title", None) or dialog.name or "Без названия"
                rows.append([button(title, f"sel_{mode}_{utils.get_peer_id(entity)}")])

        if is_callback:
            await base_message.edit_text(text, reply_markup=markup(rows))
        else:
            await base_message.answer(text, reply_markup=markup(rows))
    except Exception:
        logger.exception("Не удалось получить список каналов для пользователя %s", user_id)
        if is_callback:
            await base_message.edit_text("Не удалось загрузить список каналов.")
        else:
            await base_message.answer("Не удалось загрузить список каналов.")


async def find_and_set_channel(user_id, message, target, key):
    client_acc = temp_clients.get(user_id)
    if not client_acc:
        await message.answer("Временный клиент аккаунта не найден. Начните заново.")
        return

    if target == "me":
        temp_data[user_id][key] = "me"
        user_states[user_id] = "WAIT_MSG"
        await message.answer("Введите текст сообщения. Разделитель: |")
        return

    try:
        chat = await client_acc.get_entity(target)
    except Exception:
        try:
            updates = await client_acc(functions.channels.JoinChannelRequest(target))
            chat = updates.chats[0] if getattr(updates, "chats", None) else await client_acc.get_entity(target)
        except Exception:
            logger.exception("Не удалось найти или подключить канал: %s", target)
            await message.answer("Канал не найден.")
            return

    temp_data[user_id][key] = utils.get_peer_id(chat)

    if key == "target_channel_id":
        await list_channels(user_id, message, "report")
    else:
        user_states[user_id] = "WAIT_MSG"
        await message.answer("Введите текст сообщения. Разделитель: |")


@dp.message(F.photo)
async def process_photo(message: Message):
    user_id = message.from_user.id

    if user_states.get(user_id) != "WAIT_PHOTO":
        return

    if user_id not in temp_data or "name" not in temp_data[user_id]:
        await cleanup_user(user_id)
        await message.answer("Временные данные были очищены. Начните заново через /start")
        return

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    path = os.path.join(DOWNLOADS_DIR, f"photo_{temp_data[user_id]['name']}.jpg")
    await admin_bot.download(message.photo[-1], destination=path)

    if not os.path.exists(path):
        await message.answer("Не удалось скачать фото. Попробуйте отправить его еще раз.")
        return

    await finish_wizard(message, path)


async def finish_wizard(message, photo_path):
    user_id = message.from_user.id

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await message.answer("Временные данные были очищены. Начните заново через /start")
        return

    data = temp_data[user_id]
    data["photo_path"] = photo_path
    data["enabled"] = False
    data["client_type"] = "telethon"

    client_acc = temp_clients.get(user_id)
    if isinstance(client_acc, TelegramClient):
        data["telethon_session"] = client_acc.session.save()

    for key in ["phone", "phone_hash"]:
        data.pop(key, None)

    config = load_config()
    config[data["name"]] = data
    save_config(config)

    await cleanup_user(user_id)
    await message.answer(f"Задача «{data['name']}» создана.")


@dp.callback_query()
async def handle_callback(callback: CallbackQuery):
    data = callback.data or ""

    # New independent account/campaign model. Legacy branches below are kept intact.
    if data == "accounts_menu":
        await callback.answer(); await show_accounts_menu(callback); return
    if data == "legacy_menu":
        await callback.answer()
        config = load_config(); rows = [[button(f"{'🟢' if name in active_tasks else '🔴'} {name}", f"view_{name}")] for name in config]
        rows.append([button("Назад", "back_main")])
        await callback.message.edit_text("Старые задачи (не входят в кампании)", reply_markup=markup(rows)); return
    if data == "campaigns_menu":
        await callback.answer(); await show_campaigns_menu(callback); return
    if data == "account_add":
        user_states[callback.from_user.id] = "ACCOUNT_PROXY"
        temp_data[callback.from_user.id] = {"flow": "account", "account_id": new_campaign_id()}
        await callback.answer(); await callback.message.edit_text("Введите прокси или отправьте «нет».\n\n" + proxy_format_hint()); return
    if data.startswith("account_auth_"):
        method = data.rsplit("_", 1)[1]; user_id = callback.from_user.id
        if user_states.get(user_id) != "ACCOUNT_AUTH":
            await callback.answer("Мастер подключения устарел", show_alert=True); return
        await callback.answer()
        if method == "session":
            user_states[user_id] = "ACCOUNT_SESSION"
            await callback.message.edit_text("Вставьте готовую строку Telethon StringSession одним сообщением.")
        elif method == "qr":
            await start_qr_login(callback.message, user_id)
        else:
            await connect_and_request_phone(callback.message, user_id)
        return
    if data.startswith("account_view_"):
        account_id = data.split("account_view_", 1)[1]; account = load_accounts().get(account_id)
        if not account: await callback.answer("Аккаунт не найден", show_alert=True); return
        state = "включен" if account.get("enabled", True) else "выключен"
        info = (f"Аккаунт: {account.get('name', account_id)}\nСтатус: {state}\nПрокси: {redact_proxy(account.get('proxy'))}\n"
                f"2-е сообщение: {'настроено' if account.get('second_messages') else 'не настроено'}\n"
                f"3-е сообщение: {'настроено' if account.get('third_messages') else 'не настроено'}")
        rows = [[button("Выключить" if account.get("enabled", True) else "Включить", f"account_toggle_{account_id}")],
                [button("2-е сообщение", f"account_second_{account_id}"), button("3-е сообщение", f"account_third_{account_id}")],
                [button("Прокси", f"account_proxy_{account_id}")], [button("Назад", "accounts_menu")]]
        await callback.answer(); await callback.message.edit_text(info, reply_markup=markup(rows)); return
    if data.startswith("account_toggle_"):
        account_id = data.split("account_toggle_", 1)[1]; registry = load_accounts()
        if account_id in registry:
            registry[account_id]["enabled"] = not registry[account_id].get("enabled", True); save_accounts(registry)
        await callback.answer(); await show_accounts_menu(callback); return
    if data.startswith(("account_second_", "account_third_", "account_proxy_")):
        action, account_id = data.split("_", 1)[1].split("_", 1); registry = load_accounts()
        if account_id not in registry: await callback.answer("Аккаунт не найден", show_alert=True); return
        if action == "proxy":
            user_states[callback.from_user.id] = "ACCOUNT_EDIT_PROXY"; temp_data[callback.from_user.id] = {"account_id": account_id}
            await callback.answer(); await callback.message.edit_text("Введите новый прокси или «нет» для отключения."); return
        user_states[callback.from_user.id] = "ACCOUNT_SECOND" if action == "second" else "ACCOUNT_THIRD"
        temp_data[callback.from_user.id] = {"account_id": account_id}
        await callback.answer(); await callback.message.edit_text("Перешлите готовое сообщение из Telegram — оформление, цитаты и ссылки сохранятся.\n\nЛибо отправьте текстовые варианты через символ |."); return
    if data == "campaign_add":
        if not load_accounts(): await callback.answer("Сначала подключите хотя бы один аккаунт", show_alert=True); return
        user_states[callback.from_user.id] = "CAMPAIGN_NAME"; temp_data[callback.from_user.id] = {"campaign_id": new_campaign_id()}
        await callback.answer(); await callback.message.edit_text("Название новой кампании:"); return
    if data.startswith("campaign_approver_"):
        account_id = data.split("campaign_approver_", 1)[1]; user_id = callback.from_user.id
        if account_id not in load_accounts() or user_states.get(user_id) != "CAMPAIGN_APPROVER": await callback.answer("Выбор устарел", show_alert=True); return
        temp_data[user_id]["approver_account_id"] = account_id; temp_data[user_id]["sender_account_ids"] = []
        user_states[user_id] = "CAMPAIGN_SENDERS"
        await callback.answer(); await callback.message.edit_text("Выберите аккаунты рассылки (можно несколько):", reply_markup=markup(account_selection_rows("campaign_sender_", set(), True))); return
    if data.startswith("campaign_sender_"):
        user_id = callback.from_user.id
        if user_states.get(user_id) != "CAMPAIGN_SENDERS": await callback.answer("Выбор устарел", show_alert=True); return
        account_id = data.split("campaign_sender_", 1)[1]; chosen = set(temp_data[user_id].get("sender_account_ids", []))
        if account_id in chosen: chosen.remove(account_id)
        else: chosen.add(account_id)
        temp_data[user_id]["sender_account_ids"] = list(chosen)
        await callback.answer(); await callback.message.edit_reply_markup(reply_markup=markup(account_selection_rows("campaign_sender_", chosen, True))); return
    if data == "campaign_senders_done":
        user_id = callback.from_user.id; draft = temp_data.get(user_id, {})
        if not draft.get("sender_account_ids"): await callback.answer("Выберите хотя бы один аккаунт", show_alert=True); return
        campaign_id = draft["campaign_id"]
        def channel(value):
            if value == "me": return value
            try: return int(value)
            except (TypeError, ValueError): return value
        all_campaigns = load_campaigns(); all_campaigns[campaign_id] = {"id": campaign_id, "name": draft["campaign_name"], "target_channel_id": channel(draft["target_channel_id"]), "report_channel_id": channel(draft["report_channel_id"]), "approver_account_id": draft["approver_account_id"], "sender_account_ids": draft["sender_account_ids"], "pauses": draft["pauses"], "enabled": False}
        save_campaigns(all_campaigns); await cleanup_user(user_id)
        await callback.answer("Кампания создана"); await show_campaigns_menu(callback); return
    if data.startswith("campaign_view_"):
        campaign_id = data.split("campaign_view_", 1)[1]; campaign = load_campaigns().get(campaign_id)
        if not campaign: await callback.answer("Кампания не найдена", show_alert=True); return
        registry = load_accounts(); approver = registry.get(campaign.get("approver_account_id"), {}).get("name", "не найден")
        senders = [registry.get(a, {}).get("name", a) for a in campaign.get("sender_account_ids", [])]
        info = f"Кампания: {campaign.get('name')}\nКанал: {campaign.get('target_channel_id')}\nОдобряет: {approver}\nРассылка: {', '.join(senders) or '—'}\nРучная очередь: {campaign_store.pending_count(campaign_id)}"
        running = campaign_id in active_campaigns
        rows = [[button("Остановить" if running else "Запустить", f"campaign_stop_{campaign_id}" if running else f"campaign_start_{campaign_id}")], [button("Обработать старые ответы", f"campaign_old_replies_{campaign_id}")], [button("Обработать старые заявки", f"campaign_old_requests_{campaign_id}")], [button("Назад", "campaigns_menu")]]
        await callback.answer(); await callback.message.edit_text(info, reply_markup=markup(rows)); return
    if data.startswith(("campaign_start_", "campaign_stop_", "campaign_old_replies_", "campaign_old_requests_")):
        parts = data.split("_"); action = "_".join(parts[1:-1]); campaign_id = parts[-1]
        definitions = load_campaigns(); definition = definitions.get(campaign_id)
        if not definition: await callback.answer("Кампания не найдена", show_alert=True); return
        if action == "start":
            await callback.answer(); await callback.message.edit_text("Подключаю аккаунты кампании…")
            worker = CampaignWorker(campaign_id, definition); ok, result = await worker.start()
            if ok: definitions[campaign_id]["enabled"] = True; save_campaigns(definitions)
            await callback.message.edit_text(result); await asyncio.sleep(1); await show_campaigns_menu(callback); return
        if action == "stop":
            if campaign_id in active_campaigns: await active_campaigns[campaign_id].stop()
            definitions[campaign_id]["enabled"] = False; save_campaigns(definitions); await callback.answer("Остановлено"); await show_campaigns_menu(callback); return
        worker = active_campaigns.get(campaign_id)
        if not worker: await callback.answer("Сначала запустите кампанию", show_alert=True); return
        await callback.answer("Запущено")
        asyncio.create_task(worker.process_old_replies() if action == "old_replies" else worker.process_old_requests())
        await callback.message.answer("Разовая обработка запущена. Результат появится в канале отчётов."); return

    if data in ("refresh", "back_main"):
        await callback.answer()
        await show_main_menu(callback)
        return

    if data == "cancel_add":
        await cleanup_user(callback.from_user.id)
        await show_main_menu(callback)
        return

    if data.startswith("view_"):
        task_name = data.split("_", 1)[1]
        config = load_config().get(task_name)
        if not config:
            return

        status = "РАБОТАЕТ" if task_name in active_tasks else "ОСТАНОВЛЕНА"
        session_name = config.get("session_file", task_name)
        pauses = config.get("pauses") or {
            "first_min": 3,
            "first_max": 10,
            "gap_min": 25,
            "gap_max": 40,
        }
        pauses_text = (
            f"{pauses.get('first_min', 3)}-{pauses.get('first_max', 10)}|"
            f"{pauses.get('gap_min', 25)}-{pauses.get('gap_max', 40)}"
        )

        info = (
            f"Задача: {task_name}\n"
            f"Статус: {status}\n"
            f"Аккаунт: {session_name}\n"
            f"Паузы: {pauses_text}\n"
            f"2-е сообщение: {'настроено' if config.get('second_messages') else 'не настроено'}\n"
            f"3-е сообщение: {'настроено' if config.get('messages') else 'не настроено'}"
        )

        rows = [[
            button("Остановить", f"stop_{task_name}")
            if task_name in active_tasks
            else button("Запустить", f"start_{task_name}")
        ]]
        rows.append([
            button("Настроить 2-е сообщение", f"second_{task_name}"),
            button("Настроить 3-е сообщение", f"third_{task_name}"),
        ])
        rows.append([
            button("Удалить", f"del_{task_name}"),
            button("Назад", "back_main"),
        ])

        await callback.answer()
        await callback.message.edit_text(info, reply_markup=markup(rows))
        return

    if data.startswith(("second_", "third_")):
        message_stage, task_name = data.split("_", 1)
        if task_name not in load_config():
            await callback.answer("Задача не найдена", show_alert=True)
            return
        message_number = "2-е" if message_stage == "second" else "3-е"
        user_states[callback.from_user.id] = "WAIT_SECOND_MSG" if message_stage == "second" else "WAIT_THIRD_MSG"
        temp_data[callback.from_user.id] = {"editing_message_task": task_name}
        await callback.answer()
        await callback.message.edit_text(
            f"{message_number} сообщение для задачи «{task_name}».\n\n"
            "Отправьте текст. Для нескольких вариантов разделяйте их символом |"
        )
        return

    if data.startswith("start_"):
        task_name = data.split("_", 1)[1]

        if task_name in active_tasks:
            await callback.answer("Задача уже запущена", show_alert=True)
            await show_main_menu(callback)
            return

        config = load_config().get(task_name)
        if not config:
            return

        await callback.message.edit_text(f"Запускаю задачу «{task_name}»...")
        worker = AutoApproveWorker(
            task_name=task_name,
            api_id=config.get("api_id", API_ID),
            api_hash=config.get("api_hash", API_HASH),
            proxy=config.get("proxy"),
            target_channel_id=config["target_channel_id"],
            report_channel_id=config["report_channel_id"],
            messages=config["messages"],
            second_messages=config.get("second_messages"),
            photo_path=config.get("photo_path"),
            session_file=config.get("session_file"),
            pauses=config.get("pauses"),
            telethon_session=config.get("telethon_session"),
        )

        success, result_message = await worker.start()

        if success:
            active_tasks[task_name] = worker
            set_task_enabled(task_name, True)
            await callback.message.edit_text(result_message)
        else:
            await callback.message.edit_text(f"Не удалось запустить задачу: {result_message}")

        await asyncio.sleep(2)
        await show_main_menu(callback)
        return

    if data.startswith("stop_"):
        task_name = data.split("_", 1)[1]

        if task_name in active_tasks:
            await active_tasks.pop(task_name).stop()

        set_task_enabled(task_name, False)
        await show_main_menu(callback)
        return

    if data.startswith("del_"):
        task_name = data.split("_", 1)[1]

        if task_name in active_tasks:
            await active_tasks.pop(task_name).stop()

        config = load_config()
        if task_name in config:
            del config[task_name]
            save_config(config)

        await show_main_menu(callback)
        return


async def restore_enabled_tasks():
    config = load_config()

    for task_name, task_config in config.items():
        if not task_config.get("enabled"):
            continue

        if task_name in active_tasks:
            continue

        try:
            worker = AutoApproveWorker(
                task_name=task_name,
                api_id=task_config.get("api_id", API_ID),
                api_hash=task_config.get("api_hash", API_HASH),
                proxy=task_config.get("proxy"),
                target_channel_id=task_config["target_channel_id"],
                report_channel_id=task_config["report_channel_id"],
                messages=task_config["messages"],
                second_messages=task_config.get("second_messages"),
                photo_path=task_config.get("photo_path"),
                session_file=task_config.get("session_file"),
                pauses=task_config.get("pauses"),
                telethon_session=task_config.get("telethon_session"),
            )

            success, result_message = await worker.start()
            if success:
                active_tasks[task_name] = worker
                logger.info("Задача восстановлена: %s", task_name)
            else:
                logger.warning("Не удалось восстановить задачу %s: %s", task_name, result_message)
                set_task_enabled(task_name, False)
        except Exception:
            logger.exception("Непредвиденная ошибка при восстановлении задачи %s", task_name)
            set_task_enabled(task_name, False)


async def stop_active_tasks():
    for task_name, worker in list(active_tasks.items()):
        try:
            await worker.stop()
        except Exception:
            logger.exception("Не удалось остановить задачу %s", task_name)
        finally:
            active_tasks.pop(task_name, None)


async def restore_enabled_campaigns():
    for campaign_id, definition in load_campaigns().items():
        if not definition.get("enabled") or campaign_id in active_campaigns:
            continue
        worker = CampaignWorker(campaign_id, definition)
        success, result = await worker.start()
        if not success:
            logger.warning("Не удалось восстановить кампанию %s: %s", campaign_id, result)
            data = load_campaigns()
            if campaign_id in data:
                data[campaign_id]["enabled"] = False
                save_campaigns(data)


async def stop_active_campaigns():
    for campaign_id, worker in list(active_campaigns.items()):
        with contextlib.suppress(Exception):
            await worker.stop()


async def main():
    logger.info("Админ-бот Bot API запускается")
    await admin_bot.delete_webhook(drop_pending_updates=False)
    await restore_enabled_tasks()
    await restore_enabled_campaigns()

    try:
        await dp.start_polling(admin_bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await stop_active_tasks()
        await stop_active_campaigns()
        await admin_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
