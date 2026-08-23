import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import inventory.models.returns


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('inventory', '0027_alter_usermfa_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='PartReturnGuard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('part_number', models.CharField(db_index=True, max_length=100, verbose_name='Part Number (مطابقة الموقع = SKU)')),
                ('source', models.CharField(choices=[('internal', 'المحل (داخلي)'), ('website', 'موقع FixIt')], default='internal', max_length=20, verbose_name='مصدر الحارس')),
                ('external_ref', models.CharField(blank=True, db_index=True, max_length=120, verbose_name='مرجع خارجي (رقم طلب الموقع)')),
                ('public_token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='رمز عام آمن للموقع')),
                ('status', models.CharField(choices=[('awaiting_dispatch', 'بانتظار تصوير الصرف'), ('fingerprinted', 'تم تثبيت بصمة الصرف'), ('return_requested', 'طلب إرجاع قيد الفحص'), ('return_approved', 'الإرجاع مقبول'), ('return_rejected', 'الإرجاع مرفوض'), ('needs_human', 'يحتاج مراجعة بشرية')], db_index=True, default='awaiting_dispatch', max_length=20, verbose_name='الحالة')),
                ('dispatch_fingerprint', models.JSONField(blank=True, null=True, verbose_name='بصمة القطعة وقت الصرف')),
                ('dispatch_analyzed_at', models.DateTimeField(blank=True, null=True, verbose_name='وقت تحليل بصمة الصرف')),
                ('return_fingerprint', models.JSONField(blank=True, null=True, verbose_name='بصمة القطعة وقت الإرجاع')),
                ('verdict', models.JSONField(blank=True, null=True, verbose_name='الحكم (returnable / match_score / reasons)')),
                ('verdict_at', models.DateTimeField(blank=True, null=True, verbose_name='وقت إصدار الحكم')),
                ('notes', models.TextField(blank=True, verbose_name='ملاحظات')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('customer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='return_guards', to='inventory.customer', verbose_name='العميل')),
                ('invoice_item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='return_guards', to='inventory.saleinvoiceitem', verbose_name='سطر الفاتورة')),
                ('original_invoice', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='return_guards', to='inventory.saleinvoice', verbose_name='الفاتورة الأصلية')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='return_guards', to='inventory.product', verbose_name='القطعة')),
            ],
            options={
                'verbose_name': 'حارس مرتجعات القطعة',
                'verbose_name_plural': 'حرّاس مرتجعات القطع',
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='PartReturnPhoto',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stage', models.CharField(choices=[('dispatch', 'صورة الصرف (المحل)'), ('return', 'صورة الإرجاع (المحل)'), ('customer_pre', 'صورة العميل قبل الشراء'), ('customer_post', 'صورة العميل عند الإرجاع')], max_length=20, verbose_name='المرحلة')),
                ('image', models.ImageField(blank=True, null=True, upload_to=inventory.models.returns._return_photo_upload_path, verbose_name='الصورة')),
                ('image_url_external', models.URLField(blank=True, verbose_name='رابط صورة مستضافة على الموقع')),
                ('sha256', models.CharField(blank=True, db_index=True, max_length=64, verbose_name='بصمة النزاهة (SHA-256)')),
                ('source', models.CharField(choices=[('internal', 'المحل'), ('website', 'موقع FixIt')], default='internal', max_length=20, verbose_name='المصدر')),
                ('uploaded_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='وقت الرفع')),
                ('guard', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='photos', to='inventory.partreturnguard', verbose_name='الحارس')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL, verbose_name='رفعها')),
            ],
            options={
                'verbose_name': 'صورة مرتجع',
                'verbose_name_plural': 'صور المرتجعات',
                'ordering': ('uploaded_at',),
            },
        ),
        migrations.AddIndex(
            model_name='partreturnguard',
            index=models.Index(fields=['part_number', 'external_ref'], name='inventory_p_part_nu_idx'),
        ),
    ]
