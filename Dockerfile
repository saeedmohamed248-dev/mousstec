# 🐳 Mouss Tec ERP — صورة التشغيل (ASGI/daphne + Celery)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# مكتبات النظام المطلوبة لبناء psycopg2 والتعامل مع Postgres + الترجمة
# libgomp1: مطلوبة لتشغيل onnxruntime (بوت إزالة الخلفية المحلي).
# libopenblas/liblapack: مكتبات الحساب اللي dlib (تأمين الوجه للروبوت) بيستخدمها؛ cmake لأي حزمة بتتبني.
# ffmpeg: تحويل صوت الروبوت لـ WAV يشغّله الـ ESP32 مباشرة (/speak/?format=wav).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        libpq-dev \
        gettext \
        curl \
        libgomp1 \
        libopenblas-dev \
        liblapack-dev \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-robot.txt ./
RUN pip install -r requirements.txt
# 🤖 اعتماديات الروبوت الاختيارية (face_recognition/dlib + صوت الروبوت edge-tts/gTTS) — منفصلة عن
# requirements.txt عشان ما تبطّأش الـ CI. بتتبني في الصورة بس.
# 🐛 [FIX]: face_recognition بيطلب dlib فـ pip كان بيبنيه من الكود — 20–40 دقيقة ورامات أكتر من
#    2GB، والسيرفر الصغير بيقف على «Building wheel for dlib» أو بيتقتل. دلوقتي dlib-bin
#    (نفس dlib مبني جاهز) من الملف، و face_recognition نفسه --no-deps.
RUN pip install -r requirements-robot.txt \
    && pip install --no-deps "face_recognition>=1.3"

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
