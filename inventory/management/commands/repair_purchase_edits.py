# 🩹 إصلاح فواتير شراء اتعدّلت ومخزونها مارجعش (قبل إصلاح خطأ التعديل).
#
# التعديل كان بيشيل كميات الفاتورة من المخزن ومستحقاتها من المورد ومابيرجّعهاش،
# والفاتورة تفضل ظاهرة «معتمدة». الأمر ده بيلاقيها ويعيد اعتمادها صح:
# الكميات + متوسط التكلفة (شامل المصاريف) + مستحقات المورد + الخزنة + القيود.
#
# الاستخدام (مع django-tenants):
#   python manage.py repair_purchase_edits                  # عرض بس (كل الشركات)
#   python manage.py repair_purchase_edits --apply          # إصلاح
#   python manage.py repair_purchase_edits --schema=gom --apply
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "إصلاح فواتير شراء اتعدّلت وبضاعتها ماترجعتش للمخزن"

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None, help="شركة واحدة (الافتراضي: كل الشركات)")
        parser.add_argument("--apply", action="store_true", help="نفّذ الإصلاح (من غيره عرض بس)")

    def _run(self, apply):
        from inventory.services.purchase_repair import broken_purchase_edits, repair_purchase
        rows = broken_purchase_edits()
        for r in rows:
            inv = r['invoice']
            line = (f"  فاتورة #{inv.id} — {inv.vendor.name} — {inv.branch.name}: "
                    f"المفروض {r['expected']} قطعة، الموجود {r['actual']} (ناقص {r['missing']})")
            if apply:
                repair_purchase(inv)
                self.stdout.write(self.style.SUCCESS("✅" + line))
            else:
                self.stdout.write("🟡" + line)
        if not rows:
            self.stdout.write("  ✓ مفيش فواتير محتاجة إصلاح")
        return len(rows)

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, get_tenant_model, schema_context
        apply, schema = options["apply"], options["schema"]
        model = get_tenant_model()
        tenants = getattr(model, 'all_objects', model.objects).exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
        total = 0
        for t in tenants.order_by("schema_name"):
            self.stdout.write(f"🏢 {t.schema_name} ({t.name})")
            with schema_context(t.schema_name):
                try:
                    total += self._run(apply)
                except Exception as exc:  # noqa: BLE001 — شركة واحدة ماتوقّفش الباقي
                    self.stderr.write(f"  ❌ {exc}")
        verb = "اتصلحت" if apply else "محتاجة إصلاح (شغّل بـ --apply)"
        self.stdout.write(self.style.SUCCESS(f"\nالإجمالي: {total} فاتورة {verb}"))
