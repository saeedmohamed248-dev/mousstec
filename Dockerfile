# 🐳 Mouss Tec ERP — صورة التشغيل (ASGI/daphne + Celery)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# مكتبات النظام المطلوبة لبناء psycopg2 والتعامل مع Postgres + الترجمة
# libgomp1: مطلوبة لتشغيل onnxruntime (بوت إزالة الخلفية المحلي).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        gettext \
        curl \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# 🎨 موديل إزالة الخلفية المحلي (U²-Net) — بننزّله وقت البناء عشان استوديو
# الصور يشتغل offline بالكامل من غير أي خدمة خارجية ولا تنزيل وقت أول طلب.
ENV IMAGE_STUDIO_U2NET_PATH=/app/.models/u2net.onnx
RUN mkdir -p /app/.models && \
    curl -fsSL -o /app/.models/u2net.onnx \
    https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx

COPY . .

RUN chmod +x deploy/entrypoint.web.sh

EXPOSE 8000

# الأمر الافتراضي (docker-compose بيحدد أمر كل خدمة)
CMD ["bash", "deploy/entrypoint.web.sh"]
