# زرع حزم AI Studio الافتراضية عشان تفعيل الحزم من السوبر أدمن يشتغل.
# الاستخدام: python manage.py seed_ai_packages
# آمن ويتعاد تشغيله (idempotent) — بيعمل update_or_create بالـ slug.
from django.core.management.base import BaseCommand

from clients.models import AIAddonPackage

DEFAULTS = [
    {
        "slug": "ai-basic", "name": "AI Studio — أساسية", "monthly_price": 199,
        "ai_generations_limit": 50, "whatsapp_messages_limit": 200,
        "features": ["تصميم بالذكاء الاصطناعي", "خلفيات تلقائية"], "sort_order": 1,
    },
    {
        "slug": "ai-pro", "name": "AI Studio — احترافية", "monthly_price": 499,
        "ai_generations_limit": 200, "whatsapp_messages_limit": 1000,
        "features": ["كل مميزات الأساسية", "علامة مائية", "دقة أعلى"], "sort_order": 2,
    },
    {
        "slug": "ai-empire", "name": "AI Studio — إمبراطورية", "monthly_price": 999,
        "ai_generations_limit": 1000, "whatsapp_messages_limit": 5000,
        "features": ["كل مميزات الاحترافية", "توليد غير محدود عملياً", "أولوية المعالجة"],
        "sort_order": 3,
    },
]


class Command(BaseCommand):
    help = "زرع حزم AI Studio الافتراضية (idempotent)"

    def handle(self, *args, **options):
        created, updated = 0, 0
        for row in DEFAULTS:
            slug = row.pop("slug")
            obj, was_created = AIAddonPackage.objects.update_or_create(
                slug=slug, defaults={**row, "is_active": True},
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"➕ {obj.name} ({slug})"))
            else:
                updated += 1
                self.stdout.write(f"✓ {obj.name} ({slug}) — محدّثة")
        self.stdout.write(self.style.SUCCESS(
            f"\n✅ تم: {created} حزمة جديدة، {updated} محدّثة. دلوقتي تقدر تفعّل AI Studio لأي شركة."
        ))
