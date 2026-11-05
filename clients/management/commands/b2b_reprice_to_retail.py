"""🌐 تعديل أسعار الأصناف المنشورة بالفعل في سوق التجار لسعر البيع.

🐛 [FIX]: النشر في السوق كان بياخد سعر الجملة؛ دلوقتي بياخد سعر البيع
(InventoryService.b2b_market_price). الأمر ده بيصلّح الإعلانات اللي اتنشرت قبل التعديل.
آمن يتعاد: الإعلان اللي سعره صح مابيتلمسش.

    python manage.py b2b_reprice_to_retail            # تنفيذ
    python manage.py b2b_reprice_to_retail --dry-run  # عرض بس
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = 'يعدّل سعر كل إعلان في سوق التجار (B2B) لسعر البيع الحالي للصنف.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='اعرض التغييرات من غير ما تحفظها')

    def handle(self, *args, dry_run=False, **opts):
        from clients.models import Client, GlobalB2BMarketplace
        from inventory.models import Product
        from inventory.services.inventory_service import InventoryService

        changed = unchanged = missing = 0
        tenants = Client.objects.exclude(schema_name='public').order_by('name')
        for tenant in tenants:
            with schema_context('public'):
                listings = list(GlobalB2BMarketplace.objects.filter(tenant=tenant))
            if not listings:
                continue
            updates = []
            with schema_context(tenant.schema_name):
                for li in listings:
                    p = Product.objects.filter(part_number=li.part_number, condition=li.condition).first()
                    price = InventoryService.b2b_market_price(p) if p else 0
                    if not price:
                        missing += 1
                        continue
                    if li.wholesale_price == price:
                        unchanged += 1
                        continue
                    updates.append((li.pk, li.part_number, li.wholesale_price, price))
            for pk, pn, old, new in updates:
                self.stdout.write(f"{tenant.name} · {pn}: {old} → {new}")
            if updates and not dry_run:
                with schema_context('public'):
                    for pk, _pn, _old, new in updates:
                        GlobalB2BMarketplace.objects.filter(pk=pk).update(wholesale_price=new)
            changed += len(updates)
        verb = 'هيتعدّل' if dry_run else 'اتعدّل'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {changed} إعلان لسعر البيع · {unchanged} سعرهم صح · {missing} من غير صنف/سعر (اتسابوا)."))
