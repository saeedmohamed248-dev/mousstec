"""🎨 Brand Memory — CustomerBrandProfile.

Allows customers to save their brand identity once (logo, colors, aesthetic,
tone) so every subsequent design auto-inherits the brand defaults. Massively
reduces input effort and ensures brand consistency across the portfolio.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0039_quality_gate_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='CustomerBrandProfile',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('brand_name', models.CharField(max_length=120, verbose_name='اسم البراند')),
                ('brand_name_en', models.CharField(blank=True, max_length=120, verbose_name='الاسم بالإنجليزي (اختياري)')),
                ('tagline', models.CharField(blank=True, max_length=200, help_text='جملة قصيرة بتلخص رسالة البراند', verbose_name='الشعار / السلوجان')),
                ('primary_color', models.CharField(default='#7c3aed', help_text='لون البراند الأساسي — هيظهر في كل تصميم', max_length=9, verbose_name='اللون الرئيسي')),
                ('secondary_color', models.CharField(default='#1e293b', max_length=9, verbose_name='اللون الثانوي')),
                ('accent_color', models.CharField(blank=True, default='', max_length=9, verbose_name='لون التمييز (اختياري)')),
                ('logo_image', models.ImageField(blank=True, null=True, help_text='هيتستخدم كـ reference في كل تصميم تلقائياً', upload_to='brand_profiles/logos/%Y/%m/', verbose_name='اللوجو')),
                ('logo_alt_image', models.ImageField(blank=True, null=True, upload_to='brand_profiles/logos/%Y/%m/', verbose_name='لوجو بديل (لون مختلف / monochrome)')),
                ('industry', models.CharField(choices=[('fashion', 'موضة / ملابس'), ('food', 'مطاعم / طعام'), ('tech', 'تكنولوجيا'), ('beauty', 'تجميل'), ('jewelry', 'مجوهرات'), ('home', 'أثاث / ديكور'), ('education', 'تعليم'), ('healthcare', 'صحة / طب'), ('automotive', 'سيارات'), ('real_estate', 'عقارات'), ('retail', 'تجزئة'), ('services', 'خدمات'), ('events', 'مناسبات'), ('agency', 'وكالة / استشارات'), ('other', 'أخرى')], default='other', max_length=20, verbose_name='المجال')),
                ('aesthetic', models.CharField(choices=[('modern_minimal', 'عصري بسيط'), ('luxury_elegant', 'فاخر أنيق'), ('bold_playful', 'جريء مرح'), ('classic_traditional', 'كلاسيكي تراثي'), ('natural_organic', 'طبيعي عضوي'), ('tech_futuristic', 'تقني مستقبلي'), ('artisan_handcrafted', 'حرفي صناعة يدوية'), ('corporate_professional', 'شركاتي محترف')], default='modern_minimal', max_length=30, verbose_name='الأسلوب البصري')),
                ('tone', models.CharField(choices=[('formal', 'رسمي'), ('casual', 'غير رسمي / صديق'), ('playful', 'مرح / فكاهي'), ('authoritative', 'واثق / مرجعي'), ('warm', 'دافئ / إنساني'), ('luxurious', 'فاخر / حصري')], default='warm', max_length=20, verbose_name='نبرة البراند')),
                ('arabic_font', models.CharField(choices=[('modern_sans', 'Modern Sans-serif'), ('classic_serif', 'Classic Serif'), ('geometric', 'Geometric Sans'), ('elegant_script', 'Elegant Script'), ('bold_display', 'Bold Display'), ('arabic_naskh', 'خط النسخ التقليدي'), ('arabic_kufi', 'خط كوفي عصري'), ('arabic_diwani', 'خط ديواني فاخر'), ('arabic_modern', 'خط عربي عصري')], default='arabic_modern', max_length=30, verbose_name='الخط العربي المفضل')),
                ('english_font', models.CharField(choices=[('modern_sans', 'Modern Sans-serif'), ('classic_serif', 'Classic Serif'), ('geometric', 'Geometric Sans'), ('elegant_script', 'Elegant Script'), ('bold_display', 'Bold Display'), ('arabic_naskh', 'خط النسخ التقليدي'), ('arabic_kufi', 'خط كوفي عصري'), ('arabic_diwani', 'خط ديواني فاخر'), ('arabic_modern', 'خط عربي عصري')], default='modern_sans', max_length=30, verbose_name='الخط الإنجليزي المفضل')),
                ('style_notes', models.TextField(blank=True, help_text="أي تفاصيل عن أسلوب البراند بتحب الـ AI يتبعها — مثلاً 'استخدم زخارف إسلامية' أو 'تجنب الزهور'", max_length=500, verbose_name='ملاحظات أسلوب إضافية')),
                ('is_active', models.BooleanField(db_index=True, default=True, help_text='لو معطّل، التصميمات الجديدة مش هتاخد ملف البراند تلقائياً', verbose_name='نشط — يطبّق تلقائياً')),
                ('auto_inject_logo', models.BooleanField(default=True, verbose_name='ضع اللوجو تلقائياً في كل تصميم')),
                ('auto_inject_colors', models.BooleanField(default=True, verbose_name='استخدم ألوان البراند تلقائياً')),
                ('designs_with_brand', models.PositiveIntegerField(default=0, verbose_name='عدد التصميمات اللي استخدمت ملف البراند')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('customer', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='brand_profile', to='clients.marketplacecustomer', verbose_name='العميل')),
            ],
            options={
                'verbose_name': 'ملف براند العميل',
                'verbose_name_plural': '🎨 ملفات البراند',
            },
        ),
    ]
