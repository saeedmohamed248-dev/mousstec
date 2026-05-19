from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0005_fix_domain_underscores'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='ai_trust_score',
            field=models.IntegerField(default=100, help_text='مؤشر الثقة الديناميكي للتاجر (يُحسب آلياً بواسطة AI)'),
        ),
        migrations.AddField(
            model_name='client',
            name='max_treasuries',
            field=models.IntegerField(default=2, verbose_name='الخزائن المشمولة بالباقة'),
        ),
        migrations.AddField(
            model_name='client',
            name='extra_treasuries_purchased',
            field=models.IntegerField(default=0, verbose_name='خزائن إضافية مشتراة'),
        ),
        migrations.AddField(
            model_name='globalb2bmarketplace',
            name='demand_hits',
            field=models.IntegerField(default=0, help_text='عدد مرات البحث/الطلب على هذه القطعة'),
        ),
        migrations.AddField(
            model_name='globalb2bmarketplace',
            name='last_sold_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, help_text='آخر سعر تم الترسية به'),
        ),
        migrations.AddField(
            model_name='bidoffer',
            name='ai_match_score',
            field=models.DecimalField(decimal_places=2, default=0.00, max_digits=5, help_text='تقييم الـ AI الشامل لهذا العرض'),
        ),
        migrations.AddField(
            model_name='blindbiddingrequest',
            name='ai_recommended_winner',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recommended_for', to='clients.bidoffer', help_text='أفضل عرض رشحه الـ AI'),
        ),
    ]
