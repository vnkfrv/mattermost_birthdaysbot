FROM python:3.11-slim

# логи Python не буферизуются -> видны в docker logs сразу
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Хост закрыт от pypi.org -> ставим из внутреннего зеркала.
# На машине с интернетом (Mac) переопределить:
#   --build-arg PIP_INDEX_URL=https://pypi.org/simple --build-arg PIP_TRUSTED_HOST=pypi.org
ARG PIP_INDEX_URL=https://repo.corp.tander.ru/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.corp.tander.ru

# Сначала зависимости — слой кэшируется и не пересобирается при правках кода.
COPY requirements.txt .
RUN pip install --no-cache-dir \
    --index-url "$PIP_INDEX_URL" \
    --trusted-host "$PIP_TRUSTED_HOST" \
    -r requirements.txt

# Затем сам код бота.
COPY . .

# Порт HTTP-сервера кнопок (BUTTON_PORT внутри контейнера).
EXPOSE 8081

# Переменные окружения (токен, URL, порты) НЕ вшиваются в образ —
# передаются при запуске через --env-file .env.
CMD ["python", "bot.py"]
