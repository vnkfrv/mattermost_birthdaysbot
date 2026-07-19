FROM python:3.11-slim

WORKDIR /app

# Сначала зависимости — слой кэшируется и не пересобирается при правках кода.
# Пакеты берём из внутреннего зеркала PyPI (публичный pypi.org с хоста недоступен).
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --index-url https://repo.corp.tander.ru/repository/pypi/simple \
        --trusted-host repo.corp.tander.ru \
        -r requirements.txt

# Затем сам код бота.
COPY . .

# Переменные окружения (токен, URL, порты) НЕ вшиваются в образ —
# они передаются при запуске через --env-file .env.
CMD ["python", "bot.py"]