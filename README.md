# Richard — ДР-бот для Mattermost

Бот ведёт даты рождения сотрудников и их wish-list'ы, за неделю до ДР создаёт временный канал с коллегами именинника, помогает выбрать подарки без дублей и поздравляет в нужный день. Управление — кнопками и модалками Mattermost, текстовые команды в ЛС оставлены как запасной путь.

Этот README описывает полный локальный запуск с нуля: Mattermost в Docker, создание тестовых пользователей, настройка и запуск бота. Все команды написаны так, чтобы одинаково работать на основных архитектурах и ОС.

---

## 1. Поддерживаемые платформы

| Платформа | Архитектура | Что нужно знать |
|---|---|---|
| Linux (Intel/AMD) | linux/amd64 | Всё работает нативно |
| Linux (ARM-серверы, Raspberry Pi 4/5 c 64-битной ОС) | linux/arm64 | Образы Mattermost и Postgres мультиарх — работают нативно |
| macOS (Apple Silicon M1–M4) | linux/arm64 | Docker Desktop, работает нативно; эмуляция не нужна |
| macOS (Intel) | linux/amd64 | Docker Desktop |
| Windows 10/11 | amd64 (WSL2) | Docker Desktop с бэкендом WSL2; команды выполнять в терминале WSL (Ubuntu) |

Не поддерживается: 32-битный ARM (armv7, старые Raspberry Pi) — у Mattermost нет таких образов. Если какой-то сторонний образ не собран под вашу архитектуру, добавьте сервису в compose строку `platform: linux/amd64` — Docker запустит его через эмуляцию (QEMU/Rosetta), медленнее, но работает.

Требования: Docker 20.10+ (для `host-gateway`), Python 3.9+ (для `zoneinfo`), git.

## 2. Универсальная команда compose

На разных системах Compose вызывается по-разному: новый плагин — `docker compose` (v2, идёт с Docker Desktop и свежими пакетами Docker), старый отдельный бинарь — `docker-compose` (v1, встречается на старых Linux-серверах). Чтобы не думать об этом, определите команду один раз в начале сессии:

```bash
# bash / zsh (Linux, macOS, WSL)
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi
echo "Использую: $DC"
```

```powershell
# PowerShell (Windows): в Docker Desktop всегда доступен v2
$DC = "docker compose"
```

Дальше во всех командах используется `$DC`. Файл `docker-compose.yml` в репозитории совместим с обеими версиями.

## 3. Запуск Mattermost в Docker

В корне проекта лежит `docker-compose.yml` (Postgres + Mattermost Team Edition, мультиарх). Ключевые настройки уже заданы переменными окружения: включён local mode для `mmctl`, разрешено создание бот-аккаунтов и токенов, а в `AllowedUntrustedInternalConnections` внесены адреса, по которым Mattermost сможет стучаться в HTTP-сервер кнопок бота (иначе кнопки и модалки молча не будут работать).

```bash
$DC up -d
$DC ps                       # оба контейнера должны быть Up (postgres — healthy)
$DC logs -f mattermost       # дождитесь строки "Server is listening on :8065", Ctrl+C
```

Проверка: откройте http://localhost:8065 — должна открыться страница входа Mattermost. Данные живут в именованных volume'ах (специально, а не в bind-mount: так нет проблем с правами на файлы, одинаково на всех ОС). Полный сброс окружения: `$DC down -v`.

## 4. Создание админа, команды и тестовых пользователей

Всё делается через `mmctl` внутри контейнера — команды идентичны на любой платформе. Флаг `--local` работает через unix-сокет, авторизация не нужна.

