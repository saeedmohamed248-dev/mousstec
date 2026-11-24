"""
Celery app configuration for Mousstec ERP.

Loads broker/backend URLs and the CELERY_BEAT_SCHEDULE defined in settings.py.
Autodiscovers tasks from every installed app (clients/, inventory/, hr/,
smart_diagnostics/, etc).
"""
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'erp_core.settings')

app = Celery('erp_core')

# Pull all settings prefixed with CELERY_ from Django settings
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks.py modules in every installed app
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')


@app.on_after_configure.connect
def _extra_periodic_tasks(sender, **kwargs):
    """جداول زيادة على CELERY_BEAT_SCHEDULE (settings.py عند حد حجمه — test_codebase_structure)."""
    from celery.schedules import crontab
    # 📈 بورصة أسعار المطابع: تذكير واتساب الصبح للي لسه ماسعّرش النهارده (لينك تأكيد بضغطة)
    sender.add_periodic_task(
        crontab(hour=9, minute=7),
        sender.signature('clients.services.price_board.send_daily_price_reminders'),
        name='price_board_daily_reminder',
    )
