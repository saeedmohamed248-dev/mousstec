"""
🖨️ سوق المطابع — Mouss Tec Print Market.

سوق عام (public schema) للمطابع بس: المطبعة بتعمل حساب مجاني بالإيميل، تنزّل منتجاتها،
تقدّم عروض أسعار، وتبعت بروفات (صورة/PDF/فيديو) والعميل يوافق أو يطلب تعديل.
الدفع لـ Mouss Tec بس (إنستاباي / فودافون كاش) — والتواصل من جوه المنصة بس.
"""
from django.apps import AppConfig


class PrintMarketConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'print_market'
    verbose_name = '🖨️ سوق المطابع'
