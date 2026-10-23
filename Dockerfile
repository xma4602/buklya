# ---------------------------------------------------------------------------
# Образ бота «Букля». Один образ для всех платформ; в docker-compose.yml у каждой
# платформы свой контейнер, платформа выбирается командой (python -m buklya vk).
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Samara

WORKDIR /app

# Зависимости ставим отдельным слоем, чтобы не пересобирать их при каждой правке кода
COPY requirements.txt .
RUN pip install -r requirements.txt \
    # драйвер PostgreSQL — чтобы можно было переключиться без пересборки
    && pip install "asyncpg>=0.29"

COPY buklya ./buklya
COPY providers.yaml .
COPY locales ./locales

# Каталоги для БД и логов (монтируются как тома, см. docker-compose.yml)
RUN mkdir -p /app/data /app/logs

VOLUME ["/app/data", "/app/logs"]

# По умолчанию — боты из BOTS (или все, для которых задан токен) в одном процессе.
CMD ["python", "-m", "buklya"]
