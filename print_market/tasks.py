"""⏱️ مهام سوق المطابع في الخلفية."""
from celery import shared_task


@shared_task(ignore_result=True)
def send_shop_whatsapp(phone, name, line, link):
    from .notify import send_shop_whatsapp_now
    send_shop_whatsapp_now(phone, name, line, link)


@shared_task(ignore_result=True)
def auto_complete_orders():
    from .services import run_auto_complete
    return len(run_auto_complete(None))
