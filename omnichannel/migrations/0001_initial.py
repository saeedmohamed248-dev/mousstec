from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('clients', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='TenantChannelConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_subscription_active', models.BooleanField(default=False, help_text='يتحكم في تشغيل/إيقاف الردود الآلية بالكامل لهذا المستأجر.', verbose_name='اشتراك الأتمتة فعّال؟')),
                ('ai_enabled', models.BooleanField(default=True, help_text='أوقفه مؤقتاً لتسليم المحادثات لموظف بشري دون إلغاء الاشتراك.', verbose_name='الرد الآلي بالذكاء الاصطناعي مفعّل؟')),
                ('_meta_access_token', models.TextField(blank=True, db_column='meta_access_token_enc', default='', verbose_name='Meta Access Token (مشفّر)')),
                ('whatsapp_phone_number_id', models.CharField(blank=True, db_index=True, default='', help_text='معرّف رقم واتساب المُستقبِل — يُستخدم لتوجيه الرسائل الواردة.', max_length=64, verbose_name='WhatsApp Phone Number ID')),
                ('whatsapp_business_account_id', models.CharField(blank=True, default='', max_length=64, verbose_name='WhatsApp Business Account ID (WABA)')),
                ('facebook_page_id', models.CharField(blank=True, db_index=True, default='', help_text='معرّف صفحة ماسنجر المُستقبِلة — يُستخدم لتوجيه رسائل ماسنجر.', max_length=64, verbose_name='Facebook Page ID')),
                ('_app_secret', models.TextField(blank=True, db_column='app_secret_enc', default='', help_text='يُستخدم للتحقق من توقيع X-Hub-Signature-256 لكل Webhook.', verbose_name='Meta App Secret (مشفّر)')),
                ('webhook_verify_token', models.CharField(blank=True, default='', help_text='رمز التحقق الذي تُدخله الشركة في إعدادات Webhook داخل تطبيق Meta.', max_length=128, verbose_name='Webhook Verify Token')),
                ('whatsapp_enabled', models.BooleanField(default=True, verbose_name='قناة واتساب مفعّلة؟')),
                ('messenger_enabled', models.BooleanField(default=True, verbose_name='قناة ماسنجر مفعّلة؟')),
                ('business_display_name', models.CharField(blank=True, default='', max_length=120, verbose_name='اسم النشاط كما يظهر للعملاء')),
                ('tone_of_voice', models.CharField(blank=True, default='ودود، محترف، وموجز — يخاطب العميل باللهجة نفسها التي راسل بها.', max_length=255, verbose_name='نبرة الحوار (Tone of Voice)')),
                ('discount_policy', models.TextField(blank=True, default='', help_text='مثال: خصم 5% للطلبات فوق 5000 ج.م — يلتزم بها المساعد حرفياً.', verbose_name='سياسة الخصومات')),
                ('custom_instructions', models.TextField(blank=True, default='', help_text='أي قواعد أخرى للرد (ساعات العمل، مناطق التوصيل، عبارات ممنوعة...).', verbose_name='تعليمات مخصّصة إضافية')),
                ('fallback_message', models.TextField(blank=True, default='شكراً لتواصلك معنا 🙏 سيتم تحويلك لأحد موظفي خدمة العملاء للرد على استفسارك.', help_text='تُرسَل عند تعذّر توليد رد آلي موثوق.', verbose_name='رسالة التحويل لموظف بشري')),
                ('max_reply_chars', models.PositiveIntegerField(default=900, verbose_name='أقصى طول للرد (حرف)')),
                ('llm_provider', models.CharField(choices=[('platform', 'Mouss Tec (Gemini) — مزوّد المنصة'), ('openai', 'OpenAI (مفتاح الشركة)'), ('gemini', 'Google Gemini (مفتاح الشركة)')], default='platform', max_length=16, verbose_name='مزوّد الذكاء الاصطناعي')),
                ('_llm_api_key', models.TextField(blank=True, db_column='llm_api_key_enc', default='', verbose_name='مفتاح LLM (مشفّر)')),
                ('llm_model', models.CharField(blank=True, default='', help_text='مثال: gpt-4o-mini أو gemini-2.0-flash — اتركه فارغاً للافتراضي.', max_length=80, verbose_name='موديل الـ LLM')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='omnichannel_config', to='clients.client', verbose_name='الشركة (المستأجر)')),
            ],
            options={
                'verbose_name': 'إعدادات الأتمتة متعددة القنوات',
                'verbose_name_plural': '💬 إعدادات الأتمتة متعددة القنوات (Omnichannel)',
            },
        ),
        migrations.CreateModel(
            name='ChannelMessageLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(choices=[('whatsapp', 'WhatsApp'), ('messenger', 'Messenger')], max_length=16)),
                ('sender_id', models.CharField(db_index=True, max_length=128)),
                ('inbound_text', models.TextField(blank=True, default='')),
                ('outbound_text', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('received', 'وردت'), ('replied', 'تم الرد'), ('skipped', 'تم التجاوز'), ('failed', 'فشل')], default='received', max_length=16)),
                ('error', models.TextField(blank=True, default='')),
                ('meta_message_id', models.CharField(blank=True, default='', max_length=128)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='omnichannel_logs', to='clients.client')),
            ],
            options={
                'verbose_name': 'سجل رسالة قناة',
                'verbose_name_plural': 'سجل رسائل القنوات',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='channelmessagelog',
            index=models.Index(fields=['tenant', '-created_at'], name='omnichanne_tenant__6e9c8f_idx'),
        ),
    ]
