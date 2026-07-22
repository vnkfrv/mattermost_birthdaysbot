# -*- coding: utf-8 -*-
"""
Кнопки Richard (каналы + ЛС) + нативные диалоги (модалки) Mattermost.

Каналы:  @упоминание -> приветствие с кнопками «Ближайшие/Все дни рождения», «Админ».
ЛС:      @упоминание -> меню (если зарегистрирован) или эфемерка с кнопкой
         «Зарегистрироваться» (если нет). Кнопка нужна, чтобы получить trigger_id
         и открыть модалку регистрации (из обычного текста trigger_id не приходит).

Регистрация (модалка в 2 шага):
  шаг 1 — ввод даты рождения + описание бота;
  шаг 2 — подтверждение «Ваш ДР — 15 июля?» (submit «Да, всё верно», отмена —
          штатная Cancel = «ввести другую дату»). По submit — запись через
          register_user (bot.py), эфемерное подтверждение.

Кнопка «Админ» (модалка в 2 шага):
  шаг 1 — выбор участника канала + дата; шаг 2 — подтверждение. По submit — запись
  через apply_admin_birth (bot.py) и ЛС-уведомление имениннику.

Блок «Желания» и «Желания других» — модалки добавления/удаления/редактирования,
wish-list друга, выбор подарка (multi-step), «Я дарю», «Отказаться от подарка»
(модалка выбора подарка -> эфемерное подтверждение).

Блок «Мой аккаунт»:
  • «Моё послание» — модалка с textarea; сохраняется в users.letter_to_others.
  • «Удалить меня» — модалка-подтверждение; по submit удаляются ДР и wish-list,
    затем чистятся все посты в ЛС-канале, и шлётся эфемерка.

Эндпоинты (aiohttp, общий asyncio-цикл бота):
  POST /button  — нажатия кнопок и выбор в select-меню
  POST /dialog  — отправка форм-диалогов
  GET  /health

ENV: BOT_PUBLIC_URL, BUTTON_PORT (по умолчанию 8080).
"""

import asyncio
import os
from urllib.parse import urlparse

from aiohttp import web

import common
from common import (
    driver,
    log,
    run_in_thread,
    get_channel_member_ids,
    get_dm_channel_id,
    escape_md,
    parse_user_birth,
    format_birth,
    claim_gift,
    dm_user,
    is_channel_admin,
    is_team_admin,
    MONTHS_GENITIVE,
)

BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "")
BUTTON_PORT = int(os.getenv("BUTTON_PORT", "8080"))

# Цвет левой полосы attachment'а (и, по возможности, кнопок).
# Главное меню — голубой; вложенные разделы («внутри» кнопки) — пыльная роза.
HOME_COLOR = "#1C7CD6"
SECTION_COLOR = "#C08497"


# =========================================================================
#  ТЕКСТЫ
# =========================================================================

CHANNEL_WELCOME_TEXT = (
    "Привет, я Ричард! Тут вы можете составить список желаний, "
    "выбрать подарок коллеге, а еще не забудете поздравить именинника вовремя! "
    "Чтобы посмотреть ближайшие дни рождения, нажмите на кнопку ниже"
)

REGISTER_PROMPT = (
    "Привет, я Ричард! Тут вы можете составить список желаний, "
    "выбрать подарок коллеге, а еще не забудете поздравить именинника вовремя! "
    "Чтобы начать, зарегистрируйтесь — нажмите кнопку ниже."
)

REGISTER_INTRO = (
    "Я Ричард — праздничный бот. Я помогу составить список желаний, выбрать "
    "подарок коллеге и не забыть поздравить именинника вовремя.\n\n"
    "Для начала введите вашу дату рождения в формате ДД.ММ или ДД.ММ.ГГГГ."
)

MENU_INTRO = (
    "Привет, я Ричард! Тут вы можете составить список желаний, "
    "выбрать подарок коллеге, а еще не забудете поздравить именинника вовремя! "
    "Вот мой функционал:"
)

LETTER_INTRO = (
    "За 7 дней до вашего ДР будет создана временная группа, в которую добавят "
    "участников из общих с вами каналов. Они смогут выбрать для вас подарок из "
    "вашего списка желаний, а также прочитать послание, которое вы напишете ниже. "
    "В нём можно пригласить коллег отметить ДР на кухне офиса или в заведении, "
    "рассказать, что вам нравится или не нравится из подарков, или, возможно, "
    "попросить их не поздравлять вас. Им будет немного грустно, но, думаю, "
    "они вас поймут."
)

DELETE_ME_CONFIRM_TEXT = (
    "Вы действительно хотите удалить свою дату рождения из системы ДР-бота? "
    "Данное действие приведёт к удалению и вашего списка желаний."
)


def _date_ru(birth):
    """«15 июля» из объекта datetime (parse_user_birth)."""
    return f"{birth.day} {MONTHS_GENITIVE[birth.month]}"


# =========================================================================
#  RENDER-ФУНКЦИИ И ЛОГИКА ИЗ bot.py (через setup)
# =========================================================================

_render_soon = None
_render_all = None
_render_wishlist = None
_render_mygifts = None
_render_friend_wishlist = None  # необязательная; если None — встроенный рендер
_get_birthday = None  # (user_id) -> строка вида «15 марта» или None; из bot.py

_delete_account = None  # async (user_id) -> None: удалить ДР и wish-list
_apply_admin_birth = None  # async (target_id, birth_str) -> (error, normalized, is_update)
_register_user = None  # async (user_id, birth) -> (created: bool, error|None)


def setup(
    render_soon=None,
    render_all=None,
    render_wishlist=None,
    render_mygifts=None,
    render_friend_wishlist=None,
    get_birthday=None,
    delete_account=None,
    apply_admin_birth=None,
    register_user=None,
):
    global _render_soon, _render_all, _render_wishlist, _render_mygifts
    global _render_friend_wishlist, _get_birthday
    global _delete_account, _apply_admin_birth, _register_user
    if render_soon is not None:
        _render_soon = render_soon
    if render_all is not None:
        _render_all = render_all
    if render_wishlist is not None:
        _render_wishlist = render_wishlist
    if render_mygifts is not None:
        _render_mygifts = render_mygifts
    if render_friend_wishlist is not None:
        _render_friend_wishlist = render_friend_wishlist
    if get_birthday is not None:
        _get_birthday = get_birthday
    if delete_account is not None:
        _delete_account = delete_account
    if apply_admin_birth is not None:
        _apply_admin_birth = apply_admin_birth
    if register_user is not None:
        _register_user = register_user


# =========================================================================
#  КНОПКИ / БЛОКИ
# =========================================================================


def _action(action_id, label, style=None):
    btn = {
        # id уходит в URL /api/v4/posts/{post_id}/actions/{id}. В маршрутизаторе
        # Mattermost сегмент {id} ограничен [A-Za-z0-9], поэтому подчёркивания
        # ломают роут -> 404. Убираем их из id, логическое имя оставляем в
        # context.action (по нему и диспетчеризует обработчик).
        "id": action_id.replace("_", ""),
        "name": label,
        "type": "button",
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/button",
            "context": {"action": action_id},
        },
    }
    if style:
        btn["style"] = style
    return btn


def _action_ctx(action_id, label, extra_context=None, style=None):
    """Кнопка с произвольным доп-контекстом (например, wisher_id)."""
    ctx = {"action": action_id}
    if extra_context:
        ctx.update(extra_context)
    btn = {
        "id": action_id.replace("_", ""),
        "name": label,
        "type": "button",
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/button",
            "context": ctx,
        },
    }
    if style:
        btn["style"] = style
    return btn


def _select_action(action_id, placeholder, options, extra_context=None):
    """Интерактивное select-меню в attachment (шлёт POST на /button).

    Выбранное значение приходит в context.selected_option (это option['value']).
    extra_context позволяет добавить в контекст сопутствующие данные (например friend_id).
    """
    ctx = {"action": action_id}
    if extra_context:
        ctx.update(extra_context)
    return {
        "id": action_id.replace("_", ""),
        "name": placeholder,
        "type": "select",
        "options": options,
        "integration": {
            "url": f"{BOT_PUBLIC_URL}/button",
            "context": ctx,
        },
    }


def channel_welcome_attachments():
    return [
        {
            "text": CHANNEL_WELCOME_TEXT,
            "actions": [
                _action("soon", "Ближайшие дни рождения"),
                _action("all", "Все дни рождения"),
                _action("admin", "Админ"),
            ],
        }
    ]


async def post_channel_welcome(channel_id):
    await run_in_thread(
        driver.posts.create_post,
        options={
            "channel_id": channel_id,
            "message": "",
            "props": {"attachments": channel_welcome_attachments()},
        },
    )


def menu_attachments():
    return [
        {
            "color": HOME_COLOR,
            "text": "Ваш список желаний",
            "actions": [
                _action("wishlist", "Мой Wish-list"),
                _action("add_gift", "Добавить желание"),
                _action("delete_gift", "Удалить желание"),
                _action("edit_gift", "Редактировать желание"),
            ],
        },
        {
            "color": HOME_COLOR,
            "text": "Желания других",
            "actions": [
                _action("friend_wishlist", "Wish-list друга"),
                _action("choose_gift", "Выбрать подарок"),
                _action("mygifts", "Я дарю"),
                _action("cancel_gift", "Отказаться от подарка"),
            ],
        },
        {
            "color": HOME_COLOR,
            "text": "Мой аккаунт",
            "actions": [
                _action("my_letter", "Моё послание"),
                _action("delete_me", "Удалить меня"),
            ],
        },
    ]


async def post_menu(channel_id):
    await run_in_thread(
        driver.posts.create_post,
        options={
            "channel_id": channel_id,
            "message": MENU_INTRO,
            "props": {"attachments": menu_attachments()},
        },
    )


# =========================================================================
#  САМООБНОВЛЯЮЩЕЕСЯ СООБЩЕНИЕ (личное меню + регистрация)
# =========================================================================
#
# Идея: в ЛС живёт ОДИН пост, который переписывается на месте через patch_post
# при каждом клике по кнопке — так не нужны ни права админа (create_ephemeral),
# ни новые посты на каждый ответ (native ephemeral_text в ЛС не рендерится).
# post_id клика приходит в data['post_id']; после модалок протаскиваем его через
# dialog['state'] и правим тот же пост на submit.


def _back_button():
    # Цвет кнопки через hex в style поддерживается не во всех версиях Mattermost и
    # может «сломать» кнопку, поэтому цвет несёт полоса attachment'а (SECTION_COLOR),
    # а кнопку оставляем дефолтной. При желании включить hex-стиль — style=SECTION_COLOR.
    return _action("home", "Вернуться на Главную")


def _register_button():
    return _action("register", "Зарегистрироваться")


def _section_attachments(text):
    """Вложенный раздел: результат + кнопка «Вернуться на Главную», розовая полоса."""
    return [{"color": SECTION_COLOR, "text": text, "actions": [_back_button()]}]


def _register_prompt_attachments():
    return [{
        "color": HOME_COLOR,
        "text": REGISTER_PROMPT,
        "actions": [_register_button()],
    }]


def _register_cancelled_attachments():
    return [{
        "color": HOME_COLOR,
        "text": "Регистрация отменена. Нажмите на кнопку, чтобы попробовать ещё раз.",
        "actions": [_register_button()],
    }]


def _deleted_attachments():
    return [{
        "color": HOME_COLOR,
        "text": "Ваша дата рождения и список желаний удалены из ДР-бота. "
                "Чтобы снова пользоваться ботом — зарегистрируйтесь.",
        "actions": [_register_button()],
    }]


async def _patch_view(post_id, message, attachments):
    """Переписывает существующий пост бота (текст + attachments) на месте.

    Правка своего поста не требует прав админа (edit_post есть у автора). Работает
    и в ЛС. post_id пустой -> тихо выходим (нечего править).
    """
    if not post_id:
        log.warning("patch_view: пустой post_id — пост не обновлён")
        return
    try:
        await run_in_thread(
            driver.posts.patch_post,
            post_id,
            {"message": message, "props": {"attachments": attachments}},
        )
    except Exception as e:
        log.warning("Не удалось обновить пост %s: %s", post_id, e)


async def _show_home(post_id):
    """Возврат к главному меню (голубые блоки)."""
    await _patch_view(post_id, MENU_INTRO, menu_attachments())


async def _show_section(post_id, text):
    """Показ результата раздела (розовая полоса + «Вернуться на Главную»)."""
    await _patch_view(post_id, "", _section_attachments(text))


async def _dm_post(user_id, message="", attachments=None):
    """Личное сообщение боту→пользователю (текст и/или attachments) без прав админа.

    Заменяет эфемерные посты там, где эфемерку без system_admin показать нельзя
    (ответы после submit диалога, интерактивные меню, проактивные уведомления).
    Возвращает id созданного поста или None; ошибки только логируются.
    """
    try:
        channel_id = await run_in_thread(get_dm_channel_id, user_id)
        if not channel_id:
            return None
        options = {"channel_id": channel_id, "message": message}
        if attachments is not None:
            options["props"] = {"attachments": attachments}
        post = await run_in_thread(driver.posts.create_post, options=options)
        return post.get("id") if isinstance(post, dict) else None
    except Exception as e:
        log.warning("Не удалось отправить ЛС пользователю %s: %s", user_id, e)
        return None


async def send_registration_prompt(user_id, channel_id):
    """Первый пост в ЛС: приветствие + кнопка «Зарегистрироваться».

    Дальше это же сообщение переписывается на месте (регистрация -> подтверждение ->
    главное меню). channel_id — для совместимости сигнатуры, не используется.
    """
    await _dm_post(user_id, "", _register_prompt_attachments())


async def _send_ephemeral(user_id, channel_id, text):
    """Приватное уведомление пользователю — личным сообщением бота (видно только ему).

    Без прав system_admin эфемерку через API не создать, а нативный `ephemeral_text`
    в ответе на кнопку Mattermost не рендерит в личных каналах (ЛС). ЛС-пост работает
    везде и без админа. channel_id сохранён в сигнатуре для совместимости со старыми
    вызовами, но не используется — сообщение всегда уходит в личку бота.
    """
    await _dm_post(user_id, text)


async def _post_dismissible(channel_id, text):
    """Ответ канальной кнопки: обычный пост бота в ОСНОВНОЙ ленте + кнопка «Скрыть».

    Эфемерку в ленте без прав system_admin показать нельзя (REST /posts/ephemeral —
    админский, а ephemeral_text из ответа на кнопку MM прикрепляет к треду, где его
    не видно при CollapsedThreads=always_on). Поэтому отвечаем обычным постом:
    видно сразу в переписке, а кнопка «Скрыть» удаляет пост (свой пост бот удаляет
    без прав админа). Содержимое канальных кнопок (списки ДР) — публичное.
    """
    attachment = {
        "color": HOME_COLOR,
        "text": text,
        "actions": [_action("dismiss", "Скрыть")],
    }
    await _create_post(channel_id, "", [attachment])


async def _create_post(channel_id, message="", attachments=None):
    """Реальный пост; возвращает id созданного поста (или None)."""
    options = {"channel_id": channel_id, "message": message}
    if attachments is not None:
        options["props"] = {"attachments": attachments}
    post = await run_in_thread(driver.posts.create_post, options=options)
    return post.get("id") if isinstance(post, dict) else None


def birthday_channel_actions(wisher_id, has_wishlist, has_letter):
    """Кнопки для поста во временном ДР-канале (используется из scheduler.py).

    Кнопку показываем только если есть что показывать: список желаний / послание.
    """
    actions = []
    if has_wishlist:
        actions.append(
            _action_ctx("bday_wishlist", "Список желаний", {"wisher_id": wisher_id})
        )
        actions.append(
            _action_ctx("bday_choose_gift", "Выбрать подарок", {"wisher_id": wisher_id})
        )
    if has_letter:
        actions.append(
            _action_ctx("bday_letter", "Послание от именинника", {"wisher_id": wisher_id})
        )
    return actions


# =========================================================================
#  ПОЛЬЗОВАТЕЛИ (имена)
# =========================================================================


def _fmt_name(user):
    """Красивое имя из объекта пользователя MM (с markdown-эскейпом)."""
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full = (first + " " + last).strip()
    username = user.get("username") or ""
    if full and username:
        return f"{escape_md(full)} (@{username})"
    if full:
        return escape_md(full)
    if username:
        return f"@{username}"
    return "пользователь"


def _plain_name(user):
    """То же, что _fmt_name, но без markdown-эскейпа — для текста опций select."""
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    full = (first + " " + last).strip()
    username = user.get("username") or ""
    if full and username:
        return f"{full} (@{username})"
    if full:
        return full
    if username:
        return f"@{username}"
    return "пользователь"


async def _display_name(user_id):
    try:
        u = await run_in_thread(driver.users.get_user, user_id)
    except Exception:
        return "пользователь"
    return _fmt_name(u or {})


async def _plain_name_by_id(user_id):
    try:
        u = await run_in_thread(driver.users.get_user, user_id)
    except Exception:
        return "пользователь"
    return _plain_name(u or {})


async def _username(user_id):
    """Голый username (для @упоминания в подтверждении)."""
    try:
        u = await run_in_thread(driver.users.get_user, user_id)
        return u.get("username") or "—"
    except Exception:
        return "—"


async def _birthday_line(user_id):
    """Строка с ДР именинника через инъектированный get_birthday; None, если нет."""
    if _get_birthday is None:
        return None
    try:
        return await _get_birthday(user_id)
    except Exception as e:
        log.warning("get_birthday упал для %s: %s", user_id, e)
        return None


# =========================================================================
#  РЕГИСТРАЦИЯ (модалка в 2 шага)
# =========================================================================


async def _open_register_dialog(data):
    """Кнопка «Зарегистрироваться»: открывает шаг 1 модалки регистрации."""
    user_id = data.get("user_id")
    post_id = data.get("post_id")

    # Если уже зарегистрирован (нажал старую кнопку) — просто покажем меню.
    cur = await common.db.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
    )
    if await cur.fetchone() is not None:
        await _show_home(post_id)
        return

    dialog = {
        "callback_id": "register",
        "title": "Регистрация",
        "introduction_text": REGISTER_INTRO,
        "submit_label": "Далее",
        # В state кладём post_id сообщения-приглашения: после submit по нему
        # перепишем этот же пост на экран подтверждения.
        "state": post_id or "",
        "elements": [
            {
                "display_name": "Дата",
                "name": "birth",
                "type": "text",
                "placeholder": "ДД.ММ или ДД.ММ.ГГГГ",
                "help_text": "Например: 15.07 или 15.07.1990",
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


# ---------------------------------------------------------------------------
# TTL для «зависших» подтверждений (регистрация / админское изменение ДР).
# Если пользователь ввёл данные в модалке, но 5 минут не нажимал кнопку
# подтверждения/отмены — запись стирается, а ему уходит эфемерка об этом.
# ---------------------------------------------------------------------------

PENDING_TIMEOUT_SECONDS = 300  # 5 минут

# (kind, user_id) -> asyncio.Task; kind различает регистрацию и админ-сценарий,
# чтобы их таймеры не перетирали друг друга у одного пользователя.
_pending_timeout_tasks = {}


def _cancel_pending_timeout(kind, user_id):
    """Отменяет таймер подтверждения, если он есть (вызов при confirm/cancel)."""
    task = _pending_timeout_tasks.pop((kind, user_id), None)
    if task and not task.done():
        task.cancel()


def _schedule_pending_timeout(kind, storage, user_id, channel_id, message, post_id=None):
    """
    Запускает таймер на PENDING_TIMEOUT_SECONDS для записи storage[user_id].
    По истечении — стирает запись и сообщает пользователю.

    Если передан post_id (сценарий самообновляющегося сообщения) — переписываем
    этот пост на экран с текстом message и кнопкой «Зарегистрироваться». Иначе
    (админский сценарий в канале) — шлём личное сообщение.
    Повторный вызов (пользователь заново открыл модалку) перезапускает таймер.
    """
    _cancel_pending_timeout(kind, user_id)

    async def _expire():
        await asyncio.sleep(PENDING_TIMEOUT_SECONDS)
        if user_id in storage:
            storage.pop(user_id, None)
            _pending_timeout_tasks.pop((kind, user_id), None)
            try:
                if post_id:
                    await _patch_view(post_id, "", [{
                        "color": HOME_COLOR,
                        "text": message,
                        "actions": [_register_button()],
                    }])
                else:
                    await _send_ephemeral(user_id, channel_id, message)
            except Exception as e:
                log.warning(
                    "Не удалось отправить сообщение о таймауте %s/%s: %s",
                    kind, user_id, e,
                )

    _pending_timeout_tasks[(kind, user_id)] = asyncio.create_task(_expire())


def cancel_all_pending_timeouts():
    """Гасит все таймеры подтверждений (вызывается при graceful shutdown из bot.py)."""
    for task in list(_pending_timeout_tasks.values()):
        if not task.done():
            task.cancel()
    _pending_timeout_tasks.clear()


REGISTER_TIMEOUT_MESSAGE = (
    "Время подтверждения регистрации истекло (5 минут), введённая дата сброшена. "
    "Позовите меня снова, чтобы начать заново."
)

# Недописанные регистрации между шагом ввода даты и подтверждением.
# Ключ — user_id, значение — объект datetime. Данные держим на стороне бота,
# а подтверждение показываем кнопками в самом сообщении (пустая модалка-форма
# в Mattermost не сабмитится — submit просто не срабатывает).
_pending_registration = {}


async def _handle_register_dialog(user_id, post_id, submission):
    """Шаг 1 регистрации: ввод даты -> тот же пост переписываем на экран
    подтверждения с кнопками «Да, всё верно» / «Отмена».

    Разобранную дату кладём в _pending_registration[user_id]; подтверждение
    приходит нажатием кнопки (action 'register_confirm' / 'register_cancel').
    Модалка при этом закрывается (возвращаем пустой ответ).
    """
    raw = (submission.get("birth") or "").strip()
    birth = parse_user_birth(raw)
    if birth is None:
        return web.json_response({"errors": {"birth":
            "Некорректная дата. Введите ДД.ММ или ДД.ММ.ГГГГ "
            "(например 15.07 или 15.07.1990)."}})

    # Уже зарегистрирован — не плодим дубликаты, просто показываем меню.
    cur = await common.db.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
    )
    if await cur.fetchone() is not None:
        _pending_registration.pop(user_id, None)
        await _show_home(post_id)
        return web.json_response({})

    _pending_registration[user_id] = birth
    _schedule_pending_timeout(
        "register", _pending_registration, user_id, post_id,
        REGISTER_TIMEOUT_MESSAGE, post_id=post_id,
    )

    confirm_view = [{
        "color": HOME_COLOR,
        "text": f"Подтвердите, что ваш день рождения — {_date_ru(birth)} "
                f"({format_birth(str(birth))}).",
        "actions": [
            _action("register_confirm", "Да, всё верно"),
            _action("register_cancel", "Отмена"),
        ],
    }]
    await _patch_view(post_id, "", confirm_view)
    # Закрываем модалку ввода даты.
    return web.json_response({})


async def _confirm_registration(data):
    """Кнопка «Да, всё верно»: регистрируем по сохранённой дате и показываем меню."""
    user_id = data.get("user_id")
    post_id = data.get("post_id")
    birth = _pending_registration.pop(user_id, None)
    _cancel_pending_timeout("register", user_id)
    if birth is None:
        await _show_section(post_id, "Не удалось определить дату (сессия истекла). "
                                     "Позовите меня снова.")
        return

    if _register_user is None:
        log.error("register_user не подключён в setup()")
        await _show_section(post_id, "Регистрация сейчас недоступна.")
        return

    created, error = await _register_user(user_id, birth)
    if error:
        await _show_section(post_id, error)
        return

    # Успех (и «уже был») — показываем главное меню в этом же сообщении.
    await _show_home(post_id)


async def _cancel_registration(data):
    """Кнопка «Отмена» при подтверждении регистрации: экран «Регистрация отменена»."""
    user_id = data.get("user_id")
    post_id = data.get("post_id")
    _pending_registration.pop(user_id, None)
    _cancel_pending_timeout("register", user_id)
    await _patch_view(post_id, "", _register_cancelled_attachments())


# =========================================================================
#  ДИАЛОГИ (модалки) — желания
# =========================================================================


async def _open_dialog(trigger_id, dialog, post_id=None):
    """Открывает модальное окно. submit прилетит на /dialog.

    post_id (если передан) кладём в dialog['state'] — так после submit мы знаем,
    какой пост переписать (модель самообновляющегося сообщения). Явно заданный в
    dialog['state'] не перетираем.
    """
    if post_id is not None and not dialog.get("state"):
        dialog["state"] = post_id or ""
    if not trigger_id:
        log.error(
            "open_dialog: пустой trigger_id — Mattermost не прислал его с нажатием"
        )
    try:
        await run_in_thread(
            driver.integration_actions.open_dialog,
            {
                "trigger_id": trigger_id,
                "url": f"{BOT_PUBLIC_URL}/dialog",
                "dialog": dialog,
            },
        )
    except Exception as e:
        log.exception("open_dialog не удался (trigger_id=%s): %s", trigger_id, e)
        raise


async def _gifts_without_donor(user_id):
    """Желания пользователя, на которые ещё нет дарителя (их можно удалять/редактировать)."""
    cursor = await common.db.execute(
        """
        SELECT g.gift_id, g.gift_name, g.gift_link, g.quantity_want,
               COUNT(gd.user_id) AS donor_count
        FROM gifts g
        LEFT JOIN gift_donors gd ON gd.gift_id = g.gift_id
        WHERE g.user_id = ?
        GROUP BY g.gift_id
        HAVING donor_count = 0
        ORDER BY g.gift_id
        """,
        (user_id,),
    )
    return await cursor.fetchall()


async def _open_add_gift_dialog(data):
    dialog = {
        "callback_id": "add_gift",
        "title": "Добавить",
        "submit_label": "Добавить",
        "elements": [
            {
                "display_name": "Что подарить",
                "name": "gift_name",
                "type": "text",
                "max_length": 300,
            },
            {
                "display_name": "Ссылка",
                "name": "gift_link",
                "type": "text",
                "subtype": "url",
                "optional": True,
                "help_text": "Оставьте пустым, если ссылки нет",
            },
            {
                "display_name": "Кол-во",
                "name": "quantity",
                "type": "text",
                "subtype": "number",
                "default": "1",
                "help_text": "Сколько человек могут подарить этот подарок",
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


async def _open_delete_gift_dialog(data):
    user_id = data.get("user_id")
    gifts = await _gifts_without_donor(user_id)
    if not gifts:
        await _show_section(
            data.get("post_id"),
            "У вас нет желаний, которые можно удалить "
            "(список пуст или на все уже есть даритель).",
        )
        return
    options = [{"text": g["gift_name"], "value": str(g["gift_id"])} for g in gifts]
    dialog = {
        "callback_id": "delete_gift",
        "title": "Удаление",
        "submit_label": "Удалить",
        "elements": [
            {
                "display_name": "Желание",
                "name": "gift_id",
                "type": "select",
                "options": options,
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


async def _open_edit_gift_dialog(data):
    user_id = data.get("user_id")
    gifts = await _gifts_without_donor(user_id)
    if not gifts:
        await _show_section(
            data.get("post_id"),
            "У вас нет желаний, которые можно редактировать "
            "(список пуст или на все уже есть даритель).",
        )
        return
    options = [{"text": g["gift_name"], "value": str(g["gift_id"])} for g in gifts]
    dialog = {
        "callback_id": "edit_gift",
        "title": "Правка",
        "submit_label": "Сохранить",
        "elements": [
            {
                "display_name": "Желание",
                "name": "gift_id",
                "type": "select",
                "options": options,
            },
            {
                "display_name": "Название",
                "name": "gift_name",
                "type": "text",
                "optional": True,
                "max_length": 300,
                "help_text": "Оставьте пустым, чтобы не менять",
            },
            {
                "display_name": "Новая ссылка",
                "name": "gift_link",
                "type": "text",
                "subtype": "url",
                "optional": True,
                "help_text": "Оставьте пустым, чтобы не менять",
            },
            {
                "display_name": "Кол-во",
                "name": "quantity",
                "type": "text",
                "subtype": "number",
                "optional": True,
                "help_text": "Оставьте пустым, чтобы не менять",
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


def _validate_link(raw):
    """Возвращает (значение_или_None, ошибка_или_None). Пустое -> (None, None)."""
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw, None
    return (
        None,
        "Ссылка должна начинаться с http:// или https:// (или оставьте пустой).",
    )


def _validate_quantity(raw, required):
    """Возвращает (число_или_None, ошибка_или_None)."""
    raw = str(raw if raw is not None else "").strip()
    if not raw:
        if required:
            return None, "Введите число."
        return None, None
    try:
        qty = int(raw)
    except ValueError:
        return None, "Введите целое число."
    if qty < 1:
        return None, "Число должно быть 1 или больше."
    return qty, None


async def _submit_add_gift(user_id, submission):
    name = (submission.get("gift_name") or "").strip()
    errors = {}
    if not name:
        errors["gift_name"] = "Укажите название желания."
    link, link_err = _validate_link(submission.get("gift_link"))
    if link_err:
        errors["gift_link"] = link_err
    qty, qty_err = _validate_quantity(submission.get("quantity"), required=True)
    if qty_err:
        errors["quantity"] = qty_err
    if errors:
        return errors, None

    async with common.transaction():
        await common.db.execute(
            "INSERT INTO gifts (gift_name, gift_link, quantity_want, user_id) "
            "VALUES (?, ?, ?, ?)",
            (name, link, qty, user_id),
        )
    return None, f"Вы добавили желание «{escape_md(name)}» в свой wish-list."


async def _submit_delete_gift(user_id, submission):
    try:
        gift_id = int(submission.get("gift_id"))
    except (TypeError, ValueError):
        return {"gift_id": "Выберите желание."}, None

    async with common.transaction():
        cur = await common.db.execute(
            "SELECT gift_name FROM gifts WHERE gift_id = ? AND user_id = ?",
            (gift_id, user_id),
        )
        gift = await cur.fetchone()
        if gift is None:
            return None, "Это желание уже не найдено."
        cur_d = await common.db.execute(
            "SELECT 1 FROM gift_donors WHERE gift_id = ? LIMIT 1", (gift_id,)
        )
        if await cur_d.fetchone():
            return None, "На это желание уже есть даритель — удалить нельзя."
        await common.db.execute("DELETE FROM gifts WHERE gift_id = ?", (gift_id,))
    return (
        None,
        f"Вы удалили желание «{escape_md(gift['gift_name'])}» из своего wish-list.",
    )


async def _submit_edit_gift(user_id, submission):
    try:
        gift_id = int(submission.get("gift_id"))
    except (TypeError, ValueError):
        return {"gift_id": "Выберите желание."}, None

    new_name = (submission.get("gift_name") or "").strip()
    errors = {}
    link_raw = (submission.get("gift_link") or "").strip()
    new_link = None
    if link_raw:
        new_link, link_err = _validate_link(link_raw)
        if link_err:
            errors["gift_link"] = link_err
    new_qty, qty_err = _validate_quantity(submission.get("quantity"), required=False)
    if qty_err:
        errors["quantity"] = qty_err
    if errors:
        return errors, None

    async with common.transaction():
        cur = await common.db.execute(
            "SELECT * FROM gifts WHERE gift_id = ? AND user_id = ?", (gift_id, user_id)
        )
        gift = await cur.fetchone()
        if gift is None:
            return None, "Это желание уже не найдено."
        cur_d = await common.db.execute(
            "SELECT 1 FROM gift_donors WHERE gift_id = ? LIMIT 1", (gift_id,)
        )
        if await cur_d.fetchone():
            return None, "На это желание уже есть даритель — редактировать нельзя."

        final_name = new_name or gift["gift_name"]
        final_link = new_link if link_raw else gift["gift_link"]
        final_qty = new_qty if new_qty is not None else gift["quantity_want"]
        await common.db.execute(
            "UPDATE gifts SET gift_name = ?, gift_link = ?, quantity_want = ? WHERE gift_id = ?",
            (final_name, final_link, final_qty, gift_id),
        )
    return None, f"Вы обновили желание «{escape_md(final_name)}»."


# =========================================================================
#  БЛОК «ЖЕЛАНИЯ ДРУГИХ»
# =========================================================================

# ---- Wish-list друга -----------------------------------------------------


async def _friend_wishlist_text(friend_id, friend_disp):
    """Текст списка желаний друга."""
    if _render_friend_wishlist is not None:
        return await _render_friend_wishlist(friend_id)

    cur = await common.db.execute(
        """
        SELECT g.gift_id, g.gift_name, g.gift_link, g.quantity_want,
               COUNT(gd.user_id) AS donor_count
        FROM gifts g
        LEFT JOIN gift_donors gd ON gd.gift_id = g.gift_id
        WHERE g.user_id = ?
        GROUP BY g.gift_id
        ORDER BY g.gift_id
        """,
        (friend_id,),
    )
    rows = await cur.fetchall()
    if not rows:
        return f"У пользователя {friend_disp} пока нет желаний."

    lines = [f"Список желаний — {friend_disp}:", ""]
    for i, g in enumerate(rows, 1):
        name = escape_md(g["gift_name"])
        title = f"[{name}]({g['gift_link']})" if g["gift_link"] else name
        want = g["quantity_want"] or 1
        have = g["donor_count"] or 0
        status = "свободно" if want - have > 0 else "уже дарят"
        lines.append(f"{i}. {title} — дарителей {have}/{want} ({status})")
    return "\n".join(lines)


async def _open_friend_wishlist_dialog(data):
    """Кнопка «Wish-list друга»: модалка с выбором пользователя (поиск по всем)."""
    dialog = {
        "callback_id": "friend_wishlist",
        "title": "Wish-list друга",
        "submit_label": "Показать",
        "elements": [
            {
                "display_name": "Пользователь",
                "name": "friend_id",
                "type": "select",
                "data_source": "users",
                "placeholder": "Начните вводить имя…",
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


async def _submit_friend_wishlist(user_id, submission):
    friend_id = submission.get("friend_id")
    if not friend_id:
        return {"friend_id": "Выберите пользователя."}, None
    friend_disp = await _display_name(friend_id)
    text = await _friend_wishlist_text(friend_id, friend_disp)
    return None, text


# ---- Отказаться от подарка (модалка) ------------------------------------


async def _my_donations(user_id):
    """Подарки, которые пользователь взялся дарить (он — даритель)."""
    cur = await common.db.execute(
        """
        SELECT g.gift_id, g.gift_name, g.user_id AS wisher_id
        FROM gifts g
        JOIN gift_donors gd ON gd.gift_id = g.gift_id
        WHERE gd.user_id = ?
        ORDER BY g.gift_id
        """,
        (user_id,),
    )
    return await cur.fetchall()


async def _open_cancel_gift_dialog(data):
    """Кнопка «Отказаться от подарка»: модалка с выбором забронированного подарка."""
    user_id = data.get("user_id")
    rows = await _my_donations(user_id)
    if not rows:
        await _show_section(data.get("post_id"), "Вы пока не дарите ни один подарок.")
        return
    options = []
    for r in rows:
        nm = await _plain_name_by_id(r["wisher_id"])
        options.append({"text": f"{nm} — «{r['gift_name']}»", "value": str(r["gift_id"])})
    dialog = {
        "callback_id": "cancel_gift",
        "title": "Отказ",
        "submit_label": "Отказаться",
        "elements": [
            {
                "display_name": "Подарок",
                "name": "gift_id",
                "type": "select",
                "options": options,
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


async def _submit_cancel_gift(user_id, submission):
    try:
        gift_id = int(submission.get("gift_id"))
    except (TypeError, ValueError):
        return {"gift_id": "Выберите подарок."}, None

    gift_name = None
    wisher_id = None
    already = False
    async with common.transaction():
        cur = await common.db.execute(
            "SELECT g.gift_name, g.user_id AS wisher_id "
            "FROM gifts g JOIN gift_donors gd ON gd.gift_id = g.gift_id "
            "WHERE g.gift_id = ? AND gd.user_id = ?",
            (gift_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            already = True
        else:
            gift_name = row["gift_name"]
            wisher_id = row["wisher_id"]
            await common.db.execute(
                "DELETE FROM gift_donors WHERE gift_id = ? AND user_id = ?",
                (gift_id, user_id),
            )
    if already:
        return None, "Похоже, вы уже отказались от этого подарка."
    name = await _display_name(wisher_id)
    return None, f"Вы отказались от дарения подарка «{escape_md(gift_name)}» пользователю {name}."


# ---- Выбрать подарок (multi-step модалка) --------------------------------


async def _giftable_recipients(me_id):
    """user_id людей, которым me_id ещё может выбрать подарок."""
    cur = await common.db.execute(
        """
        SELECT DISTINCT g.user_id AS wisher_id
        FROM gifts g
        WHERE g.user_id != ?
          AND (SELECT COUNT(*) FROM gift_donors gd
               WHERE gd.gift_id = g.gift_id) < g.quantity_want
          AND g.user_id NOT IN (
                SELECT g2.user_id FROM gifts g2
                JOIN gift_donors gd2 ON gd2.gift_id = g2.gift_id
                WHERE gd2.user_id = ?
          )
        ORDER BY g.user_id
        """,
        (me_id, me_id),
    )
    return await cur.fetchall()


async def _recipient_options(me_id):
    options = []
    for r in await _giftable_recipients(me_id):
        wid = r["wisher_id"]
        try:
            u = await run_in_thread(driver.users.get_user, wid)
        except Exception:
            continue
        if not u or u.get("is_bot"):
            continue
        options.append({"text": _plain_name(u), "value": wid})
    return options


async def _claimable_gifts(me_id, friend_id):
    cur = await common.db.execute(
        """
        SELECT g.gift_id, g.gift_name, g.quantity_want,
               COUNT(gd.user_id) AS donor_count,
               COALESCE(SUM(CASE WHEN gd.user_id = ? THEN 1 ELSE 0 END), 0) AS i_donate
        FROM gifts g
        LEFT JOIN gift_donors gd ON gd.gift_id = g.gift_id
        WHERE g.user_id = ?
        GROUP BY g.gift_id
        HAVING donor_count < g.quantity_want AND i_donate = 0
        ORDER BY g.gift_id
        """,
        (me_id, friend_id),
    )
    return await cur.fetchall()


async def _already_gifting(me_id, friend_id):
    cur = await common.db.execute(
        "SELECT 1 FROM gifts g JOIN gift_donors gd ON gd.gift_id = g.gift_id "
        "WHERE g.user_id = ? AND gd.user_id = ? LIMIT 1",
        (friend_id, me_id),
    )
    return (await cur.fetchone()) is not None


async def _open_choose_gift_for(data):
    """Кнопка «Выбрать подарок» в ДР-канале: сразу показываем кнопки-подарки
    для именинника (wisher_id из контекста), без шага выбора получателя."""
    me_id = data.get("user_id")
    channel_id = data.get("channel_id")
    friend_id = (data.get("context") or {}).get("wisher_id")

    if not friend_id:
        return "Не удалось определить именинника."
    # Кнопка из ДР-канала (вне модели самообновления) -> select уходит в личку бота
    # (выбор подарка приватен; о новом ЛС скажет бейдж непрочитанного).
    await _send_gift_picker(me_id, friend_id)


async def _start_choose_gift(data):
    """Кнопка «Выбрать подарок»: открывает шаг 1 (модалка выбора получателя)."""
    me_id = data.get("user_id")
    post_id = data.get("post_id")
    trigger_id = data.get("trigger_id")

    options = await _recipient_options(me_id)
    if not options:
        await _show_section(
            post_id,
            "Сейчас некому выбрать подарок: либо ни у кого нет свободных желаний, "
            "либо вы уже выбрали подарки всем доступным.",
        )
        return

    dialog = {
        "callback_id": "choose_gift",
        "title": "Подарок",
        "submit_label": "Далее",
        # post_id сообщения-меню -> после выбора получателя перепишем этот же пост
        # на экран выбора подарка.
        "state": post_id or "",
        "elements": [
            {
                "display_name": "Кому дарим",
                "name": "friend_id",
                "type": "select",
                "options": options,
            },
        ],
    }
    await _open_dialog(trigger_id, dialog, post_id)


async def _send_gift_picker(me_id, friend_id, post_id=None):
    """Показывает выпадающий список (select) подарков получателя.

    Если задан post_id (личное меню) — переписываем этот пост на экран выбора
    (розовая полоса + select + «Вернуться на Главную»). Если post_id нет (кнопка
    из ДР-канала) — шлём новый пост в личку бота. При выборе Mattermost шлёт POST
    на /button, выбранное option['value'] приходит в context.selected_option;
    контекст несёт action 'choose_gift_do' и friend_id.
    """
    async def _fail(text):
        if post_id:
            await _show_section(post_id, text)
        else:
            await _send_ephemeral(me_id, None, text)

    if friend_id == me_id:
        await _fail("Нельзя выбрать подарок самому себе.")
        return
    if await _already_gifting(me_id, friend_id):
        await _fail("Вы уже выбрали подарок для этого пользователя.")
        return
    gifts = await _claimable_gifts(me_id, friend_id)
    if not gifts:
        await _fail(
            "У этого пользователя не осталось доступных подарков "
            "(список пуст или всё уже разобрали)."
        )
        return

    friend_disp = await _display_name(friend_id)

    options = []
    for g in gifts:
        free = (g["quantity_want"] or 1) - (g["donor_count"] or 0)
        name = g["gift_name"]
        short = (name[:60] + "…") if len(name) > 60 else name
        options.append(
            {"text": f"«{short}» (свободно {free})", "value": str(g["gift_id"])}
        )

    attachment = {
        "color": SECTION_COLOR,
        "text": f"Выберите подарок для {friend_disp}:",
        "actions": [
            _select_action(
                "choose_gift_do",
                "Выберите подарок",
                options,
                {"friend_id": friend_id},
            ),
            _back_button(),
        ],
    }

    if post_id:
        await _patch_view(post_id, "", [attachment])
    else:
        await _dm_post(me_id, "", [attachment])


async def _do_choose_gift(data):
    """Выбор подарка из select-меню эфемерки: записываем дарителя.

    Значение прилетает в context.selected_option (option['value'] = gift_id);
    friend_id также в context, но получатель всё равно берётся из самого подарка.
    Сама запись — через common.claim_gift (общая логика с текстовым сценарием).
    """
    me_id = data.get("user_id")
    post_id = data.get("post_id")
    ctx = data.get("context") or {}
    # selected_option — для select; gift_id — на случай кнопочного контекста.
    raw_gift = ctx.get("selected_option")
    if raw_gift is None:
        raw_gift = ctx.get("gift_id")
    try:
        gift_id = int(raw_gift)
    except (TypeError, ValueError):
        await _show_section(post_id, "Не удалось определить подарок.")
        return

    problem, gift_name, wisher_id = await claim_gift(me_id, gift_id)
    if problem:
        await _show_section(post_id, problem)
        return

    username = await _username(wisher_id)
    bday = await _birthday_line(wisher_id)
    msg = f"Вы выбрали подарок «{escape_md(gift_name)}» пользователю @{username}."
    if bday:
        msg += f" День рождения — {bday}."
    await _show_section(post_id, msg)


async def _handle_choose_gift_pick_user(user_id, post_id, submission):
    """Шаг 1 «Выбрать подарок»: выбор получателя -> тот же пост переписываем на
    экран выбора подарка (select). Модалка при этом закрывается (пустой ответ).
    """
    friend_id = submission.get("friend_id")
    if not friend_id:
        return web.json_response({"errors": {"friend_id": "Выберите пользователя."}})
    if friend_id == user_id:
        return web.json_response({"errors": {"friend_id": "Нельзя дарить самому себе."}})

    await _send_gift_picker(user_id, friend_id, post_id=post_id)
    return web.json_response({})


# =========================================================================
#  МОЙ АККАУНТ — «Моё послание»
# =========================================================================


async def _open_letter_dialog(data):
    user_id = data.get("user_id")
    cur = await common.db.execute(
        "SELECT letter_to_others FROM users WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    current = row["letter_to_others"] if row and row["letter_to_others"] else ""
    dialog = {
        "callback_id": "my_letter",
        "title": "Моё послание",
        "introduction_text": LETTER_INTRO,
        "submit_label": "Сохранить",
        "elements": [
            {
                "display_name": "Послание",
                "name": "letter",
                "type": "textarea",
                "optional": True,
                "default": current,
                "max_length": 3000,
                "help_text": "Оставьте пустым, чтобы удалить послание",
            },
        ],
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


async def _submit_letter(user_id, submission):
    letter = (submission.get("letter") or "").strip()
    async with common.transaction():
        await common.db.execute(
            "UPDATE users SET letter_to_others = ? WHERE user_id = ?",
            (letter or None, user_id),
        )
    if not letter:
        return None, "Послание удалено."
    await _notify_letter_added(user_id)
    return None, "Послание сохранено. Коллеги увидят его в вашей ДР-группе."


async def _letter_text(user_id):
    cur = await common.db.execute(
        "SELECT letter_to_others FROM users WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    if not row or not row["letter_to_others"]:
        return "Именинник пока не оставил послания."
    name = await _display_name(user_id)
    return f"Послание от {name}:\n\n{row['letter_to_others']}"


async def _notify_letter_added(user_id):
    """Если временный ДР-канал уже создан — постим туда уведомление + кнопку послания."""
    try:
        cur = await common.db.execute(
            "SELECT year, channel_id FROM birthday_notices "
            "WHERE user_id = ? AND notice_type = 'week_before' "
            "AND channel_id IS NOT NULL ORDER BY year DESC",
            (user_id,),
        )
        rows = await cur.fetchall()
        for r in rows:
            cur2 = await common.db.execute(
                "SELECT 1 FROM birthday_notices WHERE user_id = ? AND year = ? "
                "AND notice_type = 'channel_deleted'",
                (user_id, r["year"]),
            )
            if await cur2.fetchone():
                continue  # канал уже удалён
            username = await _username(user_id)
            attachment = {
                "text": f"✉️ @{username} добавил(а) послание для коллег.",
                "actions": [
                    _action_ctx("bday_letter", "Послание от именинника", {"wisher_id": user_id})
                ],
            }
            await _create_post(r["channel_id"], "", [attachment])
            return  # уведомляем только ближайший живой канал
    except Exception as e:
        log.warning("Не удалось уведомить о добавлении послания (%s): %s", user_id, e)


# =========================================================================
#  МОЙ АККАУНТ — «Удалить меня»
# =========================================================================


async def _open_delete_me_dialog(data):
    """Кнопка «Удалить меня»: модалка-подтверждение (Cancel + «Удалить»)."""
    dialog = {
        "callback_id": "delete_me",
        "title": "Удаление",
        "introduction_text": DELETE_ME_CONFIRM_TEXT,
        "submit_label": "Удалить",
        "elements": [],  # подтверждение без полей
    }
    await _open_dialog(data.get("trigger_id"), dialog, data.get("post_id"))


# =========================================================================
#  КНОПКА «АДМИН» (модалка в 2 шага)
# =========================================================================


async def _is_admin_here(user_id, channel_id):
    try:
        ch = await run_in_thread(driver.channels.get_channel, channel_id)
        team_id = ch.get("team_id", "")
    except Exception:
        team_id = ""
    if await run_in_thread(is_team_admin, user_id, team_id):
        return True
    return await run_in_thread(is_channel_admin, user_id, channel_id)


async def _member_options(member_ids, exclude_id=None):
    options = []
    for uid in member_ids:
        if uid == exclude_id:
            continue
        try:
            u = await run_in_thread(driver.users.get_user, uid)
        except Exception:
            continue
        if not u or u.get("is_bot"):
            continue
        options.append({"text": _plain_name(u), "value": uid})
    options.sort(key=lambda o: o["text"].lower())
    return options


async def _delete_own_post(post_id):
    """Удаляет собственный пост бота (не требует прав админа). Ошибки логируем."""
    if not post_id:
        return
    try:
        await run_in_thread(driver.posts.delete_post, post_id)
    except Exception as e:
        log.warning("Не удалось удалить пост %s: %s", post_id, e)


async def _start_admin(data):
    """Кнопка «Админ» (в канале): постим сообщение с select-выбором участника.

    Это обычный пост бота (эфемерку с select без админа не показать), его удалим
    в конце сценария. Возврат строки -> нативная эфемерка (ephemeral_text работает
    в обычных каналах); None -> пустой ответ.
    """
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")

    if not await _is_admin_here(user_id, channel_id):
        return ("У вас нет прав менять даты рождения. "
                "Это может сделать администратор канала или команды.")

    members = await run_in_thread(get_channel_member_ids, channel_id)
    options = await _member_options(members, exclude_id=driver.client.userid)
    if not options:
        return "В этом канале нет участников, которым можно задать дату рождения."

    attachment = {
        "color": HOME_COLOR,
        "text": "Кому задать дату рождения?",
        "actions": [
            _select_action("admin_pick_user", "Выберите участника", options),
        ],
    }
    await run_in_thread(
        driver.posts.create_post,
        options={
            "channel_id": channel_id,
            "message": "",
            "props": {"attachments": [attachment]},
        },
    )
    return None


async def _admin_pick_user(data):
    """Выбор участника в select (в канале): открываем модалку ввода даты.

    В state модалки кладём '<select_post_id>|<target_id>' — после submit по нему
    найдём пост-select (перепишем на подтверждение) и целевого пользователя.
    """
    admin_id = data.get("user_id")
    channel_id = data.get("channel_id")
    post_id = data.get("post_id")
    trigger_id = data.get("trigger_id")
    target_id = (data.get("context") or {}).get("selected_option")

    if not target_id:
        return None
    if not await _is_admin_here(admin_id, channel_id):
        return "У вас нет прав менять даты рождения."

    dialog = {
        "callback_id": "admin_birth",
        "title": "ДР участника",
        "submit_label": "Далее",
        "state": f"{post_id or ''}|{target_id}",
        "elements": [
            {
                "display_name": "Дата",
                "name": "birth",
                "type": "text",
                "placeholder": "ДД.ММ или ДД.ММ.ГГГГ",
                "help_text": "Например: 15.07 или 15.07.1990",
            },
        ],
    }
    await _open_dialog(trigger_id, dialog)
    return None


async def _handle_admin_birth_dialog(admin_id, channel_id, submission, state):
    """Submit модалки даты: переписываем пост-select на экран подтверждения с
    кнопками «Подтвердить»/«Отмена». target_id и дату несём в контексте кнопок —
    server-side pending не нужен.
    """
    raw = (submission.get("birth") or "").strip()
    birth = parse_user_birth(raw)
    if birth is None:
        return web.json_response({"errors": {"birth":
            "Некорректная дата. Введите ДД.ММ или ДД.ММ.ГГГГ "
            "(например 15.07 или 15.07.1990)."}})

    select_post_id, _, target_id = (state or "").partition("|")
    if not target_id:
        return web.json_response({})
    if not await _is_admin_here(admin_id, channel_id):
        # Эфемерку после submit без админа не показать — просто убираем select-пост.
        await _delete_own_post(select_post_id)
        return web.json_response({})

    cur = await common.db.execute(
        "SELECT user_birth FROM users WHERE user_id = ?", (target_id,)
    )
    existing = await cur.fetchone()
    target_username = await _username(target_id)
    new_disp = format_birth(str(birth))

    if existing is not None:
        text = (
            f"У пользователя @{target_username} уже указано ДР "
            f"{format_birth(existing['user_birth'])}, хотите поменять на {new_disp}?"
        )
    else:
        text = (
            f"Пользователь @{target_username} не зарегистрирован в системе ДР-бота, "
            f"хотите добавить его дату рождения {new_disp}?"
        )

    confirm_view = [{
        "color": HOME_COLOR,
        "text": text,
        "actions": [
            _action_ctx("admin_birth_confirm", "Подтвердить",
                        {"target_id": target_id, "birth": str(birth)}),
            _action_ctx("admin_birth_cancel", "Отмена"),
        ],
    }]
    await _patch_view(select_post_id, "", confirm_view)
    return web.json_response({})


async def _confirm_admin_birth(data):
    """Кнопка «Подтвердить» (в канале): применяем изменение и переписываем пост
    подтверждения в «Готово…» с кнопкой «Скрыть». target_id/дата — из контекста
    кнопки. Ошибки уходят админу в ЛС (возврат строки), пост при этом удаляем.
    """
    admin_id = data.get("user_id")
    channel_id = data.get("channel_id")
    post_id = data.get("post_id")
    ctx = data.get("context") or {}
    target_id = ctx.get("target_id")
    birth_str = ctx.get("birth")

    if not target_id or not birth_str:
        await _delete_own_post(post_id)
        return "Не удалось определить данные. Откройте «Админ» заново."
    if not await _is_admin_here(admin_id, channel_id):
        await _delete_own_post(post_id)
        return "У вас нет прав менять даты рождения."
    if _apply_admin_birth is None:
        log.error("apply_admin_birth не подключён в setup()")
        await _delete_own_post(post_id)
        return "Изменение сейчас недоступно."

    error, normalized, is_update = await _apply_admin_birth(target_id, birth_str)
    if error:
        await _delete_own_post(post_id)
        return error

    target_username = await _username(target_id)
    admin_username = await _username(admin_id)
    verb = "поменял" if is_update else "внёс"

    await dm_user(
        target_id,
        f"Администратор @{admin_username} {verb} вашу дату рождения на {normalized}. "
        f"В случае ошибки обратитесь к нему.",
    )
    await _patch_view(post_id, "", [{
        "color": HOME_COLOR,
        "text": f"Готово. Дата рождения @{target_username} — {normalized}.",
        "actions": [_action("dismiss", "Скрыть")],
    }])


async def _cancel_admin_birth(data):
    """Кнопка «Отмена»: просто удаляем пост подтверждения (исчезновение = ответ)."""
    await _delete_own_post(data.get("post_id"))


async def _dismiss_post(data):
    """Кнопка «Скрыть» на канальных ответах бота: удаляем собственный пост."""
    await _delete_own_post(data.get("post_id"))


# =========================================================================
#  HTTP-ЭНДПОИНТЫ
# =========================================================================

# действия, открывающие модалку
_DIALOG_OPENERS = {
    "register": _open_register_dialog,
    "add_gift": _open_add_gift_dialog,
    "delete_gift": _open_delete_gift_dialog,
    "edit_gift": _open_edit_gift_dialog,
    "friend_wishlist": _open_friend_wishlist_dialog,
    "cancel_gift": _open_cancel_gift_dialog,
    "my_letter": _open_letter_dialog,
    "delete_me": _open_delete_me_dialog,
}

# действия со своим сценарием (сами патчат пост / постят / шлют ЛС / открывают модалку).
# Могут вернуть строку -> она уйдёт нативной эфемеркой (только для канальных
# сценариев: admin_*; в ЛС ephemeral_text не рендерится, там хендлеры патчат пост
# и возвращают None).
_CUSTOM_ACTIONS = {
    "choose_gift": _start_choose_gift,  # -> открывает шаг 1 (модалка выбора получателя)
    "bday_choose_gift": _open_choose_gift_for,  # -> сразу кнопки-подарки имениннику
    "choose_gift_do": _do_choose_gift,  # кнопка конкретного подарка (из эфемерки)
    "register_confirm": _confirm_registration,  # кнопка «Да, всё верно» (регистрация)
    "register_cancel": _cancel_registration,    # кнопка «Отмена» (регистрация)
    "admin": _start_admin,  # «Админ» (канал) -> пост с select участника
    "admin_pick_user": _admin_pick_user,  # выбор участника в select -> модалка даты
    "admin_birth_confirm": _confirm_admin_birth,  # «Подтвердить» -> патч в «Готово»
    "admin_birth_cancel": _cancel_admin_birth,    # «Отмена» -> удаление поста
    "dismiss": _dismiss_post,  # «Скрыть» на канальных ответах -> удаление поста
}

# обработчики submit по callback_id (register/choose_gift/admin_birth/delete_me — отдельно)
_DIALOG_SUBMITS = {
    "add_gift": _submit_add_gift,
    "delete_gift": _submit_delete_gift,
    "edit_gift": _submit_edit_gift,
    "friend_wishlist": _submit_friend_wishlist,
    "cancel_gift": _submit_cancel_gift,
    "my_letter": _submit_letter,
}


async def _handle_button(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    action = (data.get("context") or {}).get("action")
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")
    log.info(
        "Нажата кнопка: action=%s, post_id=%s, trigger_id=%s",
        action,
        data.get("post_id"),
        data.get("trigger_id"),
    )

    post_id = data.get("post_id")

    try:
        # --- Личное меню: самообновляющееся сообщение (правим тот же пост) ---
        if action == "home":
            await _show_home(post_id)
            return web.json_response({})

        # Открыватели модалок сами патчат пост / шлют ЛС / открывают модалку.
        if action in _DIALOG_OPENERS:
            await _DIALOG_OPENERS[action](data)
            return web.json_response({})

        # Кастомные действия: в ЛС патчат пост и возвращают None; канальные (admin_*,
        # bday_*) могут вернуть строку -> приватное уведомление в ЛС (эфемерка в
        # ленте канала без прав админа невозможна; результаты флоу показываются
        # постами/патчами внутри самих хендлеров).
        if action in _CUSTOM_ACTIONS:
            result = await _CUSTOM_ACTIONS[action](data)
            if result:
                await _send_ephemeral(user_id, channel_id, result)
            return web.json_response({})

        # Личные кнопки-«читалки» -> результат в этом же посте (розовый раздел).
        if action == "wishlist":
            await _show_section(post_id, await _render_wishlist(user_id))
            return web.json_response({})
        if action == "mygifts":
            await _show_section(post_id, await _render_mygifts(user_id))
            return web.json_response({})

        # --- Канальные кнопки-«читалки»: пост в основной ленте + кнопка «Скрыть» ---
        if action in ("soon", "all"):
            member_ids = await run_in_thread(get_channel_member_ids, channel_id)
            text = await (_render_soon if action == "soon" else _render_all)(member_ids)
        elif action == "bday_wishlist":
            wisher_id = (data.get("context") or {}).get("wisher_id")
            if wisher_id:
                text = await _friend_wishlist_text(
                    wisher_id, await _display_name(wisher_id)
                )
            else:
                text = "Не удалось определить именинника."
        elif action == "bday_letter":
            wisher_id = (data.get("context") or {}).get("wisher_id")
            text = await _letter_text(wisher_id) if wisher_id else "Не удалось определить именинника."
        else:
            text = "Неизвестная кнопка."

        await _post_dismissible(channel_id, text)
    except Exception as e:
        log.exception("Ошибка обработки нажатия кнопки (action=%s): %s", action, e)

    return web.json_response({})


async def _handle_dialog(request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    callback_id = data.get("callback_id")
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")
    submission = data.get("submission") or {}
    # В state личных диалогов лежит post_id сообщения-меню (кладём при открытии) —
    # по нему после submit переписываем тот же пост. Для админского диалога state
    # не post_id ("pick"), но админ-ветка его и не использует.
    post_id = data.get("state") or None

    # Отмена модалки — ничего не делаем (сообщение остаётся как было).
    if data.get("cancelled"):
        return web.json_response({})

    # Регистрация: шаг ввода даты -> экран подтверждения в том же посте
    if callback_id == "register":
        try:
            return await _handle_register_dialog(user_id, post_id, submission)
        except Exception as e:
            log.exception("Ошибка диалога register: %s", e)
            return web.json_response({})

    # Админ (канальный сценарий): пост-select -> экран подтверждения.
    # state = '<select_post_id>|<target_id>' (кладём при открытии модалки).
    if callback_id == "admin_birth":
        try:
            return await _handle_admin_birth_dialog(
                user_id, channel_id, submission, data.get("state")
            )
        except Exception as e:
            log.exception("Ошибка диалога admin_birth: %s", e)
            return web.json_response({})

    # Выбор подарка: шаг выбора получателя -> экран выбора подарка в том же посте
    if callback_id == "choose_gift":
        try:
            return await _handle_choose_gift_pick_user(user_id, post_id, submission)
        except Exception as e:
            log.exception("Ошибка диалога choose_gift: %s", e)
            return web.json_response({})

    # Удаление аккаунта: удалить ДР+wish-list, пост -> экран «удалено» + регистрация.
    # Историю ЛС не чистим: удаление чужих постов требует прав system_admin.
    if callback_id == "delete_me":
        try:
            if _delete_account is not None:
                await _delete_account(user_id)
            await _patch_view(post_id, "", _deleted_attachments())
        except Exception as e:
            log.exception("Ошибка удаления аккаунта: %s", e)
        return web.json_response({})

    handler = _DIALOG_SUBMITS.get(callback_id)
    if handler is None:
        return web.json_response({})

    try:
        errors, confirm = await handler(user_id, submission)
        if errors:
            return web.json_response({"errors": errors})
        if confirm:
            # Результат раздела -> тот же пост (розовая полоса + «Вернуться на Главную»)
            await _show_section(post_id, confirm)
    except Exception as e:
        log.exception("Ошибка обработки диалога (callback=%s): %s", callback_id, e)

    return web.json_response({})


async def _handle_health(request):
    return web.Response(text="ok")


async def start_button_server():
    app = web.Application()
    app.router.add_post("/button", _handle_button)
    app.router.add_post("/dialog", _handle_dialog)
    app.router.add_get("/health", _handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BUTTON_PORT)
    await site.start()
    log.info("HTTP-сервер кнопок слушает на 0.0.0.0:%d", BUTTON_PORT)
    return runner