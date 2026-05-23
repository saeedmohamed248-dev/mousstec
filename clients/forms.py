import re
import logging
from django import forms
from django.core.exceptions import ValidationError

# تهيئة رادار المراقبة
logger = logging.getLogger('mouss_tec_core')

class TenantSignupForm(forms.Form):
    """
    استمارة تسجيل المؤسسات السحابية (Enterprise Onboarding Form)
    محصنة ضد الروبوتات، موحدة الحقول، ومطابقة لشروط PostgreSQL الصارمة.
    """
    # ==========================================
    # 1. بيانات المؤسسة والنشاط التجاري
    # ==========================================
    company_name = forms.CharField(
        max_length=100,
        label="اسم المركز أو الشركة",
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: شركة النور'})
    )

    industry = forms.ChoiceField(
        choices=[
            ('automotive', '🚗 سيارات — صيانة وقطع غيار'),
            ('printing', '🎨 طباعة وتصميم جرافيك'),
        ],
        label="القطاع",
        widget=forms.Select(attrs={'class': 'form-control', 'id': 'id_industry'})
    )

    business_type = forms.ChoiceField(
        choices=[
            # سيارات
            ('service_center', '🛠️ مركز صيانة متكامل'),
            ('parts_dealer', '📦 تاجر قطع غيار (جملة/تجزئة)'),
            ('scrap_importer', '🪚 مستورد تقطيع وأنصاف'),
            ('both', '👑 توكيل شامل (صيانة وقطع غيار)'),
            # طباعة
            ('print_shop', '🖨️ مطبعة (رقمية وأوفست)'),
            ('design_studio', '🎨 استوديو تصميم جرافيك'),
            ('print_and_design', '🖨️🎨 مطبعة + تصميم (شامل)'),
        ],
        label="نوع النشاط",
        widget=forms.Select(attrs={'class': 'form-control', 'id': 'id_business_type'})
    )
    
    subdomain = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'mouss-auto', 'dir': 'ltr'}),
        label="رابط النظام"
    )

    # ==========================================
    # 2. بيانات المدير المسؤول (Tenant Admin)
    # ==========================================
    full_name = forms.CharField(
        max_length=150, 
        label="الاسم بالكامل", 
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: سعيد محمد'})
    )
    
    email = forms.EmailField(
        label="البريد الإلكتروني للإدارة", 
        widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'name@example.com'})
    )
    
    phone = forms.CharField(
        max_length=20, 
        label="رقم الهاتف (واتساب)", 
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': '010XXXXXXXX'})
    )
    
    password = forms.CharField(
        min_length=8, 
        label="كلمة المرور للنظام", 
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '••••••••'})
    )

    # ==========================================
    # 3. الدرع الأمني (Anti-Bot Honeypot)
    # ==========================================
    # 🚀 ابتكار: حقل مخفي لاصطياد روبوتات الـ Spam دون إزعاج المستخدم الحقيقي
    website_url_honeypot = forms.CharField(
        required=False, 
        widget=forms.HiddenInput(), 
        label="اترك هذا الحقل فارغاً"
    )

    # ==========================================
    # 🛡️ محركات الفحص والتطهير (Validation & Normalization)
    # ==========================================
    
    def clean_website_url_honeypot(self):
        """مصيدة الروبوتات: إذا تم ملء هذا الحقل، فالمرسل ليس بشراً."""
        honeypot = self.cleaned_data.get('website_url_honeypot')
        if honeypot:
            logger.warning("🚨 [SECURITY]: Bot detected and blocked during SaaS signup.")
            raise ValidationError("تم اكتشاف نشاط غير طبيعي. تم رفض التسجيل.")
        return honeypot

    def clean_email(self):
        """تطهير البريد الإلكتروني لمنع ازدواجية الحسابات بسبب الحروف الكبيرة"""
        return self.cleaned_data.get('email', '').strip().lower()

    def clean_phone(self):
        """تجريد رقم الهاتف من المسافات والشرطات لدعم الـ WhatsApp APIs لاحقاً"""
        phone = self.cleaned_data.get('phone', '')
        phone_clean = re.sub(r'[^\d+]', '', phone)
        if len(phone_clean) < 10:
            raise ValidationError("🚫 رقم الهاتف غير صالح. يرجى إدخال رقم صحيح.")
        return phone_clean

    def clean_password(self):
        """درع حماية أولي لكلمة المرور"""
        password = self.cleaned_data.get('password')
        if password and password.isdigit() and len(password) < 8:
            raise ValidationError("🚫 كلمة المرور ضعيفة جداً. يرجى دمج حروف وأرقام.")
        return password

    def clean_subdomain(self):
        """
        المحرك الصارم لفحص الروابط والنطاقات.
        يضمن التوافق التام مع Postgres ويمنع حجز الكلمات السيادية.
        """
        subdomain = self.cleaned_data.get('subdomain', '')
        
        if not subdomain:
            return subdomain # التوليد التلقائي مبرمج ليعمل في الـ views.py في حال تُرك فارغاً
            
        subdomain = subdomain.strip().lower()

        # 🚀 ابتكار الحماية لـ PostgreSQL: يجب أن يبدأ بحرف إنجليزي ويُمنع الأرقام في البداية!
        if not re.match(r'^[a-z][a-z0-9-]{2,49}$', subdomain):
            raise ValidationError("🚫 الرابط يجب أن يبدأ بحرف إنجليزي، ويحتوي على حروف وأرقام أو شرطة (-) فقط.")
            
        if '--' in subdomain:
            raise ValidationError("🚫 لا يمكن أن يحتوي الرابط على شرطتين متتاليتين.")
        
        # حماية كلمات النظام السيادية
        protected_terms = [
            'public', 'admin', 'www', 'api', 'secure', 'system', 'localhost', 
            'mousstec', 'support', 'billing', 'mail', 'ftp', 'test', 'demo', 'fixit'
        ]
        if subdomain in protected_terms:
            raise ValidationError("🚫 هذا الرابط محجوز ككلمة سيادية للنظام، برجاء اختيار اسم آخر.")
            
        # فحص قاعدة البيانات لمنع التكرار (Zero-Collision)
        from clients.models import Client
        safe_schema_name = subdomain.replace('-', '_')
        if Client.objects.filter(schema_name=safe_schema_name).exists():
            raise ValidationError("🚫 هذا الرابط محجوز مسبقاً لمؤسسة أخرى، يرجى اختيار اسم فريد.")
            
        return subdomain