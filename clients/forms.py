from django import forms
from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
import re

from django import forms

class TenantSignupForm(forms.Form):
    company_name = forms.CharField(max_length=100, label="اسم المركز أو الشركة")
    business_type = forms.ChoiceField(choices=[
        ('service_center', 'مركز صيانة متكامل'),
        ('parts_dealer', 'تاجر قطع غيار (جملة/تجزئة)'),
        ('scrap_importer', 'مستورد قطع غيار (تقطيع)'),
    ], label="نوع النشاط")
    from django import forms

class TenantSignupForm(forms.Form):
    company_name = forms.CharField(max_length=100, label="اسم المركز/الشركة")
    full_name = forms.CharField(max_length=100, label="الاسم بالكامل")
    email = forms.EmailField(label="البريد الإلكتروني للإدارة")
    phone = forms.CharField(max_length=20, label="رقم الهاتف (واتساب)", required=True) # 👈 الحقل الجديد
    password = forms.CharField(widget=forms.PasswordInput(), label="كلمة المرور")
    # 🚀 التعديل هنا: جعل الحقل غير إجباري وتحويله لـ HiddenInput
    subdomain = forms.CharField(required=False, widget=forms.HiddenInput(), label="رابط النظام")
    
    full_name = forms.CharField(max_length=150, label="الاسم بالكامل")
    email = forms.EmailField(label="البريد الإلكتروني")
    password = forms.CharField(widget=forms.PasswordInput(), label="كلمة المرور")    # 👤 بيانات مدير الحساب الرئيسي (Tenant Admin)
    full_name = forms.CharField(max_length=150, label="الاسم بالكامل", widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: سعيد محمد'}))
    email = forms.EmailField(label="البريد الإلكتروني للإدارة", widget=forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'name@example.com'}))
    password = forms.CharField(min_length=8, label="كلمة المرور للنظام", widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '••••••••'}))
def clean_subdomain(self):
    subdomain = self.cleaned_data.get('subdomain')
    
    if not subdomain: # 🚀 سطر الحماية السحري: لو فاضي عدي الفحص تلقائي
        return subdomain
        
    if not re.match(r'^[a-z0-9-]+$', subdomain):
        raise forms.ValidationError("الرابط يجب أن يحتوي على أحرف إنجليزية صغيرة...")
    return subdomain
    def clean_subdomain(self):
        subdomain = self.cleaned_data['subdomain'].strip().lower()
        # التأكد من أن الرابط يحتوي على حروف وأرقام إنجليزية فقط بدون مسافات
        if not re.match(r'^[a-z0-9-]+$', subdomain):
            raise ValidationError("🚫 الرابط يجب أن يحتوي على أحروف إنجليزية صغيرة، أرقام، أو شرطة (-) فقط.")
        
        # التأكد من عدم حجز الكلمات النظامية المحمية
        protected_terms = ['public', 'admin', 'www', 'api', 'secure', 'system', 'localhost']
        if subdomain in protected_terms:
            raise ValidationError("🚫 هذا الرابط محجوز للنظام، برجاء اختيار اسم آخر.")
            
        # فحص قاعدة البيانات لمنع التكرار
        from clients.models import Client
        if Client.objects.filter(schema_name=subdomain.replace('-', '_')).exists():
            raise ValidationError("🚫 هذا الرابط محجوز مسبقاً لمؤسسة أخرى، اختر اسماً فريداً.")
            
        return subdomain