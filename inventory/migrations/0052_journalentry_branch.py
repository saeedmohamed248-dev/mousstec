import django.db.models.deletion
from django.db import migrations, models


def backfill_branch(apps, schema_editor):
    """يملأ فرع القيود القديمة من مصدرها (فاتورة بيع/شراء أو خزنة الحركة)."""
    JournalEntry = apps.get_model('inventory', 'JournalEntry')
    to_update = []
    qs = JournalEntry.objects.select_related(
        'sale_invoice', 'purchase_invoice',
        'financial_transaction', 'financial_transaction__treasury',
    ).filter(branch__isnull=True)
    for je in qs.iterator(chunk_size=1000):
        branch_id = None
        if je.sale_invoice_id:
            branch_id = je.sale_invoice.branch_id
        elif je.purchase_invoice_id:
            branch_id = je.purchase_invoice.branch_id
        elif je.financial_transaction_id and je.financial_transaction.treasury_id:
            branch_id = je.financial_transaction.treasury.branch_id
        if branch_id:
            je.branch_id = branch_id
            to_update.append(je)
        if len(to_update) >= 1000:
            JournalEntry.objects.bulk_update(to_update, ['branch'])
            to_update = []
    if to_update:
        JournalEntry.objects.bulk_update(to_update, ['branch'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0051_financialtransaction_equity_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="journalentry",
            name="branch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="journal_entries",
                to="inventory.branch",
                verbose_name="الفرع",
            ),
        ),
        migrations.RunPython(backfill_branch, noop_reverse),
    ]
