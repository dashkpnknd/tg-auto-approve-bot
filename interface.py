import asyncio
import json
import logging
import os
import tempfile

from pyrogram import Client, enums, filters, idle
from pyrogram.errors import SessionPasswordNeeded
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from approvedApplications import AutoApproveWorker
from config_local import API_HASH, API_ID, BOT_TOKEN, TASKS_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

admin_app = Client(
    "admin_bot_ui",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

active_tasks = {}
user_states = {}
temp_data = {}
temp_clients = {}


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


async def cleanup_user(user_id: int):
    user_states.pop(user_id, None)
    temp_data.pop(user_id, None)

    if user_id in temp_clients:
        try:
            await temp_clients[user_id].disconnect()
        except Exception:
            logger.exception("Не удалось отключить временный клиент для пользователя %s", user_id)
        finally:
            temp_clients.pop(user_id, None)


@admin_app.on_message(filters.command("start"))
async def cmd_start(client, message):
    await cleanup_user(message.from_user.id)
    await show_main_menu(message)


async def show_main_menu(message_or_callback):
    config = load_config()
    buttons = []

    buttons.append([InlineKeyboardButton("Добавить задачу", callback_data="add_new_task")])

    for task_name in config.keys():
        status = "🟢" if task_name in active_tasks else "🔴"
        buttons.append(
            [InlineKeyboardButton(f"{status} {task_name}", callback_data=f"view_{task_name}")]
        )
    buttons.append([InlineKeyboardButton("Обновить список", callback_data="refresh")])

    text = f"Панель управления\nАктивных задач: {len(active_tasks)}"

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await message_or_callback.reply(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


@admin_app.on_callback_query(filters.regex("^add_new_task$"))
async def start_add_task(client, callback):
    user_id = callback.from_user.id
    user_states[user_id] = "WAIT_NAME"
    temp_data[user_id] = {
        "api_id": API_ID,
        "api_hash": API_HASH,
    }
    await callback.message.edit_text(
        "Шаг 1\n\nОтправьте название задачи одним сообщением."
    )


@admin_app.on_message(filters.text & ~filters.command("start"))
async def process_wizard(client, message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    text = (message.text or "").strip()

    if not state:
        return

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await message.reply("Временные данные были очищены. Начните заново через /start")
        return

    if state == "WAIT_NAME":
        config = load_config()
        task_name = sanitize_task_name(text)

        if not task_name:
            await message.reply("Некорректное название задачи. Используйте буквы, цифры, дефис или нижнее подчеркивание.")
            return

        if task_name in config:
            await message.reply("Такое название уже занято. Отправьте другое.")
            return

        temp_data[user_id]["name"] = task_name

        sessions = get_unique_sessions()
        buttons = [[InlineKeyboardButton("Подключить новый аккаунт", callback_data="acc_new")]]

        for session_name in sessions.keys():
            buttons.append(
                [InlineKeyboardButton(f"Аккаунт: {session_name}", callback_data=f"acc_use_{session_name}")]
            )

        user_states[user_id] = "SELECT_ACCOUNT"
        await message.reply(
            "Шаг 2\nВыберите аккаунт",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif state == "WAIT_PROXY":
        proxy = None

        if text.lower() not in ["нет", "-", "no"]:
            try:
                scheme, ip, port, user, password = text.split(":")
                proxy = {
                    "scheme": scheme,
                    "hostname": ip,
                    "port": int(port),
                    "username": user,
                    "password": password,
                }
            except Exception:
                await message.reply(
                    "Неверный формат прокси. Пример: socks5:ip:port:user:pass или напишите 'нет'."
                )
                return

        temp_data[user_id]["proxy"] = proxy
        temp_data[user_id]["session_file"] = temp_data[user_id]["name"]
        await connect_and_request_phone(message, user_id)

    elif state == "WAIT_PHONE":
        client_acc = temp_clients.get(user_id)
        if not client_acc:
            await message.reply("Временный клиент аккаунта не найден. Начните заново.")
            return

        phone = text.replace(" ", "").replace("-", "")
        if not phone.startswith("+"):
            phone = "+" + phone

        temp_data[user_id]["phone"] = phone

        try:
            sent = await client_acc.send_code(phone)
            temp_data[user_id]["phone_hash"] = sent.phone_code_hash
            user_states[user_id] = "WAIT_CODE"
            await message.reply("Код отправлен. Введите код:")
        except Exception:
            logger.exception("Не удалось отправить код для пользователя %s", user_id)
            await message.reply("Не удалось отправить код подтверждения.")

    elif state == "WAIT_CODE":
        client_acc = temp_clients.get(user_id)
        if not client_acc:
            await message.reply("Временный клиент аккаунта не найден. Начните заново.")
            return

        try:
            await client_acc.sign_in(
                temp_data[user_id]["phone"],
                temp_data[user_id]["phone_hash"],
                text.strip(),
            )
            await list_channels(user_id, message, "target")
        except SessionPasswordNeeded:
            user_states[user_id] = "WAIT_PASSWORD"
            await message.reply("Введите пароль двухфакторной защиты:")
        except Exception:
            logger.exception("Не удалось выполнить вход для пользователя %s", user_id)
            await message.reply("Не удалось выполнить вход в аккаунт.")

    elif state == "WAIT_PASSWORD":
        client_acc = temp_clients.get(user_id)
        if not client_acc:
            await message.reply("Временный клиент аккаунта не найден. Начните заново.")
            return

        try:
            await client_acc.check_password(text)
            await list_channels(user_id, message, "target")
        except Exception:
            logger.exception("Не удалось проверить пароль для пользователя %s", user_id)
            await message.reply("Не удалось подтвердить пароль.")

    elif state == "WAIT_TARGET_MANUAL":
        await find_and_set_channel(user_id, message, text, "target_channel_id")

    elif state == "WAIT_REPORT_MANUAL":
        await find_and_set_channel(user_id, message, text, "report_channel_id")

    elif state == "WAIT_MSG":
        messages = [item.strip() for item in text.split("|") if item.strip()]
        if not messages:
            await message.reply("Нужно указать хотя бы одно сообщение. Разделитель: |")
            return

        temp_data[user_id]["messages"] = messages
        user_states[user_id] = "WAIT_PAUSES"

        await message.reply(
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
                await message.reply("Неверный формат. Пример: 3-10|25-40 или по умолчанию.")
                return

        user_states[user_id] = "WAIT_PHOTO"
        await message.reply("Шаг 7\nОтправьте фото или напишите 'нет'.")

    elif state == "WAIT_PHOTO":
        if text.lower() in ["нет", "-", "no"]:
            await finish_wizard(message, None)


@admin_app.on_callback_query(filters.regex("^acc_"))
async def handle_account_selection(client, callback):
    user_id = callback.from_user.id
    data = callback.data

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await callback.message.edit_text("Временные данные были очищены. Начните заново через /start")
        return

    if data == "acc_new":
        user_states[user_id] = "WAIT_PROXY"
        await callback.message.edit_text(
            "Введите прокси или напишите 'нет'\n\nФормат: socks5:ip:port:user:pass"
        )

    elif data.startswith("acc_use_"):
        session_name = data.split("acc_use_", 1)[1]
        old_data = get_unique_sessions().get(session_name)

        if not old_data:
            await callback.message.edit_text("Сохраненная сессия не найдена.")
            return

        temp_data[user_id]["session_file"] = session_name
        temp_data[user_id]["proxy"] = old_data.get("proxy")

        await callback.message.edit_text(f"Выбран аккаунт: {session_name}. Подключение...")

        new_client = Client(
            name=f"session_{session_name}",
            api_id=API_ID,
            api_hash=API_HASH,
            proxy=old_data.get("proxy"),
        )

        try:
            await new_client.connect()
            temp_clients[user_id] = new_client
            await list_channels(user_id, callback, "target")
        except Exception:
            logger.exception("Не удалось подключить сохраненную сессию %s", session_name)
            await callback.message.reply("Не удалось подключить выбранный аккаунт.")


@admin_app.on_callback_query(filters.regex("^sel_"))
async def handle_channel_selection(client, callback):
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


async def connect_and_request_phone(message, user_id):
    session_name = temp_data[user_id]["session_file"]
    proxy = temp_data[user_id]["proxy"]

    new_client = Client(
        name=f"session_{session_name}",
        api_id=API_ID,
        api_hash=API_HASH,
        proxy=proxy,
    )

    try:
        await new_client.connect()
        temp_clients[user_id] = new_client
        user_states[user_id] = "WAIT_PHONE"
        await message.reply("Шаг 3\nВведите номер телефона:")
    except Exception:
        logger.exception("Не удалось подключить временный клиент для пользователя %s", user_id)
        await message.reply("Не удалось подключить клиент аккаунта.")

async def list_channels(user_id, obj, mode):
    is_callback = isinstance(obj, CallbackQuery)
    base_message = obj.message if is_callback else obj
    client_acc = temp_clients.get(user_id)

    if not client_acc:
        if is_callback:
            await base_message.edit_text("Временный клиент аккаунта не найден.")
        else:
            await base_message.reply("Временный клиент аккаунта не найден.")
        return

    buttons = [[InlineKeyboardButton("Ввести вручную", callback_data=f"sel_{mode}_manual")]]

    if mode == "report":
        buttons[0].append(
            InlineKeyboardButton("Избранное", callback_data="sel_report_me")
        )

    try:
        async for dialog in client_acc.get_dialogs(limit=200):
            chat = dialog.chat
            if chat.type in [
                enums.ChatType.CHANNEL,
                enums.ChatType.SUPERGROUP,
                enums.ChatType.GROUP,
            ]:
                buttons.append(
                    [InlineKeyboardButton(f"{chat.title}", callback_data=f"sel_{mode}_{chat.id}")]
                )

        text = "Выберите канал:"
        markup = InlineKeyboardMarkup(buttons)

        if is_callback:
            await base_message.edit_text(text, reply_markup=markup)
        else:
            await base_message.reply(text, reply_markup=markup)

    except Exception:
        logger.exception("Не удалось получить список каналов для пользователя %s", user_id)
        if is_callback:
            await base_message.edit_text("Не удалось загрузить список каналов.")
        else:
            await base_message.reply("Не удалось загрузить список каналов.")


async def find_and_set_channel(user_id, message, target, key):
    client_acc = temp_clients.get(user_id)
    if not client_acc:
        await message.reply("Временный клиент аккаунта не найден. Начните заново.")
        return

    try:
        chat = await client_acc.get_chat(target)
    except Exception:
        try:
            chat = await client_acc.join_chat(target)
        except Exception:
            logger.exception("Не удалось найти или подключить канал: %s", target)
            await message.reply("Канал не найден.")
            return

    temp_data[user_id][key] = chat.id

    if key == "target_channel_id":
        await list_channels(user_id, message, "report")
    else:
        user_states[user_id] = "WAIT_MSG"
        await message.reply("Введите текст сообщения. Разделитель: |")


@admin_app.on_message(filters.photo)
async def process_photo(client, message):
    user_id = message.from_user.id

    if user_states.get(user_id) != "WAIT_PHOTO":
        return

    if user_id not in temp_data or "name" not in temp_data[user_id]:
        await cleanup_user(user_id)
        await message.reply("Временные данные были очищены. Начните заново через /start")
        return

    path = f"photo_{temp_data[user_id]['name']}.jpg"
    await message.download(path)
    await finish_wizard(message, path)


async def finish_wizard(message, photo_path):
    user_id = message.from_user.id

    if user_id not in temp_data:
        await cleanup_user(user_id)
        await message.reply("Временные данные были очищены. Начните заново через /start")
        return

    data = temp_data[user_id]
    data["photo_path"] = photo_path
    data["enabled"] = False

    for key in ["phone", "phone_hash"]:
        data.pop(key, None)

    config = load_config()
    config[data["name"]] = data
    save_config(config)

    await cleanup_user(user_id)
    await message.reply(f"Задача «{data['name']}» создана.")


@admin_app.on_callback_query()
async def handle_callback(client, callback: CallbackQuery):
    data = callback.data

    if data in ("refresh", "back_main"):
        await show_main_menu(callback)
        return

    if data == "add_new_task":
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
            f"Паузы: {pauses_text}"
        )

        buttons = [[
            InlineKeyboardButton("Остановить", callback_data=f"stop_{task_name}")
            if task_name in active_tasks
            else InlineKeyboardButton("Запустить", callback_data=f"start_{task_name}")
        ]]
        buttons.append([
            InlineKeyboardButton("Удалить", callback_data=f"del_{task_name}"),
            InlineKeyboardButton("Назад", callback_data="back_main"),
        ])

        await callback.message.edit_text(
            info,
            reply_markup=InlineKeyboardMarkup(buttons),
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
            photo_path=config.get("photo_path"),
            session_file=config.get("session_file"),
            pauses=config.get("pauses"),
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
                photo_path=task_config.get("photo_path"),
                session_file=task_config.get("session_file"),
                pauses=task_config.get("pauses"),
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


async def main():
    await admin_app.start()
    logger.info("Админ-бот запущен")
    await restore_enabled_tasks()
    await idle()
    await admin_app.stop()


if __name__ == "__main__":
    admin_app.run(main())