"""
Основной бот: обрабатывает команды пользователей в ЛС через вебсокет Mattermost,
а также кнопки/модалки (см. buttons.py). Логика автоматических уведомлений о днях
рождения вынесена в scheduler.py.

Вся работа с ботом идёт через кнопки и модальные окна. Незарегистрированный
пользователь, позвавший бота (@упоминание) в ЛС, получает эфемерное сообщение с
кнопкой «Зарегистрироваться», которая открывает двухшаговую модалку регистрации
(ввод даты -> подтверждение). Текстовые команды в ЛС оставлены как запасной путь.
"""

import asyncio
import json
import signal
import sys
from datetime import datetime
from urllib.parse import urlparse

from mattermostdriver.websocket import Websocket

from dotenv import load_dotenv

load_dotenv()

import common
from common import (
    driver,
    log,
    init_db,
    BOT_USERNAME,
    build_fullname,
    escape_md,
    add_reaction,
    reply_to_message,
    resolve_user_id,
    claim_gift,
    find_existing_gift_for_donor,
    format_birth,
    now_local,
    today_local,
    parse_user_birth,
    parse_birth_dt,
    next_celebration,
    celebration_date,
    run_in_thread,
    _get_birthday,
)
from scheduler import birthday_scheduler, run_checks_for_user
import buttons

# Словарь состояний многошаговых диалогов: user_id -> state dict/str
waiting_for = {}
# Словарь активных таймаут-задач: user_id -> asyncio.Task
_timeout_tasks = {}

DIALOG_TIMEOUT_SECONDS = 300  # 5 минут
TIMEOUT_MESSAGE = (
    "Не дождался от тебя инфы, но если захочешь, "
    "ты всегда можешь повторить эту задачу заново."
)

HELP_TEXT = """Привет! Это праздничный бот Richard!

Доступные команды в ЛС:
`richard` - показать это сообщение
`add_birth DD.MM` или `add_birth DD.MM.YYYY` - добавить свою дату рождения
`delete-me` - удалить свой аккаунт и wish-list из бота

`wish-list` - показать свой список желаний
`add_gift` - добавить новое желание в свой список
`delete_gift N` - удалить желание под номером N из своего списка (если на него ещё нет дарителя)
`edit_gift N` - отредактировать желание под номером N (если на него ещё нет дарителя)

`wish-list @username` - показать список желаний другого пользователя и выбрать подарок
`my-gifts` - посмотреть, кому и что вы уже планируете подарить
`cancel @username` - отказаться от подарка, который вы выбрали для этого пользователя

Проще всего — просто позовите меня (@richard), и я покажу кнопки."""

ADD_GIFT_INTRO = (
    "Окей, давай добавим новое желание в список!\n"
    "Напиши своё желание — что-то общее или конкретное с деталями.\n"
    "(в любой момент можно ввести 0, чтобы отказаться от добавления)"
)

# Сколько дней вперёд считается «ближайшим месяцем» для команды soon_birthdays.
SOON_BIRTHDAY_DAYS = 30


def _cancel_timeout(user_id):
    """Отменяет активный таймаут для пользователя, если он есть."""
    task = _timeout_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def _schedule_timeout(user_id, channel_id):
    """
    Запускает таймер на DIALOG_TIMEOUT_SECONDS.
    По истечении — очищает состояние и пишет пользователю сообщение.
    Если для этого пользователя уже был таймаут — отменяет старый.
    """
    _cancel_timeout(user_id)

    async def _timeout_task():
        await asyncio.sleep(DIALOG_TIMEOUT_SECONDS)
        if user_id in waiting_for:
            waiting_for.pop(user_id, None)
            _timeout_tasks.pop(user_id, None)
            try:
                await run_in_thread(
                    driver.posts.create_post,
                    options={"channel_id": channel_id, "message": TIMEOUT_MESSAGE},
                )
            except Exception as e:
                log.warning(
                    "Не удалось отправить timeout-сообщение пользователю %s: %s",
                    user_id,
                    e,
                )

    task = asyncio.create_task(_timeout_task())
    _timeout_tasks[user_id] = task


def _set_state(user_id, channel_id, state):
    """
    Устанавливает новое состояние диалога и перезапускает таймаут.
    Используется вместо прямого присвоения waiting_for[user_id] = ...
    """
    waiting_for[user_id] = state
    _schedule_timeout(user_id, channel_id)


def _clear_state(user_id):
    """Очищает состояние диалога и отменяет таймаут."""
    waiting_for.pop(user_id, None)
    _cancel_timeout(user_id)


def format_gift_line(gift, index):
    """Форматирует строку подарка для вывода в список."""
    gift_name = escape_md(gift["gift_name"])
    if gift["gift_link"]:
        return f"{index} - [{gift_name}]({gift['gift_link']})"
    return f"{index} - {gift_name}"


def _validate_gift_number(words, gifts, command_hint):
    """
    Проверяет, что второй аргумент команды — корректный номер подарка в списке.
    Возвращает (gift, None) при успехе или (None, reply) при ошибке.
    """
    if len(words) < 2:
        return (
            None,
            f"Используй формат: `{command_hint} [номер желания в твоём wish-list]`",
        )
    try:
        gift_number = int(words[1])
    except ValueError:
        return (
            None,
            f"Используй формат: `{command_hint} [номер желания в твоём wish-list]`",
        )

    if not gifts:
        action = "удалять" if "delete" in command_hint else "редактировать"
        return None, f"Твой список желаний пуст, {action} нечего."
    if gift_number < 1 or gift_number > len(gifts):
        return None, f"Нет подарка под номером {gift_number}"

    return gifts[gift_number - 1], None


async def _render_own_wishlist(user_id, channel_id):
    """
    Показывает пользователю его собственный wish-list.
    Если список пуст — начинает диалог добавления.
    Возвращает текст ответа.
    """
    cursor = await common.db.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
    )
    if await cursor.fetchone() is None:
        return (
            "Чтобы создать свой список желаний, необходимо добавить свою дату рождения!\n"
            "Введите `add_birth DD.MM` или `add_birth DD.MM.YYYY`"
        )

    cursor = await common.db.execute(
        "SELECT * FROM gifts WHERE user_id = ? ORDER BY gift_id", (user_id,)
    )
    gifts = await cursor.fetchall()
    if not gifts:
        _set_state(user_id, channel_id, "add_gift")
        return "Кажется, ваш список желаний ещё пуст, давай добавим в него первое желание!\n(Да/Нет)"

    answer = ["В вашем списке желаний уже есть:"]
    for index, gift in enumerate(gifts, start=1):
        answer.append(format_gift_line(gift, index))
    return "\n".join(answer)


async def handle_user_added(data):
    log.info("user_added event: %s", json.dumps(data, ensure_ascii=False))
    added_user_id = data["data"].get("user_id")
    channel_id = data["broadcast"].get("channel_id")

    try:
        if added_user_id == driver.client.userid:
            # добавили самого бота — приветствие всему каналу
            await asyncio.sleep(3)
            await run_in_thread(
                driver.posts.create_post,
                options={"channel_id": channel_id, "message": HELP_TEXT},
            )
        # добавление обычных пользователей во временные ДР-каналы больше
        # не сопровождается приветствием (раньше сыпалось N одинаковых сообщений)
    except Exception as e:
        log.exception("Ошибка в handle_user_added (channel=%s): %s", channel_id, e)


async def handle_message(raw):
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        log.warning("Не удалось распарсить входящее сообщение: %s", e)
        return

    try:
        if data.get("event") == "user_added":
            await handle_user_added(data)

        if data.get("event") != "posted":
            return

        post = json.loads(data["data"]["post"])

        sender = data["data"].get("sender_name", "")
        message = post.get("message", "")
        channel_id = post.get("channel_id")
        user_id = post.get("user_id")
        post_id = post.get("id")

        if sender in (BOT_USERNAME, f"@{BOT_USERNAME}"):
            return

        # Защита от пустых сообщений (стикеры, файлы без текста, пробелы и т.п.)
        if not message.strip():
            return

        log.info("[%s]: %s", sender, message)

        # Определяем тип канала: "D" — личные сообщения с ботом, всё остальное
        # (O/P/G) — публичные/приватные/групповые каналы.
        try:
            channel_info = await run_in_thread(driver.channels.get_channel, channel_id)
            channel_type = channel_info.get("type", "")
        except Exception as e:
            log.warning("Не удалось получить тип канала %s: %s", channel_id, e)
            channel_type = ""

        if channel_type == "D":
            await _process_dm_message(
                post, sender, message, channel_id, user_id, post_id
            )
        else:
            await _process_channel_message(
                post, sender, message, channel_id, user_id, post_id
            )

    except Exception as e:
        log.exception("Необработанная ошибка в handle_message: %s", e)


async def _post_to_channel(channel_id, message, root_id=None):
    """Отправляет сообщение в канал, логируя ошибку вместо падения."""
    if message is None:
        return
    options = {"channel_id": channel_id, "message": message}
    if root_id:
        options["root_id"] = root_id
    try:
        await run_in_thread(driver.posts.create_post, options=options)
    except Exception as e:
        log.exception("Не удалось отправить сообщение в канал %s: %s", channel_id, e)


def _birthday_label(cd, today):
    """Метка даты для списка ДР: «СЕГОДНЯ» или «15 июля»."""
    if cd == today:
        return "СЕГОДНЯ"
    return f"{cd.day} {common.MONTHS_GENITIVE[cd.month]}"


async def _render_wishlist_readonly(user_id):
    """Свой wish-list для эфемерного вывода (без побочных эффектов/диалогов).

    Формат строки: «номер - название(ссылка) - N шт.», где N = quantity_want
    (сколько человек могут скинуться/подарить этот подарок).
    """
    cursor = await common.db.execute(
        "SELECT * FROM gifts WHERE user_id = ? ORDER BY gift_id", (user_id,)
    )
    gifts = await cursor.fetchall()
    if not gifts:
        return "Ваш список желаний пока пуст. Нажмите «Добавить желание», чтобы добавить первое."
    answer = ["В вашем списке желаний:"]
    for index, gift in enumerate(gifts, start=1):
        name = escape_md(gift["gift_name"])
        title = f"[{name}]({gift['gift_link']})" if gift["gift_link"] else name
        qty = gift["quantity_want"] or 1
        answer.append(f"{index} - {title} - {qty} шт.")
    return "\n".join(answer)


async def _render_my_gifts(user_id):
    """Список 'кому что дарю' для эфемерного вывода."""
    cursor = await common.db.execute(
        """
        SELECT g.*
        FROM gifts g
        JOIN gift_donors gd ON gd.gift_id = g.gift_id
        WHERE gd.user_id = ?
        ORDER BY g.gift_id
        """,
        (user_id,),
    )
    mine = await cursor.fetchall()
    if not mine:
        return "Вы пока никому не выбрали подарок.\nНажмите «Wish-list друга», чтобы выбрать."
    answer = ["Вы планируете подарить:"]
    for gift in mine:
        try:
            cursor = await common.db.execute(
                "SELECT user_fullname, user_birth FROM users WHERE user_id = ?",
                (gift["user_id"],),
            )
            recipient = await cursor.fetchone()
            birth_display = (
                format_birth(recipient["user_birth"]) if recipient else "не указана"
            )

            # Кликабельное упоминание: голый @username без markdown-эскейпа
            # (иначе Mattermost не подсветит имя). Фолбэк — имя из БД / user_id.
            try:
                mm_user = await run_in_thread(driver.users.get_user, gift["user_id"])
                username = mm_user.get("username")
            except Exception:
                username = None
            if username:
                recipient_display = f"@{username}"
            elif recipient is not None:
                recipient_display = escape_md(recipient["user_fullname"])
            else:
                recipient_display = gift["user_id"]

            gift_name = escape_md(gift["gift_name"])
            gift_part = (
                f"[{gift_name}]({gift['gift_link']})"
                if gift["gift_link"]
                else gift_name
            )
            answer.append(f"{recipient_display} - ДР {birth_display} - {gift_part}")
        except Exception as e:
            log.warning("Ошибка строки my-gifts gift_id=%s: %s", gift["gift_id"], e)
    return "\n".join(answer)


async def _render_soon_birthdays(member_ids):
    """
    Список ДР участников канала (member_ids) в ближайшие SOON_BIRTHDAY_DAYS дней,
    начиная с сегодняшнего.
    """
    today = today_local()
    cursor = await common.db.execute(
        "SELECT user_id, user_fullname, user_birth FROM users"
    )
    rows = await cursor.fetchall()

    # cd (дата празднования в этом цикле) -> список имён
    buckets = {}
    for row in rows:
        if row["user_id"] not in member_ids:
            continue
        birth_dt = parse_birth_dt(row["user_birth"])
        if birth_dt is None:
            continue
        cd = next_celebration(today, birth_dt)
        days_until = (cd - today).days
        if 0 <= days_until <= SOON_BIRTHDAY_DAYS:
            buckets.setdefault(cd, []).append(escape_md(row["user_fullname"]))

    if not buckets:
        return "В ближайший месяц дней рождения в этом канале нет."

    lines = ["В ближайший месяц ДР празднуют"]
    for cd in sorted(buckets):
        lines.append(
            f"{_birthday_label(cd, today)} - {common.format_names_ru(buckets[cd])}"
        )
    return "\n".join(lines)


async def _render_all_birthdays(member_ids):
    """
    Список всех ДР участников канала (member_ids) в календарном порядке
    (по месяцу и дню), в том же формате.
    """
    today = today_local()
    cursor = await common.db.execute(
        "SELECT user_id, user_fullname, user_birth FROM users"
    )
    rows = await cursor.fetchall()

    # (месяц, день) -> {"names": [...], "dt": образец birth_dt для проверки «сегодня»}
    buckets = {}
    for row in rows:
        if row["user_id"] not in member_ids:
            continue
        birth_dt = parse_birth_dt(row["user_birth"])
        if birth_dt is None:
            continue
        key = (birth_dt.month, birth_dt.day)
        entry = buckets.setdefault(key, {"names": [], "dt": birth_dt})
        entry["names"].append(escape_md(row["user_fullname"]))

    if not buckets:
        return "В этом канале пока ни у кого не указана дата рождения."

    lines = ["Все дни рождения:"]
    for key in sorted(buckets):
        entry = buckets[key]
        # celebration_date переносит 29.02 → 01.03 в невисокосный год — так корректно
        # определяем, празднуется ли эта дата именно сегодня.
        cd = celebration_date(today.year, entry["dt"])
        if cd == today:
            label = "СЕГОДНЯ"
        else:
            label = f"{key[1]} {common.MONTHS_GENITIVE[key[0]]}"
        lines.append(f"{label} - {common.format_names_ru(entry['names'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Обёртки для кнопок «Мой аккаунт» и модалок (buttons.py).
# Схему и правила держим здесь, в bot.py.
# ---------------------------------------------------------------------------


async def register_user(user_id, birth):
    """
    Регистрирует пользователя с датой рождения birth (объект datetime от
    parse_user_birth). Если уже есть — не трогает. После записи запускает
    точечную проверку ДР (чтобы канал/поздравление появились сразу, если ДР
    уже близко или сегодня). Возвращает (created: bool, error|None).
    """
    try:
        cursor = await common.db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        )
        if await cursor.fetchone() is not None:
            return False, None

        user = await run_in_thread(driver.users.get_user, user_id)
        fullname = build_fullname(user, user_id)
        async with common.transaction():
            await common.db.execute(
                "INSERT INTO users (user_id, user_fullname, user_birth, registration_date) "
                "VALUES (?, ?, ?, ?)",
                (user_id, fullname, str(birth), now_local().isoformat()),
            )
        # Немедленная проверка: вдруг ДР сегодня или в ближайшие дни.
        try:
            await run_checks_for_user(user_id)
        except Exception as e:
            log.warning("run_checks_for_user после регистрации %s: %s", user_id, e)
        return True, None
    except Exception as e:
        log.exception("Ошибка регистрации пользователя %s: %s", user_id, e)
        return False, "Что-то пошло не так при регистрации. Попробуйте позже."


async def apply_admin_birth(target_id, birth_str):
    """
    Вносит/меняет ДР участника от имени админа (вызов из модалки «Админ»).
    birth_str — уже нормализованная строка (str(datetime) от parse_user_birth).
    После записи запускает точечную проверку ДР для участника.
    Возвращает (error|None, normalized|None, is_update).
    """
    try:
        cursor = await common.db.execute(
            "SELECT user_birth FROM users WHERE user_id = ?", (target_id,)
        )
        existing = await cursor.fetchone()
        is_update = existing is not None

        if is_update:
            async with common.transaction():
                await common.db.execute(
                    "UPDATE users SET user_birth = ? WHERE user_id = ?",
                    (birth_str, target_id),
                )
        else:
            fullname = target_id
            try:
                mm_user = await run_in_thread(driver.users.get_user, target_id)
                fullname = build_fullname(mm_user, target_id)
            except Exception as e:
                log.warning("Не удалось получить имя пользователя %s: %s", target_id, e)
            async with common.transaction():
                await common.db.execute(
                    "INSERT INTO users (user_id, user_fullname, user_birth, registration_date) "
                    "VALUES (?, ?, ?, ?)",
                    (target_id, fullname, birth_str, now_local().isoformat()),
                )

        try:
            await run_checks_for_user(target_id)
        except Exception as e:
            log.warning("run_checks_for_user после admin_birth %s: %s", target_id, e)

        return None, format_birth(birth_str), is_update
    except Exception as e:
        log.exception("Ошибка БД при админском изменении ДР: %s", e)
        return "Что-то пошло не так. Попробуйте позже.", None, False


async def delete_account(user_id):
    """
    Полностью удаляет пользователя из бота (ДР + wish-list) — как self delete-me.
    gift_donors (где он даритель) и birthday_notices чистим вручную (нет FK на users),
    его gifts и связанные gift_donors уходят по ON DELETE CASCADE вслед за users.
    """
    async with common.transaction():
        await common.db.execute("DELETE FROM gift_donors WHERE user_id = ?", (user_id,))
        await common.db.execute(
            "DELETE FROM birthday_notices WHERE user_id = ?", (user_id,)
        )
        await common.db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


async def _process_channel_message(post, sender, message, channel_id, user_id, post_id):
    """
    Обрабатывает сообщение из публичного/приватного/группового канала.
    Текстовые команды убраны — вся работа через кнопки приветствия.
    """
    if f"@{BOT_USERNAME}" in message:
        await buttons.post_channel_welcome(channel_id)
    # прочие сообщения в каналах игнорируем


async def _process_dm_message(post, sender, message, channel_id, user_id, post_id):
    """Обрабатывает сообщение из ЛС с ботом."""
    message_lower = message.lower().strip()
    words = message.split()
    words_lower = message_lower.split()
    command = words_lower[0] if words_lower else ""

    if f"@{BOT_USERNAME}" in message:
        # Упоминание бота сбрасывает незавершённый текстовый диалог —
        # пользователь явно хочет начать заново с меню.
        _clear_state(user_id)
        cursor = await common.db.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        )
        if await cursor.fetchone() is not None:
            await buttons.post_menu(channel_id)
        else:
            # Незарегистрированный: эфемерка с кнопкой «Зарегистрироваться».
            # Кнопка нужна, чтобы получить trigger_id и открыть модалку регистрации
            # (из обычного текстового сообщения trigger_id не приходит).
            await buttons.send_registration_prompt(user_id, channel_id)
        return

    reply = None
    handled = False
    state = waiting_for.get(user_id)

    # Если пользователь в активном диалоге — перезапускаем таймаут при каждом его сообщении.
    if state is not None:
        _schedule_timeout(user_id, channel_id)

    # --- Многошаговые диалоги ---

    if isinstance(state, dict) and state.get("state") == "confirm_delete_me":
        handled = True
        if message_lower == "да":
            _clear_state(user_id)
            try:
                await delete_account(user_id)
                reply = "Готово. Ваш аккаунт и wish-list удалены из базы данных бота."
            except Exception as e:
                log.exception("Ошибка БД при удалении пользователя %s: %s", user_id, e)
                reply = "Что-то пошло не так при удалении. Попробуйте позже."
        elif message_lower == "нет":
            _clear_state(user_id)
            reply = "Хорошо, ничего не удаляем."
        else:
            reply = "Пожалуйста, ответьте `Да` или `Нет`"

    elif isinstance(state, dict) and state.get("state") == "confirm_cancel":
        handled = True
        if message_lower == "да":
            _clear_state(user_id)
            try:
                cursor = await common.db.execute(
                    "SELECT * FROM gifts WHERE gift_id = ?", (state["gift_id"],)
                )
                gift = await cursor.fetchone()
                if gift is None:
                    reply = (
                        "Этот подарок уже не существует, видимо именинник его удалил."
                    )
                else:
                    async with common.transaction():
                        result = await common.db.execute(
                            "DELETE FROM gift_donors WHERE gift_id = ? AND user_id = ?",
                            (gift["gift_id"], user_id),
                        )
                    if result.rowcount:
                        reply = (
                            f"Хорошо, вы отказались от подарка «{escape_md(gift['gift_name'])}» "
                            f"для @{state['friend_username']}."
                        )
                    else:
                        reply = "Вы уже не значитесь дарителем этого подарка."
            except Exception as e:
                log.exception("Ошибка БД при confirm_cancel: %s", e)
                reply = "Что-то пошло не так. Попробуйте позже."
        elif message_lower == "нет":
            _clear_state(user_id)
            reply = "Хорошо, ничего не меняем."
        else:
            reply = "Пожалуйста, ответьте `Да` или `Нет`"

    elif isinstance(state, dict) and state.get("state") == "want_make_present?":
        handled = True
        friend_username = state["friend_username"]
        gift_ids = state["gift_ids"]

        gift_num = None
        try:
            gift_num = int(message_lower)
        except ValueError:
            reply = "Введите число в диапазоне из списка желаний или 0, чтоб отказаться от выбора"

        if gift_num is not None:
            if gift_num == 0:
                _clear_state(user_id)
                reply = "Хорошо, к выбору подарков всегда можно вернуться позже, для этого введите `wish-list @username`"
            elif gift_num < 0 or gift_num > len(gift_ids):
                reply = f"Введите число от 0 до {len(gift_ids)}"
            else:
                # Единая логика выбора подарка (та же, что у кнопок) — common.claim_gift:
                # все проверки и запись дарителя в одной транзакции.
                try:
                    problem, gift_name, _wisher_id = await claim_gift(
                        user_id, gift_ids[gift_num - 1]
                    )
                except Exception as e:
                    log.exception("Ошибка БД при выборе подарка: %s", e)
                    problem, gift_name = "Что-то пошло не так. Попробуйте позже.", None
                _clear_state(user_id)
                if problem:
                    reply = problem
                else:
                    reply = (
                        f"Отлично! Вы записаны как даритель подарка «{escape_md(gift_name)}» "
                        f"для пользователя @{friend_username}."
                    )

    elif state == "add_gift":
        handled = True
        if message_lower == "да":
            _set_state(user_id, channel_id, {"action": "add", "option": "add_giftname"})
            reply = ADD_GIFT_INTRO
        elif message_lower == "нет":
            _clear_state(user_id)
            reply = "Хорошо, если что — обращайся!"
        else:
            reply = "Пожалуйста, ответь `Да` или `Нет`"

    elif isinstance(state, dict) and state.get("option") == "add_giftname":
        handled = True
        if message_lower == "0":
            cancel_word = (
                "редактирование" if state.get("action") == "edit" else "добавление"
            )
            _clear_state(user_id)
            reply = f"Хорошо, отменил {cancel_word} этого желания."
        else:
            new_state = {**state, "gift_name": message, "option": "add_giftlink"}
            _set_state(user_id, channel_id, new_state)
            reply = (
                "Окей, записал. Может быть у тебя есть ссылка на желаемый подарок?\n"
                "(напиши `Нет` или вставь ссылку, или 0 для отказа от добавления подарка)"
            )

    elif isinstance(state, dict) and state.get("option") == "add_giftlink":
        handled = True
        link = message_lower
        if link == "0":
            cancel_word = (
                "редактирование" if state.get("action") == "edit" else "добавление"
            )
            _clear_state(user_id)
            reply = f"Хорошо, отменил {cancel_word} этого желания."
        elif link == "нет":
            new_state = {**state, "gift_link": None, "option": "gift_quantity"}
            _set_state(user_id, channel_id, new_state)
            reply = "Хорошо, а сколько человек может подарить такой подарок?"
        else:
            result = urlparse(link)
            if result.scheme in ("http", "https") and result.netloc:
                new_state = {
                    **state,
                    "gift_link": message.strip(),
                    "option": "gift_quantity",
                }
                _set_state(user_id, channel_id, new_state)
                reply = "Хорошо, а сколько человек может подарить такой подарок?"
            else:
                reply = "Пожалуйста, введи корректную ссылку (начинающуюся с http:// или https://), `Нет` или 0 для отказа"

    elif isinstance(state, dict) and state.get("option") == "gift_quantity":
        handled = True
        try:
            quantity = int(message_lower)
        except ValueError:
            reply = "Ответь на вопрос или введи 0 для отказа от добавления подарка"
        else:
            if quantity == 0:
                cancel_word = (
                    "редактирование" if state.get("action") == "edit" else "добавление"
                )
                _clear_state(user_id)
                reply = f"Хорошо, отменил {cancel_word} этого желания."
            elif quantity < 0:
                reply = "Введи положительное число или 0 для отказа"
            else:
                try:
                    if state.get("action") == "edit":
                        async with common.transaction():
                            await common.db.execute(
                                "UPDATE gifts SET gift_name = ?, gift_link = ?, quantity_want = ? WHERE gift_id = ?",
                                (
                                    state["gift_name"],
                                    state["gift_link"],
                                    quantity,
                                    state["gift_id"],
                                ),
                            )
                        _clear_state(user_id)
                        reply = "Готово, желание обновлено!"
                    else:
                        async with common.transaction():
                            await common.db.execute(
                                "INSERT INTO gifts (gift_name, gift_link, quantity_want, user_id) VALUES (?, ?, ?, ?)",
                                (
                                    state["gift_name"],
                                    state["gift_link"],
                                    quantity,
                                    user_id,
                                ),
                            )
                        _clear_state(user_id)
                        reply = "Супер, все данные сохранены!"
                except Exception as e:
                    log.exception("Ошибка БД при сохранении подарка: %s", e)
                    _clear_state(user_id)
                    reply = "Что-то пошло не так, попробуйте позже"

    # --- Обычные команды (вне диалога) ---

    if not handled:
        if message_lower == "richard":
            reply = HELP_TEXT

        elif message_lower == "delete-me":
            _set_state(
                user_id,
                channel_id,
                {"state": "confirm_delete_me"},
            )
            reply = (
                "Вы уверены что хотите удалить аккаунт? "
                "Ваш wish-list тоже будет удалён.\n(Да/Нет)"
            )

        elif command == "add_birth":
            if len(words_lower) < 2:
                reply = "Используй формат: `add_birth DD.MM` или `add_birth DD.MM.YYYY`"
            else:
                birth_str = words_lower[1]
                birth = parse_user_birth(birth_str)

                if birth is None:
                    log.warning("Некорректная дата рождения '%s'", birth_str)
                    reply = (
                        "Некорректная дата. Используй формат `add_birth DD.MM` "
                        "или `add_birth DD.MM.YYYY` (например `07.03` или `07.03.1990`)."
                    )
                    try:
                        await run_in_thread(
                            add_reaction, driver.client.userid, post_id, "x"
                        )
                    except Exception as e:
                        log.warning("Не удалось добавить реакцию: %s", e)
                else:
                    try:
                        cursor = await common.db.execute(
                            "SELECT * FROM users WHERE user_id = ?", (user_id,)
                        )
                        existing = await cursor.fetchone()
                        if existing is None:
                            created, err = await register_user(user_id, birth)
                            if err:
                                reply = err
                            else:
                                try:
                                    await run_in_thread(
                                        add_reaction,
                                        driver.client.userid,
                                        post_id,
                                        "white_check_mark",
                                    )
                                    await run_in_thread(
                                        reply_to_message,
                                        channel_id,
                                        "Записал в БД, спасибо!",
                                        post_id,
                                    )
                                except Exception as e:
                                    log.warning(
                                        "Не удалось отправить подтверждение add_birth: %s",
                                        e,
                                    )
                        else:
                            try:
                                await run_in_thread(
                                    reply_to_message,
                                    channel_id,
                                    "Вы уже вносили дату рождения в БД! "
                                    "Если хотите изменить её — введите `delete-me`, удалите аккаунт и добавьте заново.",
                                    post_id,
                                )
                            except Exception as e:
                                log.warning("Не удалось ответить на add_birth: %s", e)
                    except Exception as e:
                        log.exception(
                            "Ошибка при add_birth для пользователя %s: %s", user_id, e
                        )
                        try:
                            await run_in_thread(
                                add_reaction, driver.client.userid, post_id, "no_entry"
                            )
                        except Exception:
                            pass

        elif command == "delete_gift":
            try:
                cursor = await common.db.execute(
                    "SELECT * FROM gifts WHERE user_id = ? ORDER BY gift_id", (user_id,)
                )
                gifts = await cursor.fetchall()
                gift, err = _validate_gift_number(words, gifts, "delete_gift")
                if err:
                    reply = err
                else:
                    cur_d = await common.db.execute(
                        "SELECT 1 FROM gift_donors WHERE gift_id = ? LIMIT 1",
                        (gift["gift_id"],),
                    )
                    if await cur_d.fetchone():
                        reply = "У этого подарка уже есть даритель, вы не можете его удалить"
                    else:
                        async with common.transaction():
                            await common.db.execute(
                                "DELETE FROM gifts WHERE gift_id = ?",
                                (gift["gift_id"],),
                            )
                        reply = f"Желание «{escape_md(gift['gift_name'])}» удалено из списка"
            except Exception as e:
                log.exception(
                    "Ошибка при delete_gift для пользователя %s: %s", user_id, e
                )
                reply = "Что-то пошло не так. Попробуйте позже."

        elif command == "edit_gift":
            try:
                cursor = await common.db.execute(
                    "SELECT * FROM gifts WHERE user_id = ? ORDER BY gift_id", (user_id,)
                )
                gifts = await cursor.fetchall()
                gift, err = _validate_gift_number(words, gifts, "edit_gift")
                if err:
                    reply = err
                else:
                    cur_d = await common.db.execute(
                        "SELECT 1 FROM gift_donors WHERE gift_id = ? LIMIT 1",
                        (gift["gift_id"],),
                    )
                    if await cur_d.fetchone():
                        reply = (
                            "У этого подарка уже есть даритель, редактировать его нельзя — "
                            "иначе даритель может купить уже не тот подарок, который вы хотели."
                        )
                    else:
                        _set_state(
                            user_id,
                            channel_id,
                            {
                                "action": "edit",
                                "gift_id": gift["gift_id"],
                                "option": "add_giftname",
                            },
                        )
                        reply = (
                            "Окей, давай отредактируем это желание!\n"
                            "Напиши новое название желания.\n"
                            "(в любой момент можно ввести 0, чтобы отказаться от редактирования)"
                        )
            except Exception as e:
                log.exception(
                    "Ошибка при edit_gift для пользователя %s: %s", user_id, e
                )
                reply = "Что-то пошло не так. Попробуйте позже."

        elif command == "cancel":
            if len(words) == 1:
                reply = "Используй формат: `cancel @username`"
            elif len(words) > 2:
                reply = "Слишком много аргументов. Используй формат: `cancel @username`"
            else:
                try:
                    friend_id, friend_username = await run_in_thread(
                        resolve_user_id, words[1]
                    )
                    if friend_id is None:
                        reply = f"Не нашёл пользователя `{words[1]}`. Проверьте никнейм и попробуйте снова."
                    else:
                        cursor = await common.db.execute(
                            """
                            SELECT g.*
                            FROM gifts g
                            JOIN gift_donors gd ON gd.gift_id = g.gift_id
                            WHERE g.user_id = ? AND gd.user_id = ?
                            LIMIT 1
                            """,
                            (friend_id, user_id),
                        )
                        found_gift = await cursor.fetchone()

                        if found_gift is None:
                            reply = f"Вы не выбирали подарок для пользователя @{friend_username}."
                        else:
                            _set_state(
                                user_id,
                                channel_id,
                                {
                                    "state": "confirm_cancel",
                                    "gift_id": found_gift["gift_id"],
                                    "friend_username": friend_username,
                                },
                            )
                            reply = (
                                f"Вы планировали подарить «{escape_md(found_gift['gift_name'])}» "
                                f"пользователю @{friend_username}, хотите отказаться? (да/нет)"
                            )
                except Exception as e:
                    log.exception(
                        "Ошибка при cancel для пользователя %s: %s", user_id, e
                    )
                    reply = "Что-то пошло не так. Попробуйте позже."

        elif command == "wish-list":
            if len(words) == 1:
                try:
                    reply = await _render_own_wishlist(user_id, channel_id)
                except Exception as e:
                    log.exception(
                        "Ошибка при wish-list для пользователя %s: %s", user_id, e
                    )
                    reply = "Что-то пошло не так. Попробуйте позже."

            elif len(words) == 2:
                target_raw = words[1]
                try:
                    friend_id, friend_username = await run_in_thread(
                        resolve_user_id, target_raw
                    )

                    if friend_id == user_id:
                        reply = await _render_own_wishlist(user_id, channel_id)
                    elif friend_id is None:
                        reply = f"Не нашёл пользователя `{target_raw}`. Проверьте никнейм и попробуйте снова."
                    else:
                        cursor = await common.db.execute(
                            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
                        )
                        if await cursor.fetchone() is None:
                            reply = (
                                "Чтобы посмотреть wish-list другого человека и выбрать подарок для него, "
                                "тебе необходимо добавить свою дату рождения!\n"
                                "Введи `add_birth DD.MM` или `add_birth DD.MM.YYYY`"
                            )
                        elif (
                            existing_gift_name := await find_existing_gift_for_donor(
                                user_id, friend_id
                            )
                        ) is not None:
                            reply = (
                                f"Вы уже выбрали «{escape_md(existing_gift_name)}» в качестве подарка для "
                                f"@{friend_username}. Дарить можно только один подарок одному человеку."
                            )
                        else:
                            cursor = await common.db.execute(
                                """
                                SELECT g.*,
                                       COUNT(gd.user_id) AS donor_count
                                FROM gifts g
                                LEFT JOIN gift_donors gd ON gd.gift_id = g.gift_id
                                WHERE g.user_id = ?
                                GROUP BY g.gift_id
                                HAVING donor_count < g.quantity_want
                                ORDER BY g.gift_id
                                """,
                                (friend_id,),
                            )
                            available_gifts = await cursor.fetchall()

                            if not available_gifts:
                                reply = f"У пользователя @{friend_username} нет доступных желаний в списке :("
                            else:
                                answer = [
                                    f"В списке желаний у @{friend_username} уже есть:"
                                ]
                                for index, gift in enumerate(available_gifts, start=1):
                                    answer.append(format_gift_line(gift, index))
                                answer.append(
                                    "\nЕсли хотите выбрать подарок для пользователя, "
                                    "укажите его порядковый номер или введите 0, чтобы отказаться"
                                )
                                _set_state(
                                    user_id,
                                    channel_id,
                                    {
                                        "state": "want_make_present?",
                                        "friend_id": friend_id,
                                        "friend_username": friend_username,
                                        "gift_ids": [
                                            g["gift_id"] for g in available_gifts
                                        ],
                                    },
                                )
                                reply = "\n".join(answer)
                except Exception as e:
                    log.exception(
                        "Ошибка при wish-list @username для пользователя %s: %s",
                        user_id,
                        e,
                    )
                    reply = "Что-то пошло не так. Попробуйте позже."

            else:
                reply = "Слишком много аргументов. Используй формат: `wish-list` или `wish-list @username`"

        elif message_lower == "my-gifts":
            try:
                reply = await _render_my_gifts(user_id)
            except Exception as e:
                log.exception("Ошибка при my-gifts для пользователя %s: %s", user_id, e)
                reply = "Что-то пошло не так. Попробуйте позже."

        elif message_lower == "add_gift":
            _set_state(user_id, channel_id, {"action": "add", "option": "add_giftname"})
            reply = ADD_GIFT_INTRO

        else:
            return

    if reply is not None:
        try:
            await run_in_thread(
                driver.posts.create_post,
                options={"channel_id": channel_id, "message": reply},
            )
        except Exception as e:
            log.exception("Не удалось отправить ответ пользователю %s: %s", user_id, e)


async def _cancel_task(task):
    """Отменяет задачу и дожидается её завершения, поглощая CancelledError."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("Ошибка при отмене задачи: %s", e)


async def _graceful_shutdown(scheduler_task):
    """Корректно гасим планировщик, активные таймауты диалогов и закрываем БД."""
    log.info("Завершаю работу, выполняю graceful shutdown...")

    await _cancel_task(scheduler_task)

    for task in list(_timeout_tasks.values()):
        await _cancel_task(task)
    _timeout_tasks.clear()

    buttons.cancel_all_pending_timeouts()

    if common.db:
        try:
            await common.db.close()
            log.info("Соединение с БД закрыто")
        except Exception as e:
            log.warning("Ошибка при закрытии БД: %s", e)


async def main():
    await init_db()

    try:
        await run_in_thread(driver.login)
        common.validate_connection()
    except SystemExit:
        raise
    except Exception as e:
        log.critical("Не удалось подключиться к Mattermost: %s", e)
        sys.exit(1)

    log.info("@%s запущен", BOT_USERNAME)
    driver.websocket = Websocket(driver.options, driver.client.token)
    scheduler_task = asyncio.create_task(birthday_scheduler())
    buttons.setup(
        render_soon=_render_soon_birthdays,
        render_all=_render_all_birthdays,
        render_wishlist=_render_wishlist_readonly,
        render_mygifts=_render_my_gifts,
        get_birthday=_get_birthday,
        delete_account=delete_account,
        apply_admin_birth=apply_admin_birth,
        register_user=register_user,
    )
    button_runner = await buttons.start_button_server()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    ws_task = asyncio.create_task(driver.websocket.connect(handle_message))
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        await asyncio.wait({ws_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if ws_task.done() and not ws_task.cancelled():
            exc = ws_task.exception()
            if exc is not None:
                log.error("Соединение с вебсокетом завершилось с ошибкой: %s", exc)
    finally:
        try:
            disconnect = getattr(driver.websocket, "disconnect", None)
            if callable(disconnect):
                disconnect()
        except Exception as e:
            log.warning("Ошибка при отключении вебсокета: %s", e)

        await _cancel_task(ws_task)
        await _cancel_task(stop_task)
        await _graceful_shutdown(scheduler_task)
        try:
            await button_runner.cleanup()
        except Exception as e:
            log.warning("Ошибка при остановке HTTP-сервера кнопок: %s", e)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
