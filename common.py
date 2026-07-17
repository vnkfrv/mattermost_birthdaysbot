"""
Общие ресурсы, используемые и основным ботом (bot.py), и планировщиком (scheduler.py):
подключение к БД, клиент Mattermost, логирование, утилиты для работы с пользователями
и датами рождения.
"""

import asyncio
import calendar
import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv
from mattermostdriver import Driver

import ssl_patch

ssl_patch.sslapply()

import urllib3

urllib3.disable_warnings()

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("richard-bot")

# ---------------------------------------------------------------------------
# Валидация переменных окружения
# ---------------------------------------------------------------------------

_REQUIRED_ENV_VARS = ["URL", "BOT_TOKEN", "BOT_USERNAME", "DB_PATH"]


def _validate_env():
    """
    Проверяет наличие всех обязательных переменных окружения.
    Завершает процесс с понятным сообщением, если что-то не задано.
    """
    missing = [var for var in _REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        for var in missing:
            log.critical("Отсутствует обязательная переменная окружения: %s", var)
        log.critical(
            "Убедитесь, что файл .env существует и содержит все нужные переменные: %s",
            ", ".join(_REQUIRED_ENV_VARS),
        )
        sys.exit(1)


_validate_env()

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

BOT_USERNAME = os.getenv("BOT_USERNAME")
MAIN_TEAM_NAME = os.getenv("BIRTHDAY_TEAM_NAME", "magnit")


def _load_tz():
    """Таймзона бота из BOT_TZ (по умолчанию Europe/Moscow), с фолбэком при ошибке."""
    name = os.getenv("BOT_TZ", "Europe/Moscow")
    try:
        return ZoneInfo(name)
    except Exception:
        log.error("Некорректный BOT_TZ=%r — использую Europe/Moscow", name)
        return ZoneInfo("Europe/Moscow")


BOT_TZ = _load_tz()


def now_local():
    """
    Текущее время в таймзоне бота (BOT_TZ), наивное (без tzinfo) — чтобы
    isoformat-строки в БД оставались сравнимыми со старыми записями.
    """
    return datetime.now(BOT_TZ).replace(tzinfo=None)


def today_local():
    """Сегодняшняя дата в таймзоне бота (не зависит от таймзоны машины/контейнера)."""
    return now_local().date()


db = None

# ---------------------------------------------------------------------------
# Клиент Mattermost
# ---------------------------------------------------------------------------

try:
    driver = Driver(
        {
            "url": os.getenv("URL"),
            "token": os.getenv("BOT_TOKEN"),
            # scheme/port берём из окружения: локально http/8065, прод https/443.
            # Дефолты https/443 — значит прод .env менять не нужно.
            "scheme": os.getenv("MM_SCHEME", "https"),
            "port": int(os.getenv("MM_PORT", "443")),
            "verify": False,
        }
    )
except Exception as e:
    log.critical("Не удалось инициализировать клиент Mattermost: %s", e)
    sys.exit(1)


def validate_connection():
    """
    Проверяет работоспособность токена и соединения с Mattermost сразу после login().
    Вызывается из bot.py после driver.login().
    """
    try:
        me = driver.users.get_user("me")
        if not me or not me.get("id"):
            log.critical(
                "Токен BOT_TOKEN принят, но не удалось получить данные бота. "
                "Проверьте права токена в Mattermost."
            )
            sys.exit(1)
        log.info(
            "Успешное подключение к Mattermost как @%s (id=%s)",
            me.get("username"),
            me.get("id"),
        )
    except Exception as e:
        _explain_connection_error(e)
        sys.exit(1)


def _explain_connection_error(e: Exception):
    """Логирует понятное сообщение в зависимости от типа ошибки подключения."""
    err = str(e).lower()
    if "401" in err or "unauthorized" in err:
        log.critical(
            "Ошибка авторизации (401): BOT_TOKEN недействителен или устарел. "
            "Сгенерируйте новый токен в настройках бота в Mattermost."
        )
    elif "403" in err or "forbidden" in err:
        log.critical(
            "Доступ запрещён (403): токен действителен, но у бота нет прав "
            "на выполнение этой операции."
        )
    elif "connection" in err or "refused" in err or "timeout" in err:
        log.critical(
            "Не удалось установить соединение с Mattermost (%s). "
            "Проверьте переменную URL и доступность сервера.",
            os.getenv("URL"),
        )
    elif "ssl" in err or "certificate" in err:
        log.critical(
            "Ошибка SSL при подключении к Mattermost. "
            "Если используется самоподписанный сертификат — убедитесь, что ssl_patch подключён."
        )
    else:
        log.critical("Не удалось подключиться к Mattermost: %s", e)


# ---------------------------------------------------------------------------
# Инициализация БД
# ---------------------------------------------------------------------------


async def init_db():
    global db
    try:
        # isolation_level=None — режим автокоммита: pysqlite не открывает неявных
        # транзакций, поэтому явные BEGIN IMMEDIATE/COMMIT работают предсказуемо
        # (никаких "cannot start a transaction within a transaction").
        db = await aiosqlite.connect(os.getenv("DB_PATH"), isolation_level=None)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        # При WAL запись сериализуется на уровне файла; ждём до 5 сек вместо
        # мгновенной ошибки "database is locked".
        await db.execute("PRAGMA busy_timeout = 5000")
    except Exception as e:
        log.critical("Не удалось открыть базу данных (%s): %s", os.getenv("DB_PATH"), e)
        sys.exit(1)

    try:
        from migrate import run_migrations

        await run_migrations(db)
    except Exception as e:
        log.critical("Ошибка при выполнении миграций БД: %s", e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Вспомогательные утилиты
# ---------------------------------------------------------------------------

# Единственное соединение с БД используется конкурентно двумя задачами одного
# процесса (вебсокет-обработчик и планировщик). Этот лок сериализует запись,
# чтобы транзакция одной задачи не пересекалась с записью другой на общем
# соединении. Все изменяющие данные операции идут через transaction().
db_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def transaction():
    """
    Атомарная транзакция на общем соединении БД.

    Использование:
        async with transaction():
            await db.execute("INSERT ...")
            await db.execute("UPDATE ...")

    Лок не даёт двум корутинам одновременно держать транзакцию на одном
    соединении; BEGIN IMMEDIATE сразу берёт write-lock БД. На исключении —
    откат и проброс ошибки. Внутри transaction() нельзя вызывать функции,
    которые сами берут db_lock (лок не реентрантный) или делают долгие
    сетевые запросы (лок держать всё это время не нужно).
    """
    async with db_lock:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise


async def run_in_thread(func, *args, **kwargs):
    """
    Запускает синхронную функцию Mattermost-драйвера в отдельном потоке,
    чтобы не блокировать event loop.

    Использование:
        result = await run_in_thread(driver.users.get_user, user_id)
        await run_in_thread(driver.posts.create_post, options={...})
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


# Символы Markdown, которые могут сломать форматирование сообщения
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()])")


def escape_md(text):
    """Экранирует спецсимволы Markdown в пользовательском тексте перед вставкой в сообщение."""
    if text is None:
        return text
    return _MD_SPECIAL.sub(r"\\\1", text)


def build_fullname(mm_user, fallback):
    """
    Собирает отображаемое имя из объекта пользователя Mattermost:
    «Имя Фамилия», иначе username, иначе fallback (обычно user_id).
    """
    first = (mm_user.get("first_name") or "").strip()
    last = (mm_user.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full or mm_user.get("username") or fallback


def add_reaction(bot_id, post_id, emoji):
    driver.reactions.create_reaction(
        {
            "user_id": bot_id,
            "post_id": post_id,
            "emoji_name": emoji,
        }
    )


def reply_to_message(channel_id, answer, original_post_id):
    driver.posts.create_post(
        options={
            "channel_id": channel_id,
            "message": answer,
            # "root_id": original_post_id,  # раскомментировать для ответов в тред
        }
    )


def get_dm_channel_id(user_id):
    """
    Открывает (или находит существующий) ЛС-канал бота с пользователем.
    Синхронная функция — из асинхронного кода вызывать через run_in_thread.
    Возвращает id канала или None.
    """
    try:
        ch = driver.channels.create_direct_message_channel(
            [driver.client.userid, user_id]
        )
        return ch["id"]
    except Exception as e:
        log.exception(
            "Не удалось открыть личный чат с пользователем %s: %s", user_id, e
        )
        return None


async def dm_user(user_id, text):
    """Отправляет пользователю личное сообщение от бота (ошибки только логируются)."""
    try:
        channel_id = await run_in_thread(get_dm_channel_id, user_id)
        if not channel_id:
            return
        await run_in_thread(
            driver.posts.create_post,
            options={"channel_id": channel_id, "message": text},
        )
    except Exception as e:
        log.warning("Не удалось отправить ЛС пользователю %s: %s", user_id, e)


def resolve_user_id(raw_username):
    """
    Резолвит @username (или username без @) в (user_id, username) через Mattermost API.
    Возвращает (None, None), если пользователь не найден.
    """
    username = raw_username.lstrip("@").strip()
    if not username:
        return None, None
    try:
        user = driver.users.get_user_by_username(username)
        return user.get("id"), user.get("username")
    except Exception as e:
        log.info("Не удалось найти пользователя по username '%s': %s", username, e)
        return None, None


async def find_existing_gift_for_donor(donor_id, recipient_id, exclude_gift_id=None):
    """
    Проверяет, дарит ли donor_id уже что-то recipient_id (кроме exclude_gift_id).
    Возвращает название найденного подарка или None.
    Использует таблицу gift_donors — без гонки при одновременном выборе.
    """
    if exclude_gift_id is not None:
        cursor = await db.execute(
            """
            SELECT g.gift_name
            FROM gifts g
            JOIN gift_donors gd ON gd.gift_id = g.gift_id
            WHERE g.user_id = ?
              AND gd.user_id = ?
              AND g.gift_id != ?
            LIMIT 1
            """,
            (recipient_id, donor_id, exclude_gift_id),
        )
    else:
        cursor = await db.execute(
            """
            SELECT g.gift_name
            FROM gifts g
            JOIN gift_donors gd ON gd.gift_id = g.gift_id
            WHERE g.user_id = ?
              AND gd.user_id = ?
            LIMIT 1
            """,
            (recipient_id, donor_id),
        )
    row = await cursor.fetchone()
    return row["gift_name"] if row else None


async def claim_gift(donor_id, gift_id):
    """
    Записывает donor_id дарителем подарка gift_id. Все проверки — в одной
    транзакции, поэтому гонка двух дарителей на «последнее место» исключена.

    Возвращает (problem, gift_name, wisher_id):
      problem   — текст ошибки для пользователя (None при успехе);
      gift_name — название подарка (при успехе);
      wisher_id — id именинника (при успехе).

    Единая точка выбора подарка: используется и текстовым сценарием (bot.py),
    и кнопками/модалками (buttons.py).
    """
    gift_name = None
    wisher_id = None
    problem = None
    async with transaction():
        cur = await db.execute(
            """
            SELECT g.gift_name, g.user_id AS wisher_id, g.quantity_want,
                   (SELECT COUNT(*) FROM gift_donors WHERE gift_id = g.gift_id) AS donor_count,
                   (SELECT COUNT(*) FROM gift_donors WHERE gift_id = g.gift_id
                                                       AND user_id = ?) AS i_donate
            FROM gifts g
            WHERE g.gift_id = ?
            """,
            (donor_id, gift_id),
        )
        row = await cur.fetchone()
        if row is None:
            problem = "Это желание уже не найдено."
        elif row["wisher_id"] == donor_id:
            problem = "Нельзя дарить самому себе."
        elif row["i_donate"]:
            problem = "Вы уже дарите этот подарок."
        elif row["donor_count"] >= (row["quantity_want"] or 1):
            problem = "Этот подарок уже разобрали — свободных мест нет."
        else:
            cur2 = await db.execute(
                "SELECT 1 FROM gifts g JOIN gift_donors gd ON gd.gift_id = g.gift_id "
                "WHERE g.user_id = ? AND gd.user_id = ? LIMIT 1",
                (row["wisher_id"], donor_id),
            )
            if await cur2.fetchone():
                problem = "Вы уже выбрали подарок для этого пользователя."
            else:
                gift_name = row["gift_name"]
                wisher_id = row["wisher_id"]
                await db.execute(
                    "INSERT INTO gift_donors (gift_id, user_id) VALUES (?, ?)",
                    (gift_id, donor_id),
                )
    return problem, gift_name, wisher_id


def parse_birth_dt(birth_str):
    """Парсит дату рождения из БД (формат datetime str) в объект datetime, либо None."""
    if not birth_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(birth_str, fmt)
        except ValueError:
            continue
    return None


def format_birth(birth_str):
    """Форматирует дату рождения из БД в ДД.ММ или ДД.ММ.ГГГГ."""
    dt = parse_birth_dt(birth_str)
    if dt is None:
        return "не указана"
    if dt.year in (1, 4):
        # год не был указан пользователем (год 1 или 4 — служебные значения для 29.02)
        return dt.strftime("%d.%m")
    return dt.strftime("%d.%m.%Y")


def is_feb29(birth_dt):
    """True, если дата рождения — 29 февраля."""
    return birth_dt.month == 2 and birth_dt.day == 29


def celebration_date(year, birth_dt):
    """
    Дата празднования ДР в указанном году (объект date).
    29 февраля в невисокосный год по решению команды отмечается 1 марта.
    """
    if is_feb29(birth_dt) and not calendar.isleap(year):
        return date(year, 3, 1)
    return date(year, birth_dt.month, birth_dt.day)


def next_celebration(today, birth_dt):
    """
    Ближайшая дата празднования ДР на сегодня или позже (объект date),
    с учётом переноса 29.02 → 01.03 в невисокосные годы и перехода через год.
    """
    for year in (today.year, today.year + 1):
        cd = celebration_date(year, birth_dt)
        if cd >= today:
            return cd
    # подстраховка — на практике недостижимо
    return celebration_date(today.year + 1, birth_dt)


def parse_user_birth(birth_str):
    """
    Разбирает дату рождения, введённую пользователем:
      'DD.MM'      — без года (хранится со служебным годом 1, для 29.02 — 4);
      'DD.MM.YYYY' — с годом.

    Возвращает datetime либо None, если дата некорректна (например 31.02, 45.13,
    или 29.02 с невисокосным годом — такой даты не существует).
    """
    if not birth_str:
        return None
    s = birth_str.strip()
    parts = s.split(".")

    # С явным годом: DD.MM.YYYY
    if len(parts) == 3:
        try:
            return datetime.strptime(s, "%d.%m.%Y")
        except ValueError:
            return None

    # Без года: DD.MM
    if len(parts) == 2:
        try:
            # служебный год 1 (невисокосный) — обычные даты парсятся как есть
            return datetime.strptime(s + ".0001", "%d.%m.%Y")
        except ValueError:
            # год 1 невисокосный, поэтому 29.02 сюда не пройдёт — обрабатываем явно
            try:
                day, month = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            if (month, day) == (2, 29):
                # ближайший реальный високосный год как служебное хранилище 29.02
                return datetime(4, 2, 29)
            return None

    return None


# ---------------------------------------------------------------------------
# Форматирование списков дней рождения
# ---------------------------------------------------------------------------

# Названия месяцев в родительном падеже (для «15 июля»); индекс = номер месяца.
MONTHS_GENITIVE = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


async def _get_birthday(user_id):
    cur = await db.execute(
        """
        SELECT 
            CAST(strftime('%d', user_birth) AS INTEGER) AS birth_day,
            CAST(strftime('%m', user_birth) AS INTEGER) AS birth_month
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    if not row or row["birth_day"] is None or row["birth_month"] is None:
        return None
    # month индексация с 1, список MONTHS_GENITIVE начинается с пустой строки для индекса 0
    return f"{row['birth_day']} {MONTHS_GENITIVE[row['birth_month']]}"


def format_names_ru(names):
    """
    Склеивает список имён по-русски: перечисление через запятую,
    а перед последним — «и». ['A'] -> 'A'; ['A','B'] -> 'A и B';
    ['A','B','C'] -> 'A, B и C'.
    """
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " и " + names[-1]


# ---------------------------------------------------------------------------
# Ролевая модель Mattermost
# ---------------------------------------------------------------------------
#
# Все функции ниже — синхронные (дёргают driver напрямую), поэтому из bot.py их
# нужно вызывать через run_in_thread, чтобы не блокировать event loop.
#
# При стандартной схеме прав Mattermost принадлежность к channel_admin/team_admin
# помечается булевым полем scheme_admin в объекте участника, а не всегда строкой
# в roles — поэтому проверяем оба варианта. system_admin считается имеющим все
# права (он админ любой команды и любого канала).


def _roles_of(obj):
    """Разбирает поле roles (строка через пробел) в множество ролей."""
    return set((obj.get("roles") or "").split())


def is_system_admin(user_id):
    """True, если пользователь — системный администратор Mattermost."""
    try:
        user = driver.users.get_user(user_id)
    except Exception as e:
        log.warning(
            "Не удалось получить пользователя %s для проверки прав: %s", user_id, e
        )
        return False
    return "system_admin" in _roles_of(user)


def is_channel_admin(user_id, channel_id):
    """True, если пользователь — администратор данного канала (или системный админ)."""
    if is_system_admin(user_id):
        return True
    try:
        member = driver.channels.get_channel_member(channel_id, user_id)
    except Exception as e:
        log.info(
            "Не удалось получить участника канала (%s, %s): %s",
            channel_id,
            user_id,
            e,
        )
        return False
    if member.get("scheme_admin"):
        return True
    return "channel_admin" in _roles_of(member)


def is_team_admin(user_id, team_id):
    """True, если пользователь — администратор данной команды (или системный админ)."""
    if is_system_admin(user_id):
        return True
    if not team_id:
        return False
    try:
        member = driver.teams.get_team_member(team_id, user_id)
    except Exception as e:
        log.info(
            "Не удалось получить участника команды (%s, %s): %s",
            team_id,
            user_id,
            e,
        )
        return False
    if member.get("scheme_admin"):
        return True
    return "team_admin" in _roles_of(member)


def is_channel_member(user_id, channel_id):
    """True, если пользователь состоит в данном канале."""
    try:
        driver.channels.get_channel_member(channel_id, user_id)
        return True
    except Exception:
        return False


def is_team_member(user_id, team_id):
    """True, если пользователь состоит в данной команде."""
    if not team_id:
        return False
    try:
        driver.teams.get_team_member(team_id, user_id)
        return True
    except Exception:
        return False


def _member_is_channel_admin(member):
    """True, если объект участника канала помечен как администратор канала."""
    if member.get("scheme_admin"):
        return True
    return "channel_admin" in _roles_of(member)


def get_channel_members_full(channel_id):
    """
    Возвращает список объектов участников канала (с полями roles/scheme_admin).
    Постранично обходит API Mattermost (по 200 участников на страницу).
    В отличие от get_channel_member_ids сохраняет роли — нужно, чтобы понять,
    кто из участников является администратором канала.
    """
    members_all = []
    page = 0
    per_page = 200
    while True:
        try:
            members = driver.channels.get_channel_members(
                channel_id, params={"page": page, "per_page": per_page}
            )
        except Exception as e:
            log.warning("Не удалось получить участников канала %s: %s", channel_id, e)
            break
        if not members:
            break
        members_all.extend(members)
        if len(members) < per_page:
            break
        page += 1
    return members_all


def get_channel_member_ids(channel_id):
    """
    Возвращает множество user_id всех участников канала.
    Постранично обходит API Mattermost (по 200 участников на страницу).
    """
    member_ids = set()
    for m in get_channel_members_full(channel_id):
        uid = m.get("user_id")
        if uid:
            member_ids.add(uid)
    return member_ids


def promote_to_channel_admin(channel_id, user_id):
    """
    Выдаёт пользователю роль администратора данного канала.
    Пользователь уже должен быть участником канала. Ошибки только логируются.
    """
    try:
        driver.channels.update_channel_roles(
            channel_id, user_id, {"roles": "channel_user channel_admin"}
        )
        return True
    except Exception as e:
        log.warning(
            "Не удалось выдать права админа канала %s пользователю %s: %s",
            channel_id,
            user_id,
            e,
        )
        return False