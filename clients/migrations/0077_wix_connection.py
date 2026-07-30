from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0076_exchange_rate'),
    ]

    operations = [
        migrations.CreateModel(
            name='WixConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('api_key', models.TextField(verbose_name='Wix API Key')),
                ('site_id', models.CharField(max_length=100, verbose_name='Wix Site ID')),
                ('account_id', models.CharField(blank=True, max_length=100, verbose_name='Wix Account ID')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='نشط')),
                ('sync_products', models.BooleanField(default=True, verbose_name='مزامنة المنتجات (Mousstec→Wix)')),
                ('sync_orders', models.BooleanField(default=True, verbose_name='سحب المبيعات (Wix→Mousstec)')),
                ('default_branch_id', models.IntegerField(blank=True, null=True, verbose_name='فرع تسجيل مبيعات Wix')),
                ('last_product_sync_at', models.DateTimeField(blank=True, null=True)),
                ('last_order_sync_at', models.DateTimeField(blank=True, null=True)),
                ('products_pushed', models.PositiveIntegerField(default=0, verbose_name='منتجات تمت مزامنتها')),
                ('orders_imported', models.PositiveIntegerField(default=0, verbose_name='طلبات تم استيرادها')),
                ('last_error', models.TextField(blank=True, verbose_name='آخر خطأ')),
                ('last_test_ok', models.BooleanField(default=False, verbose_name='آخر اختبار اتصال ناجح')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('client', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='wix_connection',
                    to='clients.client',
                    verbose_name='الشركة',
                )),
            ],
            options={
                'verbose_name': 'ربط Wix',
                'verbose_name_plural': '🔌 روابط Wix',
            },
        ),
    ]
