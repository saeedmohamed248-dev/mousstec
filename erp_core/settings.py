from pathlib import Path
import os
import environ 
from django.utils.translation import gettext_lazy as _
from datetime import timedelta 
from celery.schedules import crontab 

# 🚀 توجيه نظام الحماية المركزي للـ Dashboard الفخمة مباشرة لمنع الـ 404 عند الدخول
LOGIN_REDIRECT_URL = '/auth/redirect/'
LOGOUT_REDIRECT_URL = '/'
ADMIN_URL = os.getenv('ADMIN_URL', 'secure-portal')
BASE_DOMAIN = os.getenv('BASE_DOMAIN', 'mousstec.com')
BASE_DIR = Path(__file__).resolve().parent.parent

# 🟢 تهيئة قارئ البيئة المخفية
env = environ.Env(
    DEBUG=(bool, False)
)
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

# =====================================================================
# 🛡️ الأمان المتقدم وجدران الحماية (Enterprise Security & CORS)
# =====================================================================
SECRET_KEY = env('SECRET_KEY')
DEBUG = env('DEBUG', default=True)

ALLOWED_HOSTS = [BASE_DOMAIN, f'.{BASE_DOMAIN}', '64.226.120.5', '127.0.0.1', 'localhost']

# 💳 Paymob Payment Gateway
PAYMOB_API_KEY = env('PAYMOB_API_KEY', default='')
PAYMOB_INTEGRATION_ID = env('PAYMOB_INTEGRATION_ID', default='')
PAYMOB_IFRAME_ID = env('PAYMOB_IFRAME_ID', default='')

# 🚀 جدار حماية صارم لمنع هجمات الـ Cross-Site وحماية محافظ الـ Escrow
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = env.list('CORS_ALLOWED_ORIGINS', default=[
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    f"https://{BASE_DOMAIN}"
])
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[
    "http://localhost:8000",
    f"https://*.{BASE_DOMAIN}",
    "http://64.226.120.5"
])

# 🛡️ حماية الجلسات السحابية (معزولة وداعمة للـ Multi-Tenant)
SESSION_COOKIE_AGE = 28800  # 8 ساعات (وردية عمل كاملة للموظف)
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_SECURE = not DEBUG 
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_DOMAIN = f'.{BASE_DOMAIN}' if not DEBUG else None 

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') 
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# 🚀 تأمين مالي من درجة البنوك (Bank-Grade HSTS) في بيئة الإنتاج
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000  
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# 🚧 منع إغراق السيرفر بالملفات الضخمة غير المصرح بها (حماية الرامات من الـ Overload)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760  # 10 MB الحد الأقصى للمرفقات ورخص السيارات

# =====================================================================
# 🚀 محرك التطبيقات المعزول (Multi-Tenant Architecture)
# =====================================================================
SHARED_APPS = (
    'daphne',          # ⚡ محرك الاتصالات الحية
    'django_tenants',  
    'clients',         # 👑 إدارة الإمبراطورية السحابية والمزادات المركزية
    
    'jazzmin', 
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # 🛠️ أدوات الـ Enterprise السحابية والمراقبة اللحظية
    'channels',        
    'rest_framework',  
    'rest_framework_simplejwt', 
    'rest_framework_simplejwt.token_blacklist', 
    'corsheaders',     
    'storages',        
    'axes',            
    'simple_history',  
    'django_celery_beat', 
)

TENANT_APPS = (
    'django.contrib.admin',      
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',   
    'django.contrib.messages',   
    'inventory',       # 🏎️ النواة التشغيلية للورش
    'import_export',
    'rest_framework', 
    'simple_history', 
    'axes',           
)

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

TENANT_MODEL = 'clients.Client'
TENANT_DOMAIN_MODEL = 'clients.Domain'

# =====================================================================
# ⚙️ العمليات الوسيطة وحراس المرور (Middleware Matrix)
# =====================================================================
MIDDLEWARE = [
    'django_tenants.middleware.main.TenantMainMiddleware', # 1. عزل الـ Schema أولاً
    'clients.middleware.TenantQuotaMiddleware',           # 2. حارس الباقات الديناميكي
    
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
    'axes.middleware.AxesMiddleware',                     # 3. صد هجمات التخمين السيبرانية
    'erp_core.middleware.AuditIPMiddleware',              # 4. تسجيل IP المستخدم لسجل المراجعة
]

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]
ROOT_URLCONF = 'erp_core.urls'
PUBLIC_SCHEMA_URLCONF = 'erp_core.urls'

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

# =====================================================================
# 📡 محرك الاتصالات اللحظية (WebSockets Channel Layers)
# =====================================================================
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

# =====================================================================
# 📊 قاعدة البيانات (High-Performance PostgreSQL Routing)
# =====================================================================
DATABASES = {
    'default': env.db('DATABASE_URL', default='postgres://postgres:123@localhost:5432/erp_db')
}
DATABASES['default']['ENGINE'] = 'django_tenants.postgresql_backend'
DATABASES['default']['CONN_MAX_AGE'] = env.int('CONN_MAX_AGE', default=60) 
DATABASES['default']['CONN_HEALTH_CHECKS'] = True

# =====================================================================
# ⚡ الكيش الصاروخي المزدوج (Two-Tier Caching - Failover Enabled)
# =====================================================================
def tenant_key_func(key, key_prefix, version):
    from django.db import connection
    return f"{connection.schema_name}:{key_prefix}:{version}:{key}"

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env('REDIS_URL', default='redis://127.0.0.1:6379/1'),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True, # 🚀 ابتكار: التحول التلقائي الآمن للـ Failover عند سقوط Redis
            "CONNECTION_POOL_KWARGS": {"max_connections": 100, "retry_on_timeout": True}
        },
        "KEY_FUNCTION": "erp_core.settings.tenant_key_func", 
    },
    "local_tier": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "mousstec-local-cache",
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

# =====================================================================
# 🌍 التدويل والتوقيت المحاسبي (Localization & Accounting Sync)
# =====================================================================
LANGUAGE_CODE = 'ar'  
TIME_ZONE = 'Africa/Cairo'
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True 

LANGUAGES = [('ar', _('Arabic')), ('en', _('English'))]
LOCALE_PATHS = [BASE_DIR / 'locale']

# =====================================================================
# 📁 إدارة الأصول الرقمية والتخزين السحابي (Cloud Storage Stack)
# =====================================================================
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
WHITENOISE_MAX_AGE = 31536000  # Cache static files for 1 year (hashed filenames) 

USE_S3 = env.bool('USE_S3', default=False)
if USE_S3:
    AWS_ACCESS_KEY_ID = env('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = env('AWS_SECRET_ACCESS_KEY')
    AWS_STORAGE_BUCKET_NAME = env('AWS_STORAGE_BUCKET_NAME')
    AWS_S3_REGION_NAME = env('AWS_S3_REGION_NAME', default='eu-central-1')
    AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.amazonaws.com'
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
    MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/media/'
    
    # 🚀 ابتكار أمني: روابط مشفرة مؤقتة ذاتية الانتهاء لحماية رخص ومستندات السيارات من التسريب
    AWS_S3_OBJECT_PARAMETERS = {'CacheControl': 'max-age=86400'}
    AWS_QUERYSTRING_AUTH = True
    AWS_QUERYSTRING_EXPIRE = 3600 
    AWS_DEFAULT_ACL = None
else:
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# =====================================================================
# 📧 محرك الاتصالات والمفاتيح السيادية للبوتات (AI Agents & Gateways)
# =====================================================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = env('EMAIL_HOST', default='')
EMAIL_PORT = env.int('EMAIL_PORT', default=587)
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=True)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')

TECDOC_API_KEY = env('TECDOC_API_KEY', default='')
ALLDATA_ENDPOINT = env('ALLDATA_ENDPOINT', default='')

ENABLE_AI_PREDICTIONS = env.bool('ENABLE_AI_PREDICTIONS', default=True)
AI_MODEL_ENDPOINT = env.str('AI_MODEL_ENDPOINT', 'https://generativelanguage.googleapis.com/')
AI_VISION_API_KEY = env.str('AI_VISION_API_KEY', '')

# =====================================================================
# 🎛️ مفاتيح التحكم الديناميكية (SaaS Feature Flags Engine)
# =====================================================================
FEATURE_FLAGS = {
    'BETA_AI_BLIND_BIDDING': env.bool('FLAG_AI_BIDDING', default=True),
    'OCR_VISION_INVOICE': env.bool('FLAG_OCR_INVOICE', default=True),
    'CRYPTO_PAYMENTS_BETA': env.bool('FLAG_CRYPTO_PAYMENTS', default=False),
}

# =====================================================================
# 🎨 إعدادات JAZZMIN (تخصيص هوية Mouss Tec الفاخرة للوحة التحكم)
# =====================================================================
JAZZMIN_SETTINGS = {
    "site_title": "Mouss Tec",
    "site_header": "Mouss Tec منصة",
    "site_brand": "MOUSS TEC",
    "welcome_sign": "مرحباً بك في منصة Mouss Tec للسيارات",
    "copyright": "© 2026 Mouss Tec Ecosystem",
    "search_model": ["inventory.Product", "clients.Client", "inventory.Customer"],
    "user_avatar": None,
    "show_sidebar": True,
    "navigation_expanded": True,
    "topmenu_links": [
        {"name": "الرئيسية", "url": "/", "new_window": False},
        {"name": "لوحة السوبر أدمن", "url": "/superadmin/", "new_window": False},
        {"name": "فحص النظام", "url": "/system/health/", "new_window": True},
    ],
    "icons": {
        "auth": "fas fa-users-cog",
        "auth.user": "fas fa-user",
        "auth.Group": "fas fa-users",
        "clients.Client": "fas fa-building",
        "clients.Domain": "fas fa-globe",
        "clients.GlobalB2BMarketplace": "fas fa-store",
        "clients.BlindBiddingRequest": "fas fa-gavel",
        "clients.BidOffer": "fas fa-tags",
        "clients.EscrowLedger": "fas fa-vault",
        "inventory.Branch": "fas fa-code-branch",
        "inventory.Customer": "fas fa-user-tie",
        "inventory.Vendor": "fas fa-truck",
        "inventory.Product": "fas fa-cogs",
        "inventory.ProductCategory": "fas fa-layer-group",
        "inventory.Inventory": "fas fa-boxes",
        "inventory.PurchaseInvoice": "fas fa-file-import",
        "inventory.SaleInvoice": "fas fa-file-invoice-dollar",
        "inventory.Treasury": "fas fa-coins",
        "inventory.EmployeeProfile": "fas fa-id-badge",
        "inventory.Vehicle": "fas fa-car",
        "inventory.MaintenanceContract": "fas fa-file-contract",
        "inventory.AuditLog": "fas fa-clipboard-list",
        "inventory.ChartOfAccount": "fas fa-sitemap",
        "inventory.AccountingEntry": "fas fa-book",
        "inventory.InventoryMovement": "fas fa-exchange-alt",
        "inventory.StockAlert": "fas fa-exclamation-triangle",
        "inventory.ImportSession": "fas fa-file-upload",
    },
    "order_with_respect_to": ["clients", "inventory", "auth"],
    "changeform_format": "horizontal_tabs",
    "language_chooser": False,
    "show_ui_builder": False,
    "related_modal_active": True,
}

JAZZMIN_UI_TWEAKS = {
    "theme": "darkly",
    "dark_mode_theme": None,
    "navbar_fixed": True,
    "sidebar_fixed": True,
    "sidebar": "sidebar-dark-purple",
    "accent": "accent-purple",
    "navbar": "navbar-dark",
    "no_navbar_border": True,
    "body_small_text": False,
    "brand_colour": "navbar-purple",
    "actions_sticky_top": True,
}

# =====================================================================
# 🔗 بروتوكولات الـ REST APIs وحماية منافذ الـ API (DRF Framework)
# =====================================================================
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50, 
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle', 
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '30/minute',  
        'user': '300/minute', 
        'marketplace_scraping': '100/hour', 
    }
}

# 🚀 ابتكار: ربط حارس الصد السيبراني (Axes) لحظر الـ Token ومخترقي الـ APIs تلقائياً
AXES_DRF_INTEGRATION = True

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# =====================================================================
# 🎛️ أتمتة طابور المهام والتوجيه الطبقي (Enterprise Celery Routing)
# =====================================================================
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://127.0.0.1:6379/0')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='redis://127.0.0.1:6379/0') 
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

CELERY_TASK_ROUTES = {
    # ── Notification queue ───────────────────────────────────────────
    'clients.tasks.async_welcome_bot_task':             {'queue': 'notifications'},
    'inventory.tasks.dispatch_maintenance_reminders':   {'queue': 'notifications'},
    # ── Heavy AI queue ───────────────────────────────────────────────
    'clients.tasks.process_ai_bidding_award':           {'queue': 'heavy_ai_tasks'},
    'inventory.tasks.process_ai_vision_invoice':        {'queue': 'heavy_ai_tasks'},
    'inventory.tasks.sync_elastic_pricing':             {'queue': 'heavy_ai_tasks'},
    # ── Fintech / reconciliation queue ──────────────────────────────
    'clients.tasks.orchestrate_billing_and_suspensions':{'queue': 'urgent_fintech_tasks'},
    'inventory.tasks.process_financial_reconciliation': {'queue': 'urgent_fintech_tasks'},
    # ── B2B Marketplace queue ────────────────────────────────────────
    'clients.tasks.async_sync_b2b_marketplace_product': {'queue': 'b2b_sync'},
    'clients.tasks.async_remove_b2b_marketplace_product':{'queue': 'b2b_sync'},
    # ── DLQ / maintenance queue ──────────────────────────────────────
    'inventory.tasks.drain_dlq_and_retry':              {'queue': 'default'},
}

# 🚀 قائمة كل الـ queues المفعلة في الـ Workers (أضف هنا لتتسق مع celery worker -Q)
CELERY_QUEUES_LIST = ['default', 'notifications', 'heavy_ai_tasks', 'urgent_fintech_tasks', 'b2b_sync']

CELERY_BEAT_SCHEDULE = {
    # ── Billing & Subscription ───────────────────────────────────────
    'orchestrate_billing_and_suspensions': {
        'task': 'clients.tasks.orchestrate_billing_and_suspensions',
        'schedule': crontab(hour=0, minute=5),
    },
    # ── AI Trust & Fraud ─────────────────────────────────────────────
    'update_ai_trust_scores': {
        'task': 'clients.tasks.update_market_trust_scores',
        'schedule': crontab(hour=2, minute=0),
    },
    # ── Proactive Maintenance Reminders ─────────────────────────────
    'dispatch_maintenance_reminders': {
        'task': 'inventory.tasks.dispatch_maintenance_reminders',
        'schedule': crontab(hour=9, minute=0),  # كل يوم 9 صباحاً
    },
    # ── Financial Reconciliation (nightly per active tenant) ─────────
    'financial_reconciliation_nightly': {
        'task': 'inventory.tasks.process_financial_reconciliation',
        'schedule': crontab(hour=1, minute=0),
        'kwargs': {'schema_name': 'public'},  # Beat يُشغِّلها للـ public، الوكيل يتولى الـ tenants
    },
    # ── DLQ Retry Worker ─────────────────────────────────────────────
    'drain_dlq_and_retry': {
        'task': 'inventory.tasks.drain_dlq_and_retry',
        'schedule': crontab(minute=0),  # كل ساعة
    },
}

# =====================================================================
# 📈 رادار دفتر الحسابات والمراقبة السيبرانية (APM & Slow-Query Logs)
# =====================================================================
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
        # 🚀 ابتكار: التقاط وتسجيل أي استعلام بطيء (Slow Query > 250ms) تلقائياً لتحليل الأداء
        'django.db.backends': {
            'handlers': ['audit_file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'mouss_tec_core': { 
            'handlers': ['audit_file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

SENTRY_DSN = env.str('SENTRY_DSN', default='')
if SENTRY_DSN and not DEBUG:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration(), RedisIntegration()],
        traces_sample_rate=0.2, 
        send_default_pii=False,
    )

AXES_ENABLED = True
AXES_FAILURE_LIMIT = 5 
AXES_COOLOFF_TIME = 1 
AXES_RESET_ON_SUCCESS = True 
AXES_LOCKOUT_TEMPLATE = '403.html' 
AXES_META_PREPEND_PATH = True 
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]] 

# =====================================================================
# ⚖️ محددات النظام ومصفوفة الأسعار (B2B SaaS Pricing Constants)
# =====================================================================
MOUSS_TEC_ESCROW_FEE_PERCENTAGE = env.float('ESCROW_FEE_PERCENTAGE', default=2.5)
BLIND_BIDDING_EXPIRY_HOURS = env.int('BLIND_BIDDING_EXPIRY_HOURS', default=24)

SAAS_ADDON_PRICE_EXTRA_BRANCH = 300.00  
SAAS_ADDON_PRICE_EXTRA_USER = 150.00