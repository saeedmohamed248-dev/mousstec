from django.db import migrations, models
import django.utils.translation


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0007_update_plan_choice_labels'),
    ]

    operations = [
        migrations.AlterField(
            model_name='client',
            name='plan',
            field=models.CharField(
                choices=[
                    ('silver', django.utils.translation.gettext_lazy('باقة سيلفر — لمراكز الصيانة وتجار قطع الغيار')),
                    ('gold', django.utils.translation.gettext_lazy('باقة جولد — لمراكز الصيانة وتجار قطع الغيار الشامل')),
                    ('empire', django.utils.translation.gettext_lazy('باقة Empire — لتجار القطع والشركات الكبيرة')),
                ],
                default='gold',
                max_length=20,
                verbose_name=django.utils.translation.gettext_lazy('الباقة'),
            ),
        ),
    ]
