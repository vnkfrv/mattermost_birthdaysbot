"""
Планировщик: раз в сутки проверяет, у кого из сотрудников скоро день рождения,
создаёт временный канал с коллегами именинника и поздравляет в нужный день.

Помимо расписания, ту же логику можно запустить точечно для одного пользователя
сразу после того, как он добавил/изменил дату рождения — см. run_checks_for_user.
Это нужно, чтобы не ждать 10:00: например, если ДР уже завтра или сегодня, канал
и поздравление должны появиться немедленно.
"""

import asyncio
import calendar
import os
import re
from datetime import datetime, timedelta

import common
from common import (
    driver,
    log,
    MAIN_TEAM_NAME,
    get_channel_members_full,
    _member_is_channel_admin,
    promote_to_channel_admin,
    get_dm_channel_id,
    now_local,
    today_local,
    parse_birth_dt,
    run_in_thread,
    is_feb29,
    celebration_date,
    next_celebration,
)

FEB29_NONLEAP_NOTE = (
    "У пользователя ДР 29.02, однако этот год не високосный. "
    "По традиции, именинника поздравляют 28 февраля или 1 марта."
)


def _days_word(n):
    """Правильная форма слова «день» для числа n (1 день, 2 дня, 5 дней)."""
    if 11 <= n % 100 <= 14:
        return "дней"
    last = n % 10
    if last == 1:
        return "день"
    if 2 <= last <= 4:
        return "дня"
    return "дней"


async def notice_exists(user_id, year, notice_type):
    cursor = await common.db.execute(
        "SELECT 1 FROM birthday_notices WHERE user_id = ? AND year = ? AND notice_type = ?",
        (user_id, year, notice_type),
    )
    return await cursor.fetchone() is not None


async def save_notice(user_id, year, notice_type, channel_id=None):
    async with common.transaction():
        await common.db.execute(
            "INSERT OR IGNORE INTO birthday_notices (user_id, year, notice_type, channel_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, year, notice_type, channel_id, now_local().isoformat()),
        )


async def get_week_before_channel(user_id, year):
    cursor = await common.db.execute(
        "SELECT channel_id FROM birthday_notices WHERE user_id = ? AND year = ? AND notice_type = 'week_before'",
        (user_id, year),
    )
    row = await cursor.fetchone()
    return row["channel_id"] if row else None


def get_main_team():
    """
    Находит основную команду компании (BIRTHDAY_TEAM_NAME, по умолчанию 'magnit')
    среди команд, в которых состоит бот. Сверяем по полю team['name'] — это технический
    slug команды (виден в URL), а не отображаемое имя (display_name).
    Возвращает team dict или None, если бот не состоит в этой команде.
    """
    try:
        teams = driver.teams.get_user_teams(driver.client.userid)
    except Exception as e:
        log.exception("Не удалось получить список команд бота: %s", e)
        return None

    for team in teams:
        if team.get("name", "").lower() == MAIN_TEAM_NAME.lower():
            return team

    log.error(
        "Бот не состоит в основной команде '%s' (или команда с таким именем не найдена)",
        MAIN_TEAM_NAME,
    )
    return None


def get_shared_channel_members(birthday_user_id, team_id):
    """
    Ищет каналы (публичные/приватные, не ЛС и не групповые сообщения) внутри
    основной команды (team_id), в которых состоят одновременно бот и именинник.

    Возвращает кортеж (member_ids, admin_ids):
      • member_ids — объединённое множество user_id всех участников этих каналов;
      • admin_ids  — те из них, кто является администратором хотя бы одного такого
        исходного канала (им во временном ДР-канале выдаются права админа).
    Оба множества — без самого именинника и без бота.
    """
    bot_id = driver.client.userid
    member_ids = set()
    admin_ids = set()

    try:
        bot_channels = driver.channels.get_channels_for_user(bot_id, team_id)
    except Exception as e:
        log.warning("Не удалось получить каналы бота в команде %s: %s", team_id, e)
        return member_ids, admin_ids

    for ch in bot_channels:
        # пропускаем личные (D) и групповые (G) сообщения — нас интересуют
        # только обычные публичные/приватные каналы команды
        if ch.get("type") not in ("O", "P"):
            continue
        # get_channel_members_full постранично обходит участников (по 200 за раз)
        # и сохраняет роли; прямой вызов get_channel_members без пагинации возвращал
        # только первую страницу (~60 человек) и «терял» коллег в больших каналах.
        members = get_channel_members_full(ch["id"])
        channel_member_ids = {m.get("user_id") for m in members if m.get("user_id")}
        if birthday_user_id in channel_member_ids:
            member_ids |= channel_member_ids
            for m in members:
                uid = m.get("user_id")
                if uid and _member_is_channel_admin(m):
                    admin_ids.add(uid)

    member_ids.discard(birthday_user_id)
    member_ids.discard(bot_id)
    admin_ids.discard(birthday_user_id)
    admin_ids.discard(bot_id)
    return member_ids, admin_ids


def grant_channel_admins(channel_id, admin_ids):
    """Выдаёт роль администратора нового ДР-канала перечисленным пользователям.

    Их предварительно уже добавили в канал (add_members_to_channel), поэтому
    роль назначается участникам. Вызывать через run_in_thread.
    """
    for uid in admin_ids:
        promote_to_channel_admin(channel_id, uid)


def create_birthday_channel(team_id, username, fullname, occurrence_year):
    raw_name = f"birthday-{username}-{occurrence_year}".lower()
    channel_name = (
        re.sub(r"[^a-z0-9\-]", "-", raw_name).strip("-")[:64]
        or f"birthday-{occurrence_year}"
    )
    try:
        channel = driver.channels.create_channel(
            {
                "team_id": team_id,
                "name": channel_name,
                "display_name": f"🎉 ДР {fullname} {occurrence_year}"[:64],
                "type": "P",
                "purpose": f"Временный канал для организации поздравления {fullname}",
            }
        )
        return channel["id"]
    except Exception:
        # Канал уже существует — находим его и возвращаем id
        try:
            channel = driver.channels.get_channel_by_name(team_id, channel_name)
            log.info("Канал %s уже существует, использую существующий", channel_name)
            return channel["id"]
        except Exception as e:
            log.exception(
                "Не удалось создать или найти канал для ДР %s: %s", fullname, e
            )
            return None


def add_members_to_channel(channel_id, member_ids):
    for uid in member_ids:
        try:
            driver.channels.add_user(channel_id, {"user_id": uid})
        except Exception as e:
            log.warning(
                "Не удалось добавить пользователя %s в канал %s: %s", uid, channel_id, e
            )


def delete_channel(channel_id):
    """Удаляет канал Mattermost по его ID."""
    try:
        driver.channels.delete_channel(channel_id)
        log.info("Канал %s удалён", channel_id)
        return True
    except Exception as e:
        log.exception("Не удалось удалить канал %s: %s", channel_id, e)
        return False


# ---------------------------------------------------------------------------
# Общие «кирпичики» для одного пользователя.
# Их дёргают и расписание (перебирая всех), и точечная проверка при добавлении ДР.
# Идемпотентность обеспечивают записи в birthday_notices.
# ---------------------------------------------------------------------------


async def _create_group_for_user(user_row, cd, days_until, main_team_id):
    """
    Создаёт временный ДР-канал для одного пользователя и постит анонс с кнопками.
    Возвращает channel_id (или None). Сам ставит notice 'week_before'.
    Вызывается для days_until в диапазоне 0..7 (0 — если ДР сегодня).
    Ничего не делает, если notice 'week_before' уже стоит.
    """
    # Кнопки строим здесь, чтобы избежать циклического импорта на уровне модуля.
    from buttons import birthday_channel_actions

    user_id = user_row["user_id"]
    occurrence_year = cd.year

    if await notice_exists(user_id, occurrence_year, "week_before"):
        return await get_week_before_channel(user_id, occurrence_year)

    try:
        mm_user = await run_in_thread(driver.users.get_user, user_id)
        username = mm_user.get("username", user_id)
    except Exception as e:
        log.warning("Не удалось получить пользователя %s: %s", user_id, e)
        return None

    member_ids, admin_ids = await run_in_thread(
        get_shared_channel_members, user_id, main_team_id
    )
    if not member_ids:
        log.info(
            "Для @%s не нашлось общих каналов с коллегами в команде '%s' — временный канал не создаю",
            username,
            MAIN_TEAM_NAME,
        )
        await save_notice(user_id, occurrence_year, "week_before", channel_id=None)
        return None

    channel_id = await run_in_thread(
        create_birthday_channel,
        main_team_id,
        username,
        user_row["user_fullname"],
        occurrence_year,
    )
    if channel_id is None:
        return None

    await run_in_thread(add_members_to_channel, channel_id, member_ids)

    # Админам исходных каналов выдаём права администратора и в этом ДР-канале.
    if admin_ids:
        await run_in_thread(grant_channel_admins, channel_id, admin_ids)

    birth_dt = parse_birth_dt(user_row["user_birth"])

    cursor2 = await common.db.execute(
        "SELECT 1 FROM gifts WHERE user_id = ? LIMIT 1", (user_id,)
    )
    has_wishlist = await cursor2.fetchone() is not None

    cursor3 = await common.db.execute(
        "SELECT letter_to_others FROM users WHERE user_id = ?", (user_id,)
    )
    lr = await cursor3.fetchone()
    has_letter = bool(lr and lr["letter_to_others"])

    if days_until == 0:
        # ДР сегодня — канал создаётся в последний момент (пользователь добавил
        # дату прямо в свой день рождения).
        text = f"🎉🎂 Сегодня день рождения у @{username}! 🥳 Заряжайте конфетти и готовьте поздравления!"
    else:
        when = (
            "завтра"
            if days_until == 1
            else f"через {days_until} {_days_word(days_until)}"
        )
        if birth_dt and is_feb29(birth_dt) and not calendar.isleap(occurrence_year):
            text = f"🎂 У @{username} день рождения {when}!\n\n⚠️ {FEB29_NONLEAP_NOTE}"
        else:
            # Полная дата празднования: «13 июля» (cd — дата с учётом переноса 29.02).
            date_ru = f"{cd.day} {common.MONTHS_GENITIVE[cd.month]}"
            text = f"🎂 У @{username} день рождения {when} - {date_ru}!"

    actions = birthday_channel_actions(user_id, has_wishlist, has_letter)
    options = {"channel_id": channel_id, "message": text}
    if actions:
        options["props"] = {"attachments": [{"actions": actions}]}
    await run_in_thread(driver.posts.create_post, options=options)

    await save_notice(user_id, occurrence_year, "week_before", channel_id=channel_id)
    log.info("Создан временный канал для подготовки ДР @%s: %s", username, channel_id)
    return channel_id


async def _congratulate_user(user_row, today):
    """
    Поздравление в день ДР для одного пользователя: пост в групповой канал (если есть)
    и ЛС имениннику. Ставит notice 'day_of'. Идемпотентно.
    """
    birth_dt = parse_birth_dt(user_row["user_birth"])
    if birth_dt is None or celebration_date(today.year, birth_dt) != today:
        return

    user_id = user_row["user_id"]
    occurrence_year = today.year

    if await notice_exists(user_id, occurrence_year, "day_of"):
        return

    try:
        mm_user = await run_in_thread(driver.users.get_user, user_id)
        username = mm_user.get("username", user_id)
        first_name = mm_user.get("first_name", "")
    except Exception as e:
        log.warning("Не удалось получить пользователя %s: %s", user_id, e)
        return

    group_channel_id = await get_week_before_channel(user_id, occurrence_year)
    if group_channel_id:
        await run_in_thread(
            driver.posts.create_post,
            options={
                "channel_id": group_channel_id,
                "message": f"🎉🎂 Сегодня день рождения у @{username}! Не забудьте поздравить!",
            },
        )
    else:
        log.info(
            "Для @%s нет временного канала за эту дату рождения — пропускаю групповое напоминание",
            username,
        )

    dm_channel_id = await run_in_thread(get_dm_channel_id, user_id)
    if dm_channel_id:
        greeting = (
            f"С днём рождения{', ' + first_name if first_name else ''}! 🎉🎂🎁 "
            f"Желаем всего самого лучшего!"
        )
        await run_in_thread(
            driver.posts.create_post,
            options={"channel_id": dm_channel_id, "message": greeting},
        )

    await save_notice(user_id, occurrence_year, "day_of", channel_id=group_channel_id)
    log.info("Поздравил @%s с днём рождения", username)


async def run_checks_for_user(user_id):
    """
    Точечная проверка ДР для одного пользователя — вызывается сразу после того,
    как он добавил/изменил дату рождения (регистрация, админ, изменение).

    Покрывает случаи, когда до следующего запуска планировщика (10:00) ждать нельзя:
      • ДР сегодня  -> создаём канал и сразу поздравляем;
      • ДР через 1..7 дней -> создаём временный канал.
    Всё идемпотентно через birthday_notices, поэтому повторный прогон по расписанию
    ничего не задублирует.
    """
    try:
        cursor = await common.db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        user_row = await cursor.fetchone()
        if user_row is None:
            return

        birth_dt = parse_birth_dt(user_row["user_birth"])
        if birth_dt is None:
            return

        today = today_local()
        cd = next_celebration(today, birth_dt)
        days_until = (cd - today).days

        main_team = await run_in_thread(get_main_team)
        if main_team is None:
            log.error(
                "Не удалось определить основную команду — пропускаю точечную проверку ДР"
            )
            return
        main_team_id = main_team["id"]

        if days_until == 0:
            # ДР сегодня: создаём канал (если ещё нет) и сразу поздравляем.
            await _create_group_for_user(user_row, cd, 0, main_team_id)
            await _congratulate_user(user_row, today)
        elif 1 <= days_until <= 7:
            await _create_group_for_user(user_row, cd, days_until, main_team_id)
    except Exception:
        log.exception("Ошибка точечной проверки ДР для user_id=%s", user_id)


async def run_two_weeks_before_check():
    """За ~2 недели до ДР: ЛС с предложением обновить wish-list и оставить послание."""
    today = today_local()
    cursor = await common.db.execute("SELECT * FROM users")
    users = await cursor.fetchall()

    for user_row in users:
        try:
            birth_dt = parse_birth_dt(user_row["user_birth"])
            if birth_dt is None:
                continue
            cd = next_celebration(today, birth_dt)
            days_until = (cd - today).days
            # 1..7 закрывает week_before, поэтому здесь окно 8..14
            if not (8 <= days_until <= 14):
                continue

            user_id = user_row["user_id"]
            year = cd.year
            if await notice_exists(user_id, year, "two_weeks_before"):
                continue

            dm = await run_in_thread(get_dm_channel_id, user_id)
            if dm:
                await run_in_thread(
                    driver.posts.create_post,
                    options={
                        "channel_id": dm,
                        "message": (
                            "Привет! До твоего дня рождения около двух недель. "
                            "Самое время обновить список желаний и, если хочешь, "
                            "оставить послание коллегам — позови меня в личных сообщениях."
                        ),
                    },
                )
            await save_notice(user_id, year, "two_weeks_before")
        except Exception:
            log.exception(
                "Ошибка two_weeks_before для user_id=%s", user_row["user_id"]
            )


async def run_week_before_check():
    """ДР в пределах ближайших 1–7 дней: создаёт временный канал с коллегами именинника.

    Срабатывает не только ровно за 7 дней, но и за меньший срок — это нужно,
    чтобы догнать пользователей, добавивших дату рождения незадолго до самого ДР.
    Идемпотентность (notice 'week_before') гарантирует, что канал создаётся один раз.
    """

    today = today_local()

    main_team = await run_in_thread(get_main_team)
    if main_team is None:
        log.error(
            "Не удалось определить основную команду — пропускаю проверку 'за неделю до ДР'"
        )
        return
    main_team_id = main_team["id"]

    cursor = await common.db.execute("SELECT * FROM users")
    users = await cursor.fetchall()

    for user_row in users:
        try:
            birth_dt = parse_birth_dt(user_row["user_birth"])
            if birth_dt is None:
                continue

            cd = next_celebration(today, birth_dt)
            days_until = (cd - today).days
            # 0 дней (сам ДР) обрабатывает run_day_of_check, поэтому здесь строго 1..7
            if not (1 <= days_until <= 7):
                continue

            await _create_group_for_user(user_row, cd, days_until, main_team_id)

        except Exception:
            log.exception(
                "Ошибка при обработке недельного уведомления для user_id=%s",
                user_row["user_id"],
            )


async def run_day_of_check():
    """В день рождения: напоминает в созданном канале и поздравляет именинника в ЛС."""
    today = today_local()

    cursor = await common.db.execute("SELECT * FROM users")
    users = await cursor.fetchall()

    for user_row in users:
        try:
            await _congratulate_user(user_row, today)
        except Exception:
            log.exception(
                "Ошибка при обработке поздравления для user_id=%s", user_row["user_id"]
            )


async def run_day_after_check():
    """
    На следующий день после ДР: пишет в канал, что он будет удалён через 7 дней.
    Сохраняет notice типа 'day_after' — по нему потом run_channel_delete_check
    поймёт, когда пришло время удалять.
    """
    yesterday = today_local() - timedelta(days=1)

    cursor = await common.db.execute("SELECT * FROM users")
    users = await cursor.fetchall()

    for user_row in users:
        try:
            birth_dt = parse_birth_dt(user_row["user_birth"])
            if (
                birth_dt is None
                or celebration_date(yesterday.year, birth_dt) != yesterday
            ):
                continue

            user_id = user_row["user_id"]
            occurrence_year = yesterday.year

            if await notice_exists(user_id, occurrence_year, "day_after"):
                continue

            # Берём channel_id из записи week_before — там хранится id группового канала
            group_channel_id = await get_week_before_channel(user_id, occurrence_year)
            if not group_channel_id:
                # Канал не был создан (не нашлось коллег) — сохраняем notice без channel_id
                # чтобы не пытаться удалять несуществующий канал
                await save_notice(
                    user_id, occurrence_year, "day_after", channel_id=None
                )
                log.info(
                    "Для @%s нет группового канала — пропускаю предупреждение об удалении",
                    user_id,
                )
                continue

            # day_after выполняется на следующий день после ДР (today = ДР+1),
            # а реальное удаление происходит на ДР+7 (см. run_channel_delete_check),
            # то есть через 6 дней от сегодняшнего дня.
            delete_date = today_local() + timedelta(days=6)
            await run_in_thread(
                driver.posts.create_post,
                options={
                    "channel_id": group_channel_id,
                    "message": (
                        f"📢 Этот канал был создан для организации поздравления именинника.\n"
                        f"Он будет автоматически удалён **{delete_date.strftime('%d.%m.%Y')}**.\n"
                        f"Если вы ещё не поздравили — самое время! 🎉"
                    ),
                },
            )

            await save_notice(
                user_id, occurrence_year, "day_after", channel_id=group_channel_id
            )
            log.info(
                "Отправил предупреждение об удалении канала для @%s, канал %s",
                user_id,
                group_channel_id,
            )

        except Exception:
            log.exception(
                "Ошибка при обработке day_after для user_id=%s", user_row["user_id"]
            )


async def run_channel_delete_check():
    """
    Удаляет групповые каналы через 7 дней после самого ДР.
    day_after пишется на следующий день после ДР, значит удалять нужно
    когда day_after.created_at <= сегодня - 6 дней
    (день ДР + 1 день до day_after + 6 дней = 7 дней после ДР).
    """
    cutoff = now_local() - timedelta(days=6)

    cursor = await common.db.execute(
        """
        SELECT user_id, year, channel_id, created_at
        FROM birthday_notices
        WHERE notice_type = 'day_after'
          AND channel_id IS NOT NULL
          AND created_at <= ?
        """,
        (cutoff.isoformat(),),
    )
    rows = await cursor.fetchall()

    for row in rows:
        user_id = row["user_id"]
        year = row["year"]
        channel_id = row["channel_id"]

        # Проверяем, не удалили ли уже (notice типа 'channel_deleted')
        cursor2 = await common.db.execute(
            "SELECT 1 FROM birthday_notices WHERE user_id = ? AND year = ? AND notice_type = 'channel_deleted'",
            (user_id, year),
        )
        if await cursor2.fetchone() is not None:
            continue

        log.info(
            "Удаляю канал %s (ДР пользователя %s, год %s)", channel_id, user_id, year
        )
        success = await run_in_thread(delete_channel, channel_id)

        if success:
            # Сохраняем факт удаления чтобы не пытаться удалить повторно
            async with common.transaction():
                await common.db.execute(
                    "INSERT OR IGNORE INTO birthday_notices (user_id, year, notice_type, channel_id, created_at) "
                    "VALUES (?, ?, 'channel_deleted', ?, ?)",
                    (user_id, year, channel_id, now_local().isoformat()),
                )


async def run_letter_cleanup_check():
    """
    Через ~7 дней после ДР удаляет послание и пишет об этом в ЛС — только тем,
    у кого послание было. Привязка по notice 'day_after' (есть у всех именинников).
    """
    cutoff = now_local() - timedelta(days=6)
    cursor = await common.db.execute(
        "SELECT user_id, year FROM birthday_notices "
        "WHERE notice_type = 'day_after' AND created_at <= ?",
        (cutoff.isoformat(),),
    )
    rows = await cursor.fetchall()

    for row in rows:
        user_id = row["user_id"]
        year = row["year"]
        try:
            if await notice_exists(user_id, year, "letter_cleared"):
                continue

            cur = await common.db.execute(
                "SELECT letter_to_others FROM users WHERE user_id = ?", (user_id,)
            )
            u = await cur.fetchone()
            had_letter = bool(u and u["letter_to_others"])

            if had_letter:
                async with common.transaction():
                    await common.db.execute(
                        "UPDATE users SET letter_to_others = NULL WHERE user_id = ?",
                        (user_id,),
                    )
                dm = await run_in_thread(get_dm_channel_id, user_id)
                if dm:
                    await run_in_thread(
                        driver.posts.create_post,
                        options={
                            "channel_id": dm,
                            "message": (
                                "Привет, надеюсь, твой день рождения прошёл хорошо. "
                                "Я удалил твоё послание и временную ДР-группу, а за 2 недели "
                                "до следующего ДР предложу добавить новое послание и обновить "
                                "список желаний."
                            ),
                        },
                    )
            # notice ставим всегда — чтобы не проверять этого пользователя повторно
            await save_notice(user_id, year, "letter_cleared")
        except Exception:
            log.exception("Ошибка letter_cleanup для user_id=%s", user_id)


async def run_daily_birthday_checks():
    log.info("Запускаю ежедневную проверку дней рождения")
    await run_two_weeks_before_check()
    await run_week_before_check()
    await run_day_of_check()
    await run_day_after_check()
    await run_channel_delete_check()
    await run_letter_cleanup_check()


async def birthday_scheduler():
    """
    Планировщик проверки дней рождения.

    в енв SCHEDULER_MODE=:
      prod — раз в сутки в 10:00
      test — каждые N минут из енв SCHEDULER_INTERVAL (или 10)

    При неизвестном SCHEDULER_MODE не падаем молча, а логируем ошибку
    и откатываемся к режиму 'prod'.
    """
    mode = os.getenv("SCHEDULER_MODE", "prod").strip().lower()

    if mode not in ("prod", "test"):
        log.error(
            "Неизвестный SCHEDULER_MODE=%r — допустимы 'prod' или 'test'. "
            "Использую режим 'prod'.",
            mode,
        )
        mode = "prod"

    if mode == "test":
        raw_interval = os.getenv("SCHEDULER_INTERVAL", "10")
        try:
            interval_minutes = int(raw_interval)
            if interval_minutes <= 0:
                raise ValueError("интервал должен быть положительным")
        except ValueError:
            log.error(
                "Некорректный SCHEDULER_INTERVAL=%r — использую 10 мин.", raw_interval
            )
            interval_minutes = 10
        log.info(
            "Планировщик запущен в тестовом режиме — проверка каждые %d мин.",
            interval_minutes,
        )
        while True:
            try:
                await run_daily_birthday_checks()
            except Exception:
                log.exception("Ошибка в ежедневной проверке дней рождения")
            log.info("Следующая проверка через %d мин.", interval_minutes)
            await asyncio.sleep(interval_minutes * 60)
    elif mode == "prod":
        log.info("Проверка ежедневно в 10:00 (таймзона %s)", common.BOT_TZ.key)
        while True:
            now = now_local()
            target = now.replace(hour=10, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            log.info(
                "Следующая проверка дней рождения запланирована на %s (через %.0f сек)",
                target,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
            try:
                await run_daily_birthday_checks()
            except Exception:
                log.exception("Ошибка в ежедневной проверке дней рождения")