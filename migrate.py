"""
Миграции базы данных.
Запускается автоматически при старте бота (вызывается из init_db в common.py).
Каждая миграция идемпотентна — безопасно запускать повторно.
"""

import logging

log = logging.getLogger("richard-bot")

# Список миграций в порядке применения.
# Каждая запись: (migration_id, описание, SQL).
# Все таблицы создаются сразу в финальном виде (бот разворачивается с нуля),
# поэтому никаких миграций с переносом данных не требуется.


async def _add_registration_date(db):
    """
    Добавляет столбец registration_date в таблицу users.
    Идемпотентна: проверяет наличие столбца перед ALTER (у старых БД, где
    пользователи уже были заведены, значение останется NULL).
    """
    cursor = await db.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in await cursor.fetchall()]
    if "registration_date" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN registration_date TEXT")
    await db.commit()


async def _add_letter_to_others(db):
    """
    Добавляет столбец letter_to_others в таблицу users. Идемпотентна.
    Хранит послание пользователя коллегам, которое показывается во временной
    ДР-группе и удаляется через неделю после дня рождения.
    """
    cursor = await db.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in await cursor.fetchall()]
    if "letter_to_others" not in cols:
        await db.execute("ALTER TABLE users ADD COLUMN letter_to_others TEXT")
    await db.commit()


async def _extend_notice_types(db):
    """
    Расширяет CHECK-ограничение notice_type новыми значениями
    ('two_weeks_before', 'letter_cleared'). SQLite не умеет ALTER CHECK —
    пересобираем таблицу. Идемпотентна: если новый тип уже в схеме — выходим.
    """
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='birthday_notices'"
    )
    row = await cursor.fetchone()
    if row and "two_weeks_before" in (row[0] or ""):
        return  # уже мигрировано

    # PRAGMA foreign_keys нельзя менять внутри транзакции — соединение в автокоммите.
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.execute("BEGIN")
    try:
        await db.execute(
            """
            CREATE TABLE birthday_notices_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                year        INTEGER NOT NULL,
                notice_type TEXT NOT NULL CHECK(notice_type IN (
                                'two_weeks_before', 'week_before', 'day_of',
                                'day_after', 'channel_deleted', 'letter_cleared'
                            )),
                channel_id  TEXT,
                created_at  TEXT NOT NULL,
                UNIQUE (user_id, year, notice_type)
            )
            """
        )
        await db.execute(
            "INSERT INTO birthday_notices_new "
            "(id, user_id, year, notice_type, channel_id, created_at) "
            "SELECT id, user_id, year, notice_type, channel_id, created_at "
            "FROM birthday_notices"
        )
        await db.execute("DROP TABLE birthday_notices")
        await db.execute("ALTER TABLE birthday_notices_new RENAME TO birthday_notices")
        await db.execute("COMMIT")
    except Exception:
        await db.execute("ROLLBACK")
        raise
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


MIGRATIONS = [
    (
        1,
        "Создание таблицы users",
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id       TEXT NOT NULL UNIQUE,
            user_fullname TEXT NOT NULL,
            user_birth    TEXT NOT NULL,
            PRIMARY KEY (user_id)
        )
        """,
    ),
    (
        2,
        "Создание таблицы gifts",
        """
        CREATE TABLE IF NOT EXISTS gifts (
            gift_id          INTEGER NOT NULL UNIQUE,
            gift_name        TEXT NOT NULL,
            gift_link        TEXT,
            quantity_want    INTEGER NOT NULL,
            user_id          TEXT NOT NULL,
            PRIMARY KEY (gift_id AUTOINCREMENT),
            FOREIGN KEY (user_id) REFERENCES users (user_id)
                ON DELETE CASCADE ON UPDATE CASCADE
        )
        """,
    ),
    (
        3,
        "Создание таблицы birthday_notices",
        """
        CREATE TABLE IF NOT EXISTS birthday_notices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT NOT NULL,
            year        INTEGER NOT NULL,
            notice_type TEXT NOT NULL CHECK(notice_type IN (
                            'week_before', 'day_of', 'day_after', 'channel_deleted'
                        )),
            channel_id  TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE (user_id, year, notice_type)
        )
        """,
    ),
    (
        4,
        "Создание таблицы gift_donors",
        """
        CREATE TABLE IF NOT EXISTS gift_donors (
            gift_id INTEGER NOT NULL REFERENCES gifts(gift_id) ON DELETE CASCADE,
            user_id TEXT    NOT NULL,
            PRIMARY KEY (gift_id, user_id)
        )
        """,
    ),
    (
        5,
        "Добавление registration_date в users",
        _add_registration_date,
    ),
    (
        6,
        "Добавление letter_to_others в users",
        _add_letter_to_others,
    ),
    (
        7,
        "Расширение notice_type (two_weeks_before, letter_cleared)",
        _extend_notice_types,
    ),
]


async def run_migrations(db):
    """Применяет все миграции из MIGRATIONS, которые ещё не были применены."""

    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id INTEGER PRIMARY KEY,
            description  TEXT NOT NULL,
            applied_at   TEXT NOT NULL
        )
        """)
    await db.commit()

    cursor = await db.execute("SELECT migration_id FROM schema_migrations")
    rows = await cursor.fetchall()
    applied = {row[0] for row in rows}

    for migration_id, description, sql_or_fn in MIGRATIONS:
        if migration_id in applied:
            continue

        log.info("Применяю миграцию #%d: %s", migration_id, description)
        try:
            if callable(sql_or_fn):
                await sql_or_fn(db)
            else:
                await db.execute(sql_or_fn)
                await db.commit()
        except Exception as e:
            log.critical(
                "Ошибка при применении миграции #%d (%s): %s",
                migration_id,
                description,
                e,
            )
            raise

        await db.execute(
            "INSERT INTO schema_migrations (migration_id, description, applied_at) "
            "VALUES (?, ?, datetime('now'))",
            (migration_id, description),
        )
        await db.commit()
        log.info("Миграция #%d применена", migration_id)

    log.info("Все миграции применены")