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
