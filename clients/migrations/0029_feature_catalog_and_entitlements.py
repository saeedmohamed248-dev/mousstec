"""
Phase 1 (schema) — Feature catalog table + Plan.entitlements JSONField
=======================================================================
Additive فقط: جدول جديد + field جديد. مفيش تعديل لأي عمود قديم.
الـ seed data في migration 0030 منفصلة عشان نقدر نـ rollback الـ seed
لوحدها لو احتجنا.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0028_phase0a_backfill_tenant_subscriptions'),
    ]

    operations = [
        # ── Feature catalog ──────────────────────────────────────────
        migrations.CreateModel(
            name='Feature',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.SlugField(
                    max_length=60, unique=True, verbose_name='الكود البرمجي',
                    help_text='معرف فريد بـ snake_case — يتـ reference من Plan.entitlements',
                )),
                ('name_ar', models.CharField(max_length=120, verbose_name='الاسم بالعربية')),
                ('name_en', models.CharField(max_length=120, verbose_name='الاسم بالإنجليزية')),
                ('description', models.TextField(blank=True, verbose_name='الوصف')),
                ('category', models.CharField(
                    max_length=20, verbose_name='التصنيف',
                    choices=[
                        ('core',         '🏛️ Core — أساسية'),
                        ('workshop',     '🚗 Workshop — مراكز صيانة'),
                        ('printing',     '🎨 Printing — مطابع'),
                        ('marketplace',  '🛒 Marketplace — أسواق'),
                        ('analytics',    '📊 Analytics — تقارير'),
                        ('integrations', '🔌 Integrations — تكاملات'),
                        ('support',      '🛟 Support — دعم'),
                    ],
                )),
                ('is_quantitative', models.BooleanField(
                    default=False, verbose_name='له حد رقمي؟',
                    help_text='True لو الـ feature لها monthly_limit أو quantitative cap',
                )),
                ('unit_label_ar', models.CharField(
                    max_length=40, blank=True, verbose_name='وحدة القياس',
                    help_text="مثلاً: 'تصميم/شهر' أو 'رسالة/شهر' — للعرض في الـ UI",
                )),
                ('is_active', models.BooleanField(default=True, verbose_name='مفعّل')),
                ('sort_order', models.IntegerField(default=0)),
            ],
            options={
                'verbose_name': 'ميزة',
                'verbose_name_plural': '💎 Feature Catalog — مرجع المزايا',
                'ordering': ['category', 'sort_order', 'code'],
            },
        ),
        migrations.AddIndex(
            model_name='feature',
            index=models.Index(fields=['category', 'is_active'], name='clients_fea_categor_6c0a85_idx'),
        ),

        # ── Plan.entitlements ────────────────────────────────────────
        migrations.AddField(
            model_name='plan',
            name='entitlements',
            field=models.JSONField(
                default=dict, blank=True,
                verbose_name='الصلاحيات (entitlements)',
                help_text='dict من feature_code → {enabled: bool, monthly_limit?: int}',
            ),
        ),

        # Cosmetic: update legacy features field verbose_name
        # (في Phase 0 الـ user-facing display اتعدل من "المميزات" لـ "المميزات (عرض)")
        migrations.AlterField(
            model_name='plan',
            name='features',
            field=models.JSONField(blank=True, default=list, verbose_name='المميزات (عرض)'),
        ),
    ]
