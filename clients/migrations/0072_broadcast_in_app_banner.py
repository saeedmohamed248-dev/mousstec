from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('clients', '0071_broadcastcampaign'),
    ]

    operations = [
        migrations.AddField(
            model_name='broadcastcampaign',
            name='send_email',
            field=models.BooleanField(default=True, help_text='إرسال نسخة بالبريد الإلكتروني'),
        ),
        migrations.AddField(
            model_name='broadcastcampaign',
            name='show_in_app',
            field=models.BooleanField(db_index=True, default=False, help_text='عرض كبانر داخل لوحة كل شركة'),
        ),
        migrations.AddField(
            model_name='broadcastcampaign',
            name='in_app_starts_at',
            field=models.DateTimeField(blank=True, null=True, help_text='بداية ظهور البانر (افتراضي: وقت الإنشاء)'),
        ),
        migrations.AddField(
            model_name='broadcastcampaign',
            name='in_app_ends_at',
            field=models.DateTimeField(blank=True, null=True, help_text='نهاية ظهور البانر (افتراضي: 14 يوم)'),
        ),
        migrations.AddField(
            model_name='broadcastcampaign',
            name='in_app_severity',
            field=models.CharField(
                choices=[('info', 'معلومة'), ('success', 'نجاح'), ('warning', 'تحذير'), ('critical', 'حرج')],
                default='info',
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name='BroadcastDismissal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('dismissed_at', models.DateTimeField(auto_now_add=True)),
                ('campaign', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dismissals',
                    to='clients.broadcastcampaign',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'إخفاء بانر',
                'verbose_name_plural': 'إخفاءات البانرات',
                'unique_together': {('campaign', 'user')},
                'indexes': [models.Index(fields=['user', 'campaign'], name='clients_bro_user_id_campaign_idx')],
            },
        ),
    ]
