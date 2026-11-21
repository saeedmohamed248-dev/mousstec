"""🎨 Printing serializers — المطابع: الطلبات/الفواتير، المهام (مراحل الإنتاج)، العملاء،
الخامات، عروض الأسعار، البروفات، الماكينات.

⚠️ الفلوس (المدفوع/الرصيد/التكلفة) للقراءة بس — بتتحسب من مصدرها (الدفعات والقيود)،
والكتابة بتعدّي على نفس دوال الويب (printing.quick_invoice) عشان الدفاتر تفضل مظبوطة.
"""
from rest_framework import serializers

from printing.models import (
    MachineProfile, PriceQuotation, PrintCustomer, PrintJob, PrintMaterial, PrintOrder,
    PrintProof, QuotationLine,
)


class PrintCustomerListSerializer(serializers.ModelSerializer):
    class Meta:
        model = PrintCustomer
        fields = ('id', 'name', 'phone', 'whatsapp', 'company')


class PrintCustomerDetailSerializer(serializers.ModelSerializer):
    balance = serializers.SerializerMethodField()
    open_orders = serializers.SerializerMethodField()

    class Meta:
        model = PrintCustomer
        fields = ('id', 'name', 'phone', 'whatsapp', 'email', 'company', 'notes',
                  'credit_limit', 'balance', 'open_orders', 'created_at')

    def get_balance(self, obj):
        return str(obj.balance)

    def get_open_orders(self, obj):
        return obj.printorder_set.exclude(status__in=('delivered', 'cancelled')).count()


class PrintCustomerWriteSerializer(serializers.ModelSerializer):
    """الرصيد الافتتاحي وحد الائتمان بيتعدّلوا من الويب بس (بيأثّروا على الدفاتر/الائتمان)."""

    class Meta:
        model = PrintCustomer
        fields = ('id', 'name', 'phone', 'whatsapp', 'email', 'company', 'notes')

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('اكتب اسم العميل.')
        return value


class PrintProofSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    file_url = serializers.SerializerMethodField()
    share_url = serializers.SerializerMethodField()

    class Meta:
        model = PrintProof
        fields = ('id', 'job', 'version', 'status', 'status_display', 'notes', 'customer_comment',
                  'file_url', 'share_url', 'created_at', 'responded_at')

    def _abs(self, path):
        request = self.context.get('request')
        return request.build_absolute_uri(path) if request else path

    def get_file_url(self, obj):
        return self._abs(obj.file.url) if obj.file else None

    def get_share_url(self, obj):
        # نفس رابط الويب اللي العميل بيوافق منه (printing.views.production.proof_share_url)
        return self._abs(f'/printing/proof/{obj.share_token}/')


class PrintJobSerializer(serializers.ModelSerializer):
    stage_display = serializers.CharField(source='get_stage_display', read_only=True)
    order_number = serializers.CharField(source='order.order_number', read_only=True)
    order_id = serializers.IntegerField(source='order.id', read_only=True)
    customer_name = serializers.CharField(source='order.customer.name', read_only=True)
    date_due = serializers.DateTimeField(source='order.date_due', read_only=True)
    machine_name = serializers.CharField(source='machine.name', read_only=True, default=None)
    designer_name = serializers.SerializerMethodField()
    next_stage = serializers.SerializerMethodField()
    blocked = serializers.SerializerMethodField()

    class Meta:
        model = PrintJob
        fields = ('id', 'order_id', 'order_number', 'customer_name', 'description', 'quantity',
                  'copies', 'unit_price', 'total_price', 'paper_size', 'stage', 'stage_display',
                  'next_stage', 'blocked', 'proof_waived', 'is_complete', 'machine_name',
                  'designer_name', 'date_due', 'stage_changed_at', 'notes')

    def get_designer_name(self, obj):
        if not obj.designer_id:
            return None
        u = obj.designer.user
        return u.get_full_name() or u.username

    def _next(self, obj):
        nxt = obj.next_stage
        if nxt == 'proof' and obj.proof_waived:
            nxt = 'printing'
        return nxt

    def get_next_stage(self, obj):
        return self._next(obj)

    def get_blocked(self, obj):
        """⏳ زي لوحة الإنتاج في الويب: الطباعة مقفولة لحد موافقة العميل على البروفة."""
        nxt = self._next(obj)
        return bool(nxt) and nxt in PrintJob.PRODUCTION_STAGES and not obj.can_go_to_production


class PrintOrderListSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    net_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    remaining = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = PrintOrder
        fields = ('id', 'order_number', 'customer', 'customer_name', 'status', 'status_display',
                  'net_total', 'paid_amount', 'remaining', 'date_created', 'date_due')


class PrintOrderDetailSerializer(PrintOrderListSerializer):
    jobs = PrintJobSerializer(many=True, read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    due_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    allowed_actions = serializers.SerializerMethodField()

    class Meta(PrintOrderListSerializer.Meta):
        fields = PrintOrderListSerializer.Meta.fields + (
            'customer_phone', 'branch', 'total_amount', 'discount', 'tax_amount', 'credited_amount',
            'due_amount', 'date_delivered', 'notes', 'jobs', 'allowed_actions')

    def get_allowed_actions(self, obj):
        """الإجراءات اللي التطبيق يعرض زرايرها (السيرفر بيتأكد تاني وقت التنفيذ)."""
        return allowed_order_actions(obj)


def allowed_order_actions(order):
    actions = []
    if order.status == 'draft':
        actions.append('confirm')
    if order.status in ('confirmed', 'in_progress', 'ready'):
        actions.append('deliver')
    if order.status in ('draft', 'confirmed', 'in_progress', 'ready') and not order.paid_amount:
        actions.append('cancel')
    return actions


class PrintOrderLineSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=300)
    quantity = serializers.IntegerField(min_value=1, default=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)


class PrintOrderCreateSerializer(serializers.Serializer):
    """⚡ فاتورة/طلب من الموبايل = نفس «الفاتورة السريعة» في الويب (printing.quick_invoice)."""
    customer = serializers.IntegerField(required=False, allow_null=True)
    customer_name = serializers.CharField(required=False, allow_blank=True, default='')
    customer_phone = serializers.CharField(required=False, allow_blank=True, default='')
    lines = PrintOrderLineSerializer(many=True)
    discount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    delivered = serializers.BooleanField(default=False)
    paid = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    treasury = serializers.IntegerField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError('ضيف بند واحد على الأقل.')
        return value


class PrintOrderActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=('confirm', 'deliver', 'cancel'))


class PrintMaterialSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = PrintMaterial
        fields = ('id', 'name', 'category', 'category_display', 'sku', 'unit', 'branch_name',
                  'quantity', 'min_stock', 'is_low_stock')


class QuotationLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuotationLine
        fields = ('id', 'description', 'quantity', 'unit_price', 'line_total')


class PriceQuotationSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    customer_display = serializers.CharField(read_only=True)
    lines = QuotationLineSerializer(many=True, read_only=True)
    share_url = serializers.SerializerMethodField()

    class Meta:
        model = PriceQuotation
        fields = ('id', 'quote_number', 'customer', 'customer_display', 'title', 'status',
                  'status_display', 'subtotal', 'discount', 'tax_percent', 'total', 'valid_until',
                  'notes', 'lines', 'share_url', 'created_at')

    def get_share_url(self, obj):
        request = self.context.get('request')
        path = f'/printing/quotation/view/{obj.share_token}/'
        return request.build_absolute_uri(path) if request else path


class MachineProfileSerializer(serializers.ModelSerializer):
    machine_type_display = serializers.CharField(source='get_machine_type_display', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True, default=None)

    class Meta:
        model = MachineProfile
        fields = ('id', 'name', 'machine_type', 'machine_type_display', 'brand', 'model_number',
                  'branch_name', 'is_active', 'total_print_hours', 'maintenance_due_date')
