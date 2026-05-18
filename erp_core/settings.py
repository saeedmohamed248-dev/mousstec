from pathlib import Path
import os
import environ # مدير الأسرار الذكي
from django.utils.translation import gettext_lazy as _
from datetime import timedelta # 🚀 ابتكار: للتحكم الدقيق في التوقيتات

# 🚀 توجيه نظام الحماية المركزي للـ Dashboard الفخمة مباشرة لمنع الـ 404
LOGIN_REDIRECT_URL = '/system/dashboard/'
LOGOUT_REDIRECT_URL = '/'
BASE_DIR = Path(__file__).resolve().parent.parent

# 🟢 تهيئة قارئ البيئة المخفية
env = environ.Env(
    DEBUG=(bool, False)
)
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

# ==========================================
# 🛡️ الأمان المتقدم (Enterprise Security & CORS)
# ==========================================
SECRET_KEY = env('SECRET_KEY', default='django-insecure-5)f75m-6r+(53$*=fvp7@88m(h@fwvt^ib4&sainhr8e55x&@_')
DEBUG = env('DEBUG', default=True)

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['.localhost', 'localhost', '127.0.0.1', '[::1]', '64.226.120.5'])

# 🚀 ابتكار: جدار حماية صارم لمنع هجمات الـ Cross-Site
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env.list('CORS_ALLOWED_ORIGINS', default=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://mousstec.com"
])
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[
    "http://localhost:8000",
    "https://*.mousstec.com",
    "http://64.226.120.5"
])

# 🛡️ حماية الجلسات السحابية (معزولة لتناسب الـ Multi-Tenant)
SESSION_COOKIE_AGE = 28800  # 8 ساعات (وردية عمل)
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = not DEBUG 
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') 

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# 🚧 منع إغراق السيرفر بالملفات الضخمة (حماية الرامات)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760  # 10 MB

# ==========================================
# 🚀 التطبيقات (Multi-Tenant Architecture)
# ==========================================
SHARED_APPS = (
    'daphne',          # ⚡ محرك الاتصالات الحية (يجب أن يكون الأول)
    'django_tenants',  
    'clients',         # 👑 إدارة الإمبراطورية والمزادات
    'jazzmin', 
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # 🛠️ أدوات الـ Enterprise السحابية
    'channels',        
    'rest_framework',  
    'rest_framework_simplejwt', 
    'rest_framework_simplejwt.token_blacklist', # ⚠️ ضروري جداً لعمل خاصية BLACKLIST_AFTER_ROTATION
    'corsheaders',     
    'storages',        
    'axes',            
    'simple_history',  
)

TENANT_APPS = (
    'django.contrib.admin',      
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',   
    'django.contrib.messages',   
    'inventory',      
    'import_export',
    'rest_framework', 
    'simple_history', 
    'axes',           
)

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

TENANT_MODEL = 'clients.Client'
TENANT_DOMAIN_MODEL = 'clients.Domain'

# ==========================================
# ⚙️ العمليات الوسيطة (Middleware)
# ==========================================
MIDDLEWARE = [
    'django_tenants.middleware.main.TenantMainMiddleware', 
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware', 
    'corsheaders.middleware.CorsMiddleware',      
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware', 
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'simple_history.middleware.HistoryRequestMiddleware', 
    'axes.middleware.AxesMiddleware', 
]

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]
ROOT_URLCONF = 'erp_core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'erp_core.wsgi.application'
ASGI_APPLICATION = 'erp_core.asgi.application'

# 📡 محرك قنوات الـ WebSockets 
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [env('REDIS_URL', default='redis://127.0.0.1:6379/1')],
            "capacity": 1500, 
            "expiry": 10,
        },
    },
}

DATABASE_ROUTERS = (
    'django_tenants.routers.TenantSyncRouter',
)

# ==========================================
# 📊 قاعدة البيانات (High-Performance PostgreSQL)
# ==========================================
DATABASES = {
    'default': env.db('DATABASE_URL', default='postgres://postgres:123@localhost:5432/erp_db')
}
DATABASES['default']['ENGINE'] = 'django_tenants.postgresql_backend'
DATABASES['default']['CONN_MAX_AGE'] = env.int('CONN_MAX_AGE', default=60)
DATABASES['default']['CONN_HEALTH_CHECKS'] = True

# ==========================================
# ⚡ الكيش الصاروخي المتقدم (Tenant-Aware Redis)
# ==========================================
def tenant_key_func(key, key_prefix, version):
    from django.db import connection
    tenant_schema = connection.schema_name
    return f"{tenant_schema}:{key_prefix}:{version}:{key}"

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env('REDIS_URL', default='redis://127.0.0.1:6379/1'),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True, 
            # 🚀 ابتكار: تفعيل الـ Connection Pool لمنع السقوط تحت الضغط
            "CONNECTION_POOL_KWARGS": {"max_connections": 100}
        },
        "KEY_FUNCTION": "erp_core.settings.tenant_key_func", 
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# ==========================================
# 🌍 اللغة، التوقيت، والمحاسبة
# ==========================================
LANGUAGE_CODE = 'ar'  
TIME_ZONE = 'Africa/Cairo'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True

LANGUAGES = [('ar', _('Arabic')), ('en', _('English'))]
LOCALE_PATHS = [BASE_DIR / 'locale']

# ==========================================
# 📁 الملفات الثابتة والتخزين السحابي المتقدم
# ==========================================
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage' 

USE_S3 = env.bool('USE_S3', default=False)
if USE_S3:
    AWS_ACCESS_KEY_ID = env('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = env('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = env('AWS_STORAGE_BUCKET_NAME')
    AWS_S3_REGION_NAME = env('AWS_S3_REGION_NAME', default='eu-central-1')
    AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
    MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/media/'
    
    # 🚀 ابتكار: تسريع السحابة وتقليل فاتورة AWS بالكاش المتقدم
    AWS_S3_OBJECT_PARAMETERS = {
        'CacheControl': 'max-age=86400', # تخزين الصور ليوم كامل في المتصفح
    }
    AWS_DEFAULT_ACL = 'public-read'
else:
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ==========================================
# 📧 محرك البريد و APIs الذكاء الاصطناعي
# ==========================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = env('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = env.int('EMAIL_PORT', default=587)
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=True)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='your-email@gmail.com')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='your-app-password')

TECDOC_API_KEY = env('TECDOC_API_KEY', default='')
ALLDATA_ENDPOINT = env('ALLDATA_ENDPOINT', default='')

ENABLE_AI_PREDICTIONS = env.bool('ENABLE_AI_PREDICTIONS', default=True)
AI_MODEL_ENDPOINT = env.str('AI_MODEL_ENDPOINT', 'https://generativelanguage.googleapis.com/v1beta/openai/')
AI_VISION_API_KEY = env.str('AI_VISION_API_KEY', '')

# ==========================================
# 🎨 إعدادات JAZZMIN (تخصيص هوية Mouss Tec)
# ==========================================
JAZZMIN_SETTINGS = {
    "site_title": "Mouss Tec Platform",
    "site_header": "Mouss Tec",
    "site_brand": "Mouss Tec B2B",
    "welcome_sign": "مرحباً بك في منصة Mouss Tec للسيارات",
    "copyright": "Mouss Tec Ecosystem",
    "search_model": ["inventory.Product", "clients.Client", "inventory.Customer"], 
    "user_avatar": None,
    "show_sidebar": True,
    "navigation_expanded": True,
    "icons": {
        "auth": "fas fa-users-cog",
        "clients.Client": "fas fa-building", 
        "clients.GlobalB2BMarketplace": "fas fa-globe-africa", 
        "clients.BlindBiddingRequest": "fas fa-gavel",         
        "inventory.Branch": "fas fa-store",
        "inventory.Customer": "fas fa-user-tie", 
        "inventory.Vendor": "fas fa-truck-loading", 
        "inventory.Product": "fas fa-tools",
        "inventory.Inventory": "fas fa-boxes",
        "inventory.PurchaseInvoice": "fas fa-file-import",
        "inventory.SaleInvoice": "fas fa-file-invoice-dollar",
        "inventory.Treasury": "fas fa-vault",
    },
    "order_with_respect_to": ["clients", "inventory", "auth"],
    "changeform_format": "horizontal_tabs",
    "language_chooser": True,
    "show_ui_builder": True, 
}

JAZZMIN_UI_TWEAKS = {
    "theme": "flatly", 
    "dark_mode_theme": "darkly",
    "navbar_fixed": True,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-primary", 
}

# ==========================================
# 🔗 الربط الخارجي و الـ APIs (DRF & Celery)
# ==========================================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle'
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '30/minute',  
        'user': '300/minute', 
    }
}

# 🚀 ابتكار: تأمين الأجهزة اللوحية والموبايل (Strict JWT)
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://127.0.0.1:6379/0')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='redis://127.0.0.1:6379/0') 
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# 🚀 ابتكار: توجيه ذكي للمهام لمنع اختناق السيرفر (Task Micro-routing)
CELERY_TASK_ROUTES = {
    'inventory.tasks.process_ai_vision_*': {'queue': 'heavy_ai_tasks'},
    'inventory.tasks.process_financial_*': {'queue': 'urgent_fintech_tasks'},
}

# ==========================================
# 📈 نظام المراقبة الشامل (FinTech Audit Logging)
# ==========================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'audit_format': {
            'format': '[%(asctime)s] %(levelname)s [%(name)s:%(lineno)s] - %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'erp_errors.log', 
        },
        'audit_file': { 
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'mouss_tec_audit.log',
            'maxBytes': 1024 * 1024 * 15, 
            'backupCount': 10,
            'formatter': 'audit_format',
        },
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'audit_format',
        }
    },
    'loggers': {
        'django': {
            'handlers': ['file'],
            'level': 'ERROR',
            'propagate': True,
        },
        'mouss_tec_core': { 
            'handlers': ['audit_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

AXES_ENABLED = True
AXES_FAILURE_LIMIT = 5 
AXES_COOLOFF_TIME = 1 
AXES_RESET_ON_SUCCESS = True 
AXES_LOCKOUT_TEMPLATE = '403.html' 
AXES_META_PREPEND_PATH = True 
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]] 

# ==========================================
# ⚖️ ثوابت Mouss Tec للسياسات المالية (B2B Policies)
# ==========================================
MOUSS_TEC_ESCROW_FEE_PERCENTAGE = env.float('ESCROW_FEE_PERCENTAGE', default=2.5)
BLIND_BIDDING_EXPIRY_HOURS = env.int('BLIND_BIDDING_EXPIRY_HOURS', default=24)