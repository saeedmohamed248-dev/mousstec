"""🧰 لوحة المطابع — المساعدات والـ mixins المشتركة (PrintSecureAdmin وغيره)."""

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils.translation import gettext
from erp_core.localization import current_tenant_symbol as _cur_sym
from django.db import connection
from django import forms
from django.core.exceptions import ValidationError

from ..models import (
    PrintTreasury, PrintTransaction,
)


def _money(value):
    return format_html('<b>{}</b> {}', f"{float(value or 0):,.2f}", _cur_sym())


class _CreatedByFormsetMixin:
    """Stamp created_by on inline rows and show model ValidationErrors as messages."""

    def save_formset(self, request, form, formset, change):
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            try:
                obj.delete()
            except ValidationError as exc:
                messages.error(request, '؛ '.join(exc.messages))
        for inst in instances:
            if hasattr(inst, 'created_by_id') and not inst.created_by_id:
                inst.created_by = request.user
            try:
                inst.save()
            except ValidationError as exc:
                messages.error(request, '؛ '.join(exc.messages))
        formset.save_m2m()


class PrintTransactionForm(forms.ModelForm):
    """
    🛡️ Refuse an 'out' movement bigger than the treasury balance *as a form
    error*. The model still guards it, but there it's a raw exception that
    turned the admin page into a 500.
    """
    class Meta:
        model = PrintTransaction
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # الحوافظ الافتراضية (شيكات / خصم من المنبع) بتتحرك من شاشاتها بس.
        if 'treasury' in self.fields:
            qs = PrintTreasury.objects.filter(kind__in=PrintTreasury.MONEY_KINDS)
            if self.instance.pk and self.instance.treasury_id:
                qs = qs | PrintTreasury.objects.filter(pk=self.instance.treasury_id)
            self.fields['treasury'].queryset = qs.distinct()

    def clean(self):
        data = super().clean()
        # الشاشات البسيطة (?mode=) مافيهاش خانة النوع — بيتحدد من الشاشة نفسها
        kind = data.get('transaction_type') or self.instance.transaction_type
        treasury, amount = data.get('treasury'), data.get('amount')
        if not (treasury and kind and amount) or kind != 'out':
            return data
        available = treasury.balance
        inst = self.instance
        if inst.pk and inst.treasury_id == treasury.pk:
            # Editing: the row's current effect is already inside the balance.
            available += inst.amount if inst.transaction_type == 'out' else -inst.amount
        if amount > available:
            raise ValidationError(gettext("رصيد الخزنة «%(name)s» لا يكفي: المتاح %(available)s.")
                                  % {'name': treasury.name, 'available': f"{available:,.2f}"})
        return data


class PrintSecureAdmin(admin.ModelAdmin):
    """حماية: حظر الوصول من الـ public schema لجداول الطباعة"""
    def has_module_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_view_permission(request, obj)

    # 🛡️ [FIX]: Block add/change/delete from public schema too
    def has_add_permission(self, request):
        if connection.schema_name == 'public':
            return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if connection.schema_name == 'public':
            return False
        return super().has_delete_permission(request, obj)


class _SafeSaveDeleteMixin:
    """أخطاء الموديل (فترة مقفولة، رصيد مش كفاية…) تظهر رسالة بدل صفحة 500."""

    def save_model(self, request, obj, form, change):
        if hasattr(obj, 'created_by_id') and not obj.created_by_id:
            obj.created_by = request.user
        try:
            super().save_model(request, obj, form, change)
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))

    def delete_model(self, request, obj):
        try:
            obj.delete()
        except ValidationError as exc:
            messages.error(request, '؛ '.join(exc.messages))

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


def _amount_html(value, color='inherit'):
    return format_html('<b style="color:{};">{} {}</b>', color, f"{float(value or 0):,.2f}", _cur_sym())
