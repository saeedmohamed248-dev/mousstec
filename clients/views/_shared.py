"""
Private helpers shared across clients.views submodules.

Originally inlined in clients/views.py. Pulled out so each submodule can
import only what it needs without dragging the rest of the legacy module.
Nothing here is part of the public URL surface.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection as db_conn

from clients.models import (
    Client,
    MarketplaceCustomer,
    PlatformEvent,
)

logger = logging.getLogger('mouss_tec_core')


# ---------------------------------------------------------------------
# Auth / authorization
# ---------------------------------------------------------------------
def _is_platform_owner(user):
    """The user is a superuser AND we are currently on the public schema."""
    if not user.is_active or not user.is_superuser:
        return False
    return getattr(db_conn, 'schema_name', 'public') == 'public'


def _marketplace_auth(request):
    """Resolve the marketplace customer from the `mp_session` cookie.

    🐛 [audit FIX]: كان بيـ raise ValidationError 500 لو الـ cookie مش UUID
    صالح (cookie قديم/مكسور/manually-edited). دلوقتي بنتـ catch كل أنواع
    الخطأ المتوقّعة (ValueError + ValidationError + DoesNotExist) ونرجع
    None — اللي بيحوّل الـ user للـ login page بدل ما يشوف 500.
    """
    from django.core.exceptions import ValidationError
    token = request.COOKIES.get('mp_session')
    if not token:
        return None
    try:
        return MarketplaceCustomer.objects.get(
            session_token=token,
            is_verified=True, is_blocked=False, is_deleted=False,
        )
    except (MarketplaceCustomer.DoesNotExist, ValueError, ValidationError):
        return None


# ---------------------------------------------------------------------
# OTP delivery
# ---------------------------------------------------------------------
def _send_otp_via_channel(phone, otp, **kwargs):
    """
    إرسال OTP عبر Twilio SMS، Vonage، WhatsApp، أو Email.
    يتم اختيار البوابة بناءً على settings.OTP_DELIVERY_PROVIDER.
    Provider options: 'twilio', 'vonage', 'whatsapp_meta', 'email', 'console' (default).
    """
    provider = getattr(settings, 'OTP_DELIVERY_PROVIDER', 'console')
    message = f"كود التحقق Mouss Tec: {otp}\nصالح لمدة 10 دقائق."

    if provider == 'twilio':
        try:
            from twilio.rest import Client as TwilioClient
            account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
            auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
            from_number = getattr(settings, 'TWILIO_FROM_NUMBER', '')
            if not all([account_sid, auth_token, from_number]):
                logger.error("[OTP/Twilio] Missing credentials in settings")
                return False
            client = TwilioClient(account_sid, auth_token)
            client.messages.create(body=message, from_=from_number, to=phone)
            logger.info(f"[OTP/Twilio] SMS sent to {phone}")
            return True
        except ImportError:
            logger.error("[OTP/Twilio] twilio package not installed: pip install twilio")
            return False
        except Exception as e:
            logger.error(f"[OTP/Twilio] Failed: {e}")
            return False

    elif provider == 'vonage':
        try:
            import vonage
            api_key = getattr(settings, 'VONAGE_API_KEY', '')
            api_secret = getattr(settings, 'VONAGE_API_SECRET', '')
            sender = getattr(settings, 'VONAGE_SENDER', 'MoussTec')
            if not all([api_key, api_secret]):
                logger.error("[OTP/Vonage] Missing credentials")
                return False
            client = vonage.Client(key=api_key, secret=api_secret)
            sms = vonage.Sms(client)
            sms.send_message({'from': sender, 'to': phone.lstrip('+'), 'text': message})
            logger.info(f"[OTP/Vonage] SMS sent to {phone}")
            return True
        except ImportError:
            logger.error("[OTP/Vonage] vonage package not installed: pip install vonage")
            return False
        except Exception as e:
            logger.error(f"[OTP/Vonage] Failed: {e}")
            return False

    elif provider == 'whatsapp_meta':
        try:
            import requests as _req
            access_token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
            phone_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
            if not all([access_token, phone_id]):
                logger.error("[OTP/WhatsApp] Missing WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID")
                return False
            url = f"https://graph.facebook.com/v21.0/{phone_id}/messages"
            template_name = getattr(settings, 'WHATSAPP_OTP_TEMPLATE', '')
            if template_name:
                payload = {
                    "messaging_product": "whatsapp",
                    "to": phone.lstrip('+'),
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"code": "ar"},
                        "components": [{"type": "body", "parameters": [{"type": "text", "text": str(otp)}]}],
                    },
                }
            else:
                payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": phone.lstrip('+'),
                    "type": "text",
                    "text": {"body": message},
                }
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            r = _req.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                logger.info(f"[OTP/WhatsApp] sent to {phone[:6]}***")
                return True
            logger.error(f"[OTP/WhatsApp] HTTP {r.status_code}: {r.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"[OTP/WhatsApp] Failed: {e}")
            return False

    elif provider == 'email':
        try:
            # 🐛 UTF-8 encoding عشان الـ Arabic subject/body يوصل صح عبر SMTP
            from django.core.mail import EmailMultiAlternatives
            email_addr = kwargs.get('email', '')
            if not email_addr:
                logger.error("[OTP/Email] No email address provided")
                return False
            html = (
                f'<div dir="rtl" style="font-family:Arial,sans-serif;max-width:400px;margin:0 auto;padding:20px;">'
                f'<h2 style="color:#2563eb;">Mouss Tec</h2>'
                f'<p>كود التحقق الخاص بك:</p>'
                f'<div style="background:#f1f5f9;padding:15px;border-radius:8px;text-align:center;margin:15px 0;">'
                f'<span style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#1e293b;">{otp}</span>'
                f'</div>'
                f'<p style="color:#64748b;font-size:13px;">صالح لمدة 10 دقائق. لا تشاركه مع أحد.</p>'
                f'</div>'
            )
            msg = EmailMultiAlternatives(
                subject='Mouss Tec — كود التحقق',
                body=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email_addr],
            )
            msg.encoding = 'utf-8'
            msg.attach_alternative(html, 'text/html')
            msg.send(fail_silently=False)
            logger.info(f"[OTP/Email] sent to {email_addr[:4]}***")
            return True
        except Exception as e:
            logger.error(f"[OTP/Email] Failed: {e}")
            return False

    logger.warning(
        f"[OTP-CONSOLE] phone={phone[:6]}*** — "
        f"اضبط OTP_DELIVERY_PROVIDER في settings (twilio/vonage/whatsapp_meta/email)"
    )
    return False


# ---------------------------------------------------------------------
# Marketplace notifications
# ---------------------------------------------------------------------
def _notify_merchants_of_new_request(svc_request, exclude_tenant=None):
    """Log a PlatformEvent so eligible merchants see the request in their feed."""
    try:
        eligible_merchants = Client.objects.filter(
            industry=svc_request.sector,
            is_active=True,
            status__in=('active', 'trial'),
        ).exclude(schema_name='public')
        if exclude_tenant:
            eligible_merchants = eligible_merchants.exclude(pk=exclude_tenant.pk)

        count = eligible_merchants.count()
        PlatformEvent.objects.create(
            event_type='other',
            tenant_schema='public',
            tenant_name='Marketplace',
            description=f"🛒 طلب جديد: {svc_request.title[:80]} — تم إشعار {count} تاجر",
            metadata={
                'request_code': str(svc_request.request_code),
                'sector': svc_request.sector,
                'urgency': svc_request.urgency,
                'merchants_notified': count,
            },
        )
        logger.info(f"[MARKETPLACE NOTIFY] Notified {count} merchants of request {svc_request.request_code}")
    except Exception as e:
        logger.error(f"[MARKETPLACE NOTIFY] Failed: {e}")


# ---------------------------------------------------------------------
# Landing chatbot fallback
# ---------------------------------------------------------------------
def _landing_bot_local_reply(msg):
    """ردود محلية ذكية للبوت لما Gemini مش متاح"""
    q = msg.lower()

    if any(k in q for k in ['hi', 'hello', 'اهلا', 'أهلا', 'مرحبا', 'سلام', 'ازيك', 'إزيك', 'صباح', 'مساء']):
        return (
            'أهلاً بيك! 👋 أنا مساعد Mouss Tec.\n'
            'أقدر أساعدك تعرف كل حاجة عن النظام:\n'
            '🔧 نظام ورش السيارات وقطع الغيار\n'
            '🎨 نظام المطابع وشركات التصميم\n'
            '💰 الباقات والأسعار\n'
            '🚀 التجربة المجانية 7 أيام بكل المزايا\n\n'
            'اسألني عن أي حاجة!'
        )

    if any(k in q for k in ['سعر', 'أسعار', 'اسعار', 'باقة', 'باقات', 'كام', 'تكلفة', 'price', 'plan', 'فرق']):
        return (
            '💰 باقاتنا مرنة وتناسب كل الأحجام:\n\n'
            '🔧 باقات السيارات:\n'
            '• سيلفر: 780 ج/شهر (فرع + موظف)\n'
            '• جولد: 1,250 ج/شهر (فرعين + 4 موظفين + B2B) ⭐\n'
            '• Empire: 1,800 ج/شهر (غير محدود)\n\n'
            '🎨 باقات الطباعة:\n'
            '• Print Basic: 550 ج/شهر\n'
            '• Print Pro: 880 ج/شهر ⭐\n'
            '• Print Enterprise: 2,000 ج/شهر\n\n'
            '🎁 خصم 9% ربع سنوي | 12.5% نصف سنوي | 25% سنوي\n'
            '🆓 جرّب مجاناً 7 أيام بكل المزايا بدون دفع!'
        )

    if any(k in q for k in ['سيار', 'ورش', 'صيان', 'قطع غيار', 'ميكانيك', 'كرت', 'garage', 'auto', 'car']):
        return (
            '🔧 نظام Mouss Tec للسيارات يشمل:\n\n'
            '• فواتير مبيعات ومشتريات ومرتجعات\n'
            '• مخزون ذكي مع باركود وتنبيه نقص\n'
            '• كروت صيانة: افتح كرت → أضف خدمات وقطع غيار → أغلقه = فاتورة تلقائية\n'
            '• سجل مركبات العملاء وتاريخ الصيانة\n'
            '• سوق B2B: اطلب قطع غيار من تجار تانيين\n'
            '• خزائن ومحاسبة كاملة\n'
            '• تقارير أرباح وخسائر\n\n'
            '🆓 جرّب النظام مجاناً 7 أيام بكل المزايا من صفحة الأسعار!'
        )

    if any(k in q for k in ['طباع', 'مطبع', 'تصميم', 'مصمم', 'print', 'design', 'بوستر', 'كارت', 'تيشرت']):
        return (
            '🎨 نظام Mouss Tec للمطابع يشمل:\n\n'
            '• طلبات طباعة مع مهام مخصصة (تيشرت، كروت، بوسترات...)\n'
            '• إدارة المصممين + سجل أعمال + تقييمات\n'
            '• حاسبة تكلفة CMYK لكل ماكينة\n'
            '• مخزون خامات (ورق، حبر) مع تنبيه نقص\n'
            '• رفع ملفات المشاريع وحفظها\n'
            '• صلاحيات موظفين دقيقة\n'
            '• AI Studio: توليد تصاميم بالذكاء الاصطناعي (إضافة اختيارية)\n'
            '• متجر التصميم AI: باقات تصميم للعملاء (99-369 ج.م) والمصممين (599-3,249 ج.م)\n\n'
            '🆓 جرّب مجاناً 7 أيام بكل المزايا + تصميم AI مجاني!'
        )

    if any(k in q for k in ['مجان', 'تجرب', 'trial', 'free', 'جرب', 'ابدأ', 'اشتراك', 'سجل']):
        return (
            '🚀 التجربة المجانية سهلة جداً:\n\n'
            '1. اذهب لصفحة الأسعار\n'
            '2. اختر الباقة المناسبة (سيارات أو طباعة)\n'
            '3. اضغط "جرّب مجاناً 7 أيام بكل المزايا"\n'
            '4. سجّل بياناتك وابدأ فوراً!\n\n'
            '✅ بدون بطاقة ائتمان\n'
            '✅ كل المميزات متاحة\n'
            '✅ لو عجبك، اشترك من داخل النظام'
        )

    if any(k in q for k in ['دفع', 'فيزا', 'فودافون', 'كاش', 'payment', 'pay', 'تحويل']):
        return (
            '💳 طرق الدفع المتاحة:\n\n'
            '1. فودافون كاش: حوّل المبلغ وابعت الإيصال على واتساب\n'
            '2. فيزا/ماستركارد: دفع فوري آمن عبر Paymob\n\n'
            'اختر الباقة من صفحة الأسعار وهتلاقي كل التفاصيل!'
        )

    if any(k in q for k in ['تواصل', 'رقم', 'تليفون', 'هاتف', 'واتس', 'whatsapp', 'contact', 'دعم', 'مساعد', 'مشكل', 'شكوى', 'إنسان', 'موظف', 'بشري']):
        return (
            '📞 تقدر تتواصل معانا في أي وقت:\n\n'
            '• 📱 اتصال/واتساب: 01094850763\n'
            '• 📨 فورم المساعدة موجود فوق في تبويب "محتاج مساعدة؟" — اكتب مشكلتك والفريق هيرد عليك\n'
            '• 📘 فيسبوك: تابعنا للأخبار والعروض\n\n'
            'فريق الدعم متاح ويرد بسرعة! 💜'
        )

    if any(k in q for k in ['مين', 'إيه هي', 'ايه هي', 'about', 'شركة', 'mouss', 'موس', 'بتعملوا', 'بتعمل ايه']):
        return (
            '🏢 Mouss Tec شركة أتمتة و ERP سحابي مصرية.\n\n'
            'بنوفر منصة أعمال موحّدة (Business OS) لأي مجال — على فلسفة SAP و Oracle و Odoo — لكن بسعر مصري وواجهة عربية ودعم محلي.\n\n'
            '✅ سحابية 24/7 بدون تنصيب\n'
            '✅ تأتمت كل دورة الشغل: مبيعات، مخزون، محاسبة، CRM، تقارير\n'
            '✅ ذكاء اصطناعي مدمج (مستشار يرد من بياناتك)\n'
            '✅ قطاعات جاهزة: السيارات + الطباعة — وقابلة للتخصيص لأي مجال\n'
            '✅ سوق B2B مدمج\n\n'
            'تحب تجرّبها مجاناً 7 أيام بكل المزايا؟ 🚀'
        )

    if any(k in q for k in ['فاتور', 'مبيعات', 'مشتريات', 'مخزون', 'محاسب', 'خزين', 'تقرير']):
        return (
            '📊 النظام يشمل كل ما تحتاجه:\n\n'
            '• فواتير مبيعات ومشتريات ومرتجعات\n'
            '• مخزون مع باركود وجرد وتحويل بين فروع\n'
            '• محاسبة كاملة: قيد مزدوج + أرباح وخسائر\n'
            '• خزائن ومدفوعات مع تحصيل وصرف\n'
            '• تقارير شاملة لكل شيء\n\n'
            'عاوز تعرف تفاصيل أكتر عن حاجة معينة؟'
        )

    return (
        'أقدر أساعدك تعرف أكتر عن:\n\n'
        '🔧 نظام السيارات والورش\n'
        '🎨 نظام المطابع والتصميم\n'
        '💰 الباقات والأسعار\n'
        '🚀 التجربة المجانية\n'
        '💳 طرق الدفع\n\n'
        'اسألني عن أي حاجة من دول! 😊'
    )


# ---------------------------------------------------------------------
# AI design store top-up cards (built from the catalog, not the DB)
# ---------------------------------------------------------------------
def _build_customer_topup_cards():
    """
    🆕 يبني كروت باقات العملاء من الـ catalog (50/100/500) مع كل الحقول
    اللي الـ template محتاجها — بدون لمس DB الباقات القديمة.
    """
    from types import SimpleNamespace
    from erp_core.ai.credit_packages import CUSTOMER_TOPUPS

    accents = ['#10b981', '#ec4899', '#f59e0b']
    icons = ['🥉', '🥈', '🥇']
    cards = []

    base_per_design = float(CUSTOMER_TOPUPS[0]['price']) / CUSTOMER_TOPUPS[0]['designs']

    for idx, pkg in enumerate(CUSTOMER_TOPUPS):
        per_design = float(pkg['price']) / pkg['designs']
        savings = int((base_per_design - per_design) / base_per_design * 100) if base_per_design > 0 else 0
        cards.append(SimpleNamespace(
            slug=pkg['slug'],
            name_ar=pkg['name'],
            designs_count=pkg['designs'],
            price_egp=pkg['price'],
            price_per_design=round(per_design, 2),
            is_featured=(idx == 1),
            accent_color=accents[idx % len(accents)],
            icon_emoji=icons[idx % len(icons)],
            badge_text=pkg.get('badge', ''),
            description_html='',
            free_regenerations_per_design=2,
            allows_whatsapp_delivery=True,
            allows_logo_upload=True,
            allows_watermark=(idx >= 1),
            allows_source_files=(idx == 2),
            get_quality_level_display=lambda i=idx: ['عالية', 'فائقة', 'فائقة Plus'][i] if i < 3 else 'عالية',
            savings_vs_starter=max(savings, 0),
        ))
    return cards
