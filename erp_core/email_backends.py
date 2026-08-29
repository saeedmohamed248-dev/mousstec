"""
📧 Brevo (Sendinblue) HTTP API email backend.

DigitalOcean وأغلب مزوّدي السحابة بيحظروا منافذ SMTP (25/465/587) الصادرة،
فأي إرسال عبر SMTP بيعلّق. الـ backend ده بيبعت عبر Brevo HTTP API على
منفذ 443 (غير محظور) — بيستخدم requests فقط (متوفرة أصلاً)، بلا أي
اعتماديات جديدة.

التفعيل في .env:
    EMAIL_BACKEND=erp_core.email_backends.BrevoAPIEmailBackend
    BREVO_API_KEY=xkeysib-....
    DEFAULT_FROM_EMAIL=verified-sender@yourdomain.com   (لازم يكون Sender موثّق في Brevo)
"""
import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger('mouss_tec_core')

_API_URL = "https://api.brevo.com/v3/smtp/email"


class BrevoAPIEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        api_key = str(getattr(settings, 'BREVO_API_KEY', '') or '').strip().strip('"').strip("'")
        if not api_key:
            if not self.fail_silently:
                raise ValueError("BREVO_API_KEY مفقود في البيئة.")
            logger.warning("⚠️ [EMAIL] BREVO_API_KEY missing — email skipped.")
            return 0
        if api_key.startswith('xsmtpsib-'):
            msg = ("BREVO_API_KEY ده مفتاح SMTP (xsmtpsib-) وده مش بينفع مع الـ HTTP API. "
                   "اعمل مفتاح API من Brevo: SMTP & API ▸ API Keys ▸ Generate a new API key "
                   "(لازم يبدأ بـ xkeysib-).")
            logger.warning("🔴 [EMAIL] %s", msg)
            if not self.fail_silently:
                raise ValueError(msg)
            return 0

        default_from = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or ''
        headers = {
            "api-key": api_key,
            "accept": "application/json",
            "content-type": "application/json",
        }
        sent = 0
        for msg in email_messages:
            recipients = list(msg.to or [])
            if not recipients:
                continue
            payload = {
                "sender": {"email": (msg.from_email or default_from)},
                "to": [{"email": a} for a in recipients],
                "subject": msg.subject or "",
                "textContent": msg.body or " ",
            }
            if msg.cc:
                payload["cc"] = [{"email": a} for a in msg.cc]
            if msg.bcc:
                payload["bcc"] = [{"email": a} for a in msg.bcc]
            # نسخة HTML لو موجودة (EmailMultiAlternatives)
            for content, mimetype in getattr(msg, 'alternatives', []) or []:
                if mimetype == 'text/html':
                    payload["htmlContent"] = content
                    break
            try:
                r = requests.post(_API_URL, json=payload, headers=headers, timeout=20)
                if r.status_code < 300:
                    sent += 1
                else:
                    logger.warning("🔴 [EMAIL] Brevo API %s: %s", r.status_code, r.text[:250])
                    if not self.fail_silently:
                        hint = ""
                        if r.status_code == 401:
                            hint = ("  ← المفتاح مرفوض من Brevo. شغّل: bash deploy/check_brevo.sh "
                                    "للتشخيص. الأغلب إنه مش مفتاح API (xkeysib-) أو متلغي.")
                        raise RuntimeError(f"Brevo API error {r.status_code}: {r.text[:250]}{hint}")
            except requests.RequestException as exc:
                logger.warning("🔴 [EMAIL] Brevo request failed: %s", exc)
                if not self.fail_silently:
                    raise
        return sent
