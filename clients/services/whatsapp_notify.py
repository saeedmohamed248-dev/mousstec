"""
📱 رسايل واتساب من رقم المنصة (WhatsApp Cloud API) — تذكيرات التجديد وأي إشعار للعميل
====================================================================================
🐛 [FIX]: تذكير التجديد بالواتساب كان «stub» بيكتب في اللوج بس — فمن غير SMTP العميل
ماكانش بيوصله أي تذكير قبل ما اشتراكه يقف.

بيستخدم نفس مفاتيح الـ OTP (WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID).
⚠️ Meta مابتسمحش برسالة من المنصة للعميل برّه نافذة الـ 24 ساعة إلا بـ «قالب» متوافَق عليه —
فلو WHATSAPP_RENEWAL_TEMPLATE متظبط بنبعت القالب، وإلا رسالة نصية (بتوصل بس لو العميل
كلّمنا آخر 24 ساعة).
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

logger = logging.getLogger('mouss_tec_core')

_COUNTRY_CODE = {'EG': '20', 'AE': '971', 'SA': '966'}


def normalize_phone(phone: str, country: str = 'EG') -> str | None:
    """رقم بالصيغة الدولية من غير + (اللي Meta عايزاها): 01012345678 ⇒ 201012345678."""
    digits = re.sub(r'\D', '', phone or '')
    if not digits:
        return None
    if digits.startswith('00'):
        digits = digits[2:]
    cc = _COUNTRY_CODE.get((country or 'EG').upper(), '20')
    if digits.startswith('0'):
        digits = cc + digits[1:]
    elif len(digits) <= 10 and not digits.startswith(cc):
        digits = cc + digits
    return digits if 10 <= len(digits) <= 15 else None


def send_platform_whatsapp(phone: str, *, country: str = 'EG', template: str = '',
                           params=(), text: str = '', lang: str = 'ar') -> bool:
    """يبعت من رقم المنصة. True لو Meta قبلت الرسالة."""
    token = getattr(settings, 'WHATSAPP_ACCESS_TOKEN', '')
    phone_id = getattr(settings, 'WHATSAPP_PHONE_NUMBER_ID', '')
    to = normalize_phone(phone, country)
    if not (token and phone_id and to):
        logger.info("[WHATSAPP] not sent (%s)", 'no credentials' if to else 'bad phone')
        return False
    if template:
        payload = {
            'messaging_product': 'whatsapp', 'to': to, 'type': 'template',
            'template': {
                'name': template, 'language': {'code': lang},
                'components': [{'type': 'body', 'parameters': [
                    {'type': 'text', 'text': str(p)[:1000]} for p in params]}],
            },
        }
    elif text:
        payload = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': to,
                   'type': 'text', 'text': {'preview_url': True, 'body': text[:4096]}}
    else:
        return False
    try:
        import requests
        r = requests.post(f"https://graph.facebook.com/v21.0/{phone_id}/messages", json=payload,
                          headers={'Authorization': f'Bearer {token}'}, timeout=15)
    except Exception as exc:  # noqa: BLE001 — الإشعار مايوقفش المهمة
        logger.warning("[WHATSAPP] send failed to %s***: %s", to[:5], exc)
        return False
    if r.status_code in (200, 201):
        logger.info("[WHATSAPP] sent to %s*** (%s)", to[:5], template or 'text')
        return True
    logger.warning("[WHATSAPP] HTTP %s to %s***: %s", r.status_code, to[:5], r.text[:300])
    return False
