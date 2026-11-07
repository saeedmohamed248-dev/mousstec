"""
يبعت تنبيه تشغيل لصاحب المنصة من سكربتات السيرفر (مثلاً لو النسخة الاحتياطية فشلت):
    docker compose exec -T web python manage.py ops_alert "العنوان" "التفاصيل"
"""
from django.core.management.base import BaseCommand

from clients.services.ops_alerts import send_ops_alert


class Command(BaseCommand):
    help = 'إرسال تنبيه تشغيل بالإيميل لصاحب المنصة'

    def add_arguments(self, parser):
        parser.add_argument('subject')
        parser.add_argument('body', nargs='?', default='')

    def handle(self, subject, body, **opts):
        ok = send_ops_alert(subject, body or subject)
        self.stdout.write('sent' if ok else 'not sent')
