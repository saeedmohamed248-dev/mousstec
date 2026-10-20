"""
بيانات قديمة ← الحقول الجديدة:

* كل حركة خزنة مربوطة بطلب بتاخد عميل الطلب وبند «تحصيل عميل»، والباقي
  بند من الوصف (نفس كلمات تصنيف تقرير الأرباح القديم).
* المهام الموجودة اتعملت قبل البروفات: «من غير بروفة»، والمكتمل منها «خلصت».
"""
from django.db import migrations

# نسخة ثابتة من PrintTransaction._CATEGORY_KEYWORDS وقت المهاجرة.
KEYWORDS = (
    ('advance', ('صرف سلفة', 'سلفة')),
    ('salaries', ('راتب', 'مرتب', 'رواتب', 'بونص', 'حافز', 'salary', 'payroll')),
    ('rent', ('ايجار', 'إيجار', 'rent')),
    ('utilities', ('كهرب', 'مياه', 'انترنت', 'إنترنت', 'electricity', 'water', 'internet')),
    ('materials', ('ورق', 'حبر', 'خام', 'فينيل', 'paper', 'ink', 'material')),
    ('maintenance', ('صيان', 'تصليح', 'maintain', 'repair')),
    ('shipping', ('شحن', 'توصيل', 'مواصلات', 'delivery', 'transport')),
    ('marketing', ('إعلان', 'اعلان', 'دعاية', 'تسويق', 'marketing')),
)


def guess(description, txn_type):
    text = (description or '').lower()
    for key, words in KEYWORDS:
        if any(w in text for w in words):
            if txn_type == 'in' and key != 'advance':
                break
            return key
    return 'other_income' if txn_type == 'in' else 'other_expense'


def forwards(apps, schema_editor):
    PrintTransaction = apps.get_model('printing', 'PrintTransaction')
    PrintJob = apps.get_model('printing', 'PrintJob')
    for txn in PrintTransaction.objects.select_related('order').all().iterator():
        if txn.order_id:
            txn.customer_id = txn.order.customer_id
            txn.category = 'customer'
        else:
            txn.category = guess(txn.description, txn.transaction_type)
        txn.save(update_fields=['customer', 'category'])
    PrintJob.objects.update(proof_waived=True)
    PrintJob.objects.filter(is_complete=True).update(stage='done')


class Migration(migrations.Migration):

    dependencies = [
        ('printing', '0007_purchases_proofs_credit_ledger'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
