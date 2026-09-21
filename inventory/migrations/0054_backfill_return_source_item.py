# ↩️ ربط أسطر المرتجعات القديمة بأسطرها الأصلية.
#
# قبل ميزة المرتجع الجزئي كانت الواجهة بتمنع أي مرتجع تاني لنفس الفاتورة
# (return_invoices.exists())، فالمرتجعات القديمة اتعملت من غير source_item.
# دلوقتي الحد بقى بالكمية المتبقّية لكل سطر، فلو سيبنا الأسطر القديمة فاضية
# هتبان الفواتير المرتجعة بالكامل وكأنها لسه قابلة للإرجاع → مرتجع مكرر
# (مخزون بيتزوّد مرتين وفلوس بترتد مرتين). المهاجرة دي بتملا الربط الناقص
# بمطابقة القطعة داخل الفاتورة الأصلية.
from collections import defaultdict

from django.db import migrations
from django.db.models import Sum


def backfill_source_item(apps, schema_editor):
    SaleInvoice = apps.get_model("inventory", "SaleInvoice")
    SaleInvoiceItem = apps.get_model("inventory", "SaleInvoiceItem")

    original_ids = (
        SaleInvoice.objects
        .filter(is_return=True, original_invoice__isnull=False)
        .values_list("original_invoice_id", flat=True)
        .distinct()
    )

    for original_id in original_ids:
        orig_lines = list(
            SaleInvoiceItem.objects.filter(invoice_id=original_id).order_by("pk")
        )
        if not orig_lines:
            continue

        by_product = defaultdict(list)
        capacity = {}
        for line in orig_lines:
            by_product[line.product_id].append(line)
            capacity[line.pk] = int(line.quantity or 0)

        # اطرح الكميات المربوطة فعلاً (مرتجعات اتعملت بعد الميزة الجديدة).
        attributed = (
            SaleInvoiceItem.objects
            .filter(source_item_id__in=list(capacity.keys()))
            .values("source_item_id")
            .annotate(total=Sum("quantity"))
        )
        for row in attributed:
            capacity[row["source_item_id"]] -= int(row["total"] or 0)

        pending = (
            SaleInvoiceItem.objects
            .filter(
                invoice__is_return=True,
                invoice__original_invoice_id=original_id,
                source_item__isnull=True,
            )
            .order_by("pk")
        )
        for ret_line in pending:
            candidates = by_product.get(ret_line.product_id)
            if not candidates:
                continue
            # فضّل السطر اللي عنده أكبر سعة متبقّية؛ لو مفيش سعة خالص نربطه
            # بأول سطر بنفس القطعة برضه — الأأمن إننا نعتبر الكمية مُرتجعة
            # (المتبقّي بيتقفل على صفر) بدل ما نسمح بمرتجع مكرر.
            best = max(candidates, key=lambda c: capacity.get(c.pk, 0))
            capacity[best.pk] = capacity.get(best.pk, 0) - int(ret_line.quantity or 0)
            ret_line.source_item_id = best.pk
            ret_line.save(update_fields=["source_item"])


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0053_saleinvoiceitem_source_item"),
    ]

    operations = [
        migrations.RunPython(backfill_source_item, migrations.RunPython.noop),
    ]
