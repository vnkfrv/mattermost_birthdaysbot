FROM python:3.11-slim

WORKDIR /app

# Сначала зависимости — слой кэшируется и не пересобирается при правках кода.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем сам код бота.
COPY . .

# Переменные окружения (токен, URL, порты) НЕ вшиваются в образ —
# они передаются при запуске через --env-file .env.
CMD ["python", "bot.py"]
