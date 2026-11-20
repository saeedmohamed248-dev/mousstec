"""🔔 مهام Celery لإشعارات الموبايل."""
from celery import shared_task


@shared_task(ignore_result=True, autoretry_for=(ConnectionError,), retry_backoff=True, max_retries=3)
def send_push(schema_name, user_ids, title, body, data=None):
    from .push import deliver
    return deliver(schema_name, user_ids, title, body, data)
