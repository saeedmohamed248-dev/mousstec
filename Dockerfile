# 🐳 Mouss Tec ERP — صورة التشغيل (ASGI/daphne + Celery)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# مكتبات النظام المطلوبة لبناء psycopg2 والتعامل مع Postgres + الترجمة
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        gettext \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x deploy/entrypoint.web.sh

EXPOSE 8000

# الأمر الافتراضي (docker-compose بيحدد أمر كل خدمة)
CMD ["bash", "deploy/entrypoint.web.sh"]
