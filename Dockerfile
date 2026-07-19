FROM python:3.11-slim

WORKDIR /app

# Индекс PyPI. По умолчанию публичный; на закрытом хосте передаётся через
# --build-arg на внутреннее зеркало (Nexus/Artifactory), т.к. pypi.org недоступен.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org

# Сначала зависимости — слой кэшируется и не пересобирается при правках кода.
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --index-url "$PIP_INDEX_URL" \
        --trusted-host "$PIP_TRUSTED_HOST" \
        -r requirements.txt

# Затем сам код бота.
COPY . .

# Переменные окружения (токен, URL, порты) НЕ вшиваются в образ —
# они передаются при запуске через --env-file .env.
CMD ["python", "bot.py"]
