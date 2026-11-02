"""🏷️ عرض مدد مؤقت على كل الباقات (سيارات + طباعة): خصم خاص للـ 3/6/12 شهر والشهري بسعره.

العرض بيخلص لوحده في ``--days`` (الأسعار بترجع لخصومات الباقة العادية من غير أي أمر).
``--pause-coupon CODE`` بيوقف كود خصم طول العرض عشان مايتجمعش فوقه (50% + 50% = 75%).

    python manage.py start_period_offer --quarterly 30 --semi 40 --annual 50 --days 7 --pause-coupon MUSSTEC2026
    python manage.py start_period_offer --stop        # يوقف العرض الشغّال دلوقتي
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from clients.models import Coupon, PlanPromotion


class Command(BaseCommand):
    help = "عرض خصم مؤقت على مدد الاشتراك (3/6/12 شهر) لكل الباقات"

    def add_arguments(self, parser):
        parser.add_argument('--quarterly', type=int, default=30)
        parser.add_argument('--semi', type=int, default=40)
        parser.add_argument('--annual', type=int, default=50)
        parser.add_argument('--days', type=int, default=7)
        parser.add_argument('--label', default='عرض الأسبوع')
        parser.add_argument('--pause-coupon', action='append', default=[], dest='pause')
        parser.add_argument('--stop', action='store_true')

    @transaction.atomic
    def handle(self, *args, **o):
        now = timezone.now()
        running = PlanPromotion.objects.filter(is_active=True, starts_at__lte=now, ends_at__gt=now,
                                               plan__isnull=True, industry='')
        if o['stop']:
            n = running.update(ends_at=now)
            self.stdout.write(self.style.SUCCESS(f"✅ وقفت {n} عرض — الأسعار رجعت لخصومات الباقات العادية."))
            return
        for f in ('quarterly', 'semi', 'annual'):
            if not 0 <= o[f] <= 90:
                raise CommandError(f"--{f} لازم من 0 لـ 90")
        if not 1 <= o['days'] <= 60:
            raise CommandError("--days لازم من 1 لـ 60")
        running.update(ends_at=now)   # عرض واحد بس شغّال في نفس الوقت
        ends = now + timedelta(days=o['days'])
        promo = PlanPromotion(percent_off=0, quarterly_off=o['quarterly'], semi_annual_off=o['semi'],
                              annual_off=o['annual'], label=o['label'], starts_at=now, ends_at=ends)
        promo.full_clean()
        promo.save()
        for code in o['pause']:
            c = Coupon.objects.filter(code=code.strip().upper()).first()
            if c is None:
                self.stdout.write(self.style.WARNING(f"⚠️ الكود {code} مش موجود"))
                continue
            # الكود بيقف لحد آخر العرض (لو كان هيخلص قبلها بيفضل على ميعاده)
            c.starts_at = max(c.starts_at, ends)
            if c.ends_at and c.ends_at <= c.starts_at:
                c.is_active = False
            c.save(update_fields=['starts_at', 'is_active'])
            self.stdout.write(f"⏸️ الكود {c.code} متوقف لحد نهاية العرض")
        self.stdout.write(self.style.SUCCESS(
            f"✅ العرض شغّال: 3 شهور {o['quarterly']}% · 6 شهور {o['semi']}% · سنة {o['annual']}% — "
            f"الشهري بسعره — لحد {timezone.localtime(ends):%Y-%m-%d %H:%M}"))