```bash
MM="docker exec -i mattermost mmctl --local"

# 4.1. Системный администратор (для входа в веб-интерфейс)
$MM user create --email admin@example.com --username admin \
    --password 'Admin123!' --system-admin

# 4.2. Команда компании. Имя (slug) должно совпадать с BIRTHDAY_TEAM_NAME
#      из .env бота (по умолчанию magnit)
$MM team create --name magnit --display-name "Magnit"

# 4.3. Тестовые пользователи
$MM user create --email anna@example.com  --username anna.petrova  --password 'Test123!' --firstname Анна  --lastname Петрова
$MM user create --email ivan@example.com  --username ivan.sidorov  --password 'Test123!' --firstname Иван  --lastname Сидоров
$MM user create --email olga@example.com  --username olga.smirnova --password 'Test123!' --firstname Ольга --lastname Смирнова

# 4.4. Все — в команду
$MM team users add magnit admin anna.petrova ivan.sidorov olga.smirnova

# 4.5. Общий канал (бот ищет коллег именинника по общим каналам,
#      поэтому пользователи и бот должны состоять хотя бы в одном общем канале)
$MM channel create --team magnit --name office --display-name "Офис"
$MM channel users add magnit:office admin anna.petrova ivan.sidorov olga.smirnova
```

## 5. Создание бота и токена

```bash
# 5.1. Бот-аккаунт. Username должен совпадать с BOT_USERNAME из .env
$MM bot create richard --display-name "Richard" --description "Праздничный ДР-бот"

# 5.2. Токен — ВЫВОД СОХРАНИТЕ, это BOT_TOKEN для .env (показывается один раз)
$MM token generate richard richard-bot-token

# 5.3. Права system_admin: бот удаляет чужие посты при delete-me,
#      создаёт каналы и добавляет в них людей — без этой роли часть функций откажет
$MM roles system_admin richard

# 5.4. Бот — в команду и в общий канал
$MM team users add magnit richard
$MM channel users add magnit:office richard
```

## 6. Настройка и запуск бота

### 6.1. Окружение Python

```bash
python3 --version              # нужен 3.9+
python3 -m venv .venv

source .venv/bin/activate      # Linux / macOS / WSL
# .venv\Scripts\Activate.ps1   # Windows PowerShell

pip install -r requirements.txt
```

### 6.2. Модуль ssl_patch

Бот импортирует `ssl_patch` (в проде он отключает проверку самоподписанного сертификата). Для локального запуска по HTTP достаточно заглушки — если файла `ssl_patch.py` нет рядом с `bot.py`, создайте его:

```python
# ssl_patch.py — заглушка для локального запуска по HTTP.
def sslapply():
    pass
```

### 6.3. Файл .env

Скопируйте пример и подставьте токен из шага 5.2:

```bash
cp .env.example .env
```

Содержимое для локального запуска (файл `.env.example` уже заполнен этими значениями, кроме токена):

```ini
# --- Подключение к Mattermost ---
URL=localhost                 # хост БЕЗ схемы и порта
MM_SCHEME=http                # локально http; в проде https
MM_PORT=8065                  # локально 8065; в проде 443
BOT_TOKEN=<токен из шага 5.2>
BOT_USERNAME=richard

# --- Бот ---
DB_PATH=richard.db
BIRTHDAY_TEAM_NAME=magnit     # slug команды из шага 4.2
BOT_TZ=Europe/Moscow          # таймзона расписания и меток «СЕГОДНЯ»

# --- HTTP-сервер кнопок/модалок ---
BUTTON_PORT=8080
# Адрес, по которому КОНТЕЙНЕР Mattermost достучится до бота на хосте:
BOT_PUBLIC_URL=http://host.docker.internal:8080

# --- Планировщик ---
SCHEDULER_MODE=test           # test — проверка каждые N минут; prod — раз в день в 10:00 BOT_TZ
SCHEDULER_INTERVAL=2          # минуты, только для режима test
```

Про `BOT_PUBLIC_URL` — это самая частая причина «кнопки не работают». Mattermost сам делает POST-запросы на этот адрес при каждом нажатии кнопки, поэтому адрес должен быть достижим **изнутри контейнера**, а не с хоста. `host.docker.internal` работает на macOS и Windows из коробки, а на Linux — благодаря строке `extra_hosts: host.docker.internal:host-gateway` в нашем compose-файле (Docker 20.10+). Если у вас очень старый Docker на Linux, замените на `http://172.17.0.1:8080` (IP docker-моста, посмотреть: `ip addr show docker0`).

### 6.4. Запуск

```bash
python bot.py
```

В логе должно появиться: успешное подключение как `@richard`, применение миграций БД, `HTTP-сервер кнопок слушает на 0.0.0.0:8080` и режим планировщика.

## 7. Проверка сценария

Зайдите на http://localhost:8065 под `anna.petrova / Test123!` (при первом входе выберите команду Magnit). Дальше: напишите боту в ЛС `@richard` — придёт эфемерка с кнопкой «Зарегистрироваться»; пройдите модалку (введите дату, например завтрашнюю, подтвердите) — откроется меню. Добавьте пару желаний через «Добавить желание». Под вторым пользователем (`ivan.sidorov`) зарегистрируйтесь и через «Wish-list друга» / «Выбрать подарок» забронируйте подарок Анне. В канале «Офис» упомяните `@richard` — появятся кнопки «Ближайшие/Все дни рождения» и «Админ» (последняя доступна admin'у). Если дата рождения Анны в пределах 7 дней, бот сразу создаст приватный канал `🎉 ДР Анна Петрова <год>` и позовёт туда коллег; в режиме `SCHEDULER_MODE=test` фоновые проверки крутятся каждые `SCHEDULER_INTERVAL` минут, ждать 10:00 не нужно.

## 8. Тесты

Юнит- и интеграционные тесты не требуют Mattermost (зависимости подменяются заглушками, БД — временная SQLite):

```bash
python3 tests/test_richard.py
```

## 9. Опционально: бот тоже в Docker

Если хотите запускать бота контейнером рядом с Mattermost, добавьте `Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

и сервис в `docker-compose.yml`:

```yaml
  richard:
    build: .
    container_name: richard
    restart: unless-stopped
    env_file: .env
    depends_on:
      - mattermost
    volumes:
      - richard-data:/app/data
```

При этом в `.env` меняются адреса: `URL=mattermost` (имя сервиса в сети compose), `BOT_PUBLIC_URL=http://richard:8080`, `DB_PATH=/app/data/richard.db`, а в `MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS` контейнера Mattermost нужно добавить слово `richard`. Не забудьте объявить volume `richard-data:` в секции `volumes`.

## 10. Диагностика типовых проблем

**Кнопки/модалки не реагируют или в логе Mattermost «address forbidden».** Проверьте цепочку: бот жив на хосте — `curl http://localhost:8080/health` должен вернуть `ok`; контейнер достаёт до бота — `docker exec mattermost curl -s http://host.docker.internal:8080/health`; хост из `BOT_PUBLIC_URL` перечислен в `MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS` (после правки compose — `$DC up -d`, чтобы пересоздать контейнер). На Linux с firewalld/ufw убедитесь, что порт 8080 открыт для docker-моста.

**`docker-compose: command not found` или наоборот.** Используйте определение `$DC` из раздела 2. На совсем старых серверах v1 ставится одной командой: `sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose && sudo chmod +x /usr/local/bin/docker-compose`.

**`no matching manifest for linux/arm64`.** Какой-то образ не собран под вашу архитектуру — добавьте этому сервису `platform: linux/amd64` (эмуляция). Образов из этого README это не касается: `mattermost/mattermost-team-edition` и `postgres` мультиарх.

**Порт занят (`bind: address already in use`).** Поменяйте левую часть проброса в compose (`"8066:8065"`) и соответственно `MM_SERVICESETTINGS_SITEURL` и `MM_PORT` в `.env`.

**Бот падает на старте с «Отсутствует обязательная переменная окружения».** Обязательны `URL`, `BOT_TOKEN`, `BOT_USERNAME`, `DB_PATH` — проверьте, что `.env` лежит рядом с `bot.py` и заполнен.

**Ошибка 401 при подключении.** Токен неверный или пересоздан — сгенерируйте заново (шаг 5.2) и обновите `BOT_TOKEN`.

**«Бот не состоит в основной команде 'magnit'».** Slug команды в Mattermost не совпадает с `BIRTHDAY_TEAM_NAME`, либо бот не добавлен в команду (шаг 5.4).

**Windows: `python3` не найден.** Используйте `python` вместо `python3`; пакет `tzdata` из requirements.txt закрывает отсутствие системной базы таймзон.