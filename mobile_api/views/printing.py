"""🎨 Printing views — المطابع في تطبيق الموبايل.

نفس منطق الويب بالظبط (مفيش قيود أو حسابات جديدة):
  • إنشاء فاتورة/طلب  ← printing.quick_invoice.create_quick_invoice
  • تسليم الطلب        ← printing.quick_invoice.deliver_order
  • نقل مهمة لمرحلة     ← PrintJob.advance_stage (بيحترم موافقة البروفة)
  • تأكيد مسودة          ← حد الائتمان (apply_credit_hold) + الدفتر (ledger.on_order_changed)
الصلاحيات = صلاحيات Django الموديل (جروبات «فريق العمل» في الويب).
"""
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from printing.models import (
    MachineProfile, PriceQuotation, PrintCustomer, PrintJob, PrintMaterial, PrintOrder,
    PrintProof, PrintTreasury,
)

from ..permissions import PRINTING, IndustryGate, PrintModelPerms
from ..serializers.printing import (
    MachineProfileSerializer, PriceQuotationSerializer, PrintCustomerDetailSerializer,
    PrintCustomerListSerializer, PrintCustomerWriteSerializer, PrintJobSerializer,
    PrintMaterialSerializer, PrintOrderActionSerializer, PrintOrderCreateSerializer,
    PrintOrderDetailSerializer, PrintOrderListSerializer, PrintProofSerializer,
    allowed_order_actions,
)

OPEN_ORDER_STATUSES = ('draft', 'confirmed', 'in_progress', 'ready')


def _errors(exc):
    return '؛ '.join(getattr(exc, 'messages', None) or [str(exc)])


class PrintingMixin:
    permission_classes = [IsAuthenticated, IndustryGate, PrintModelPerms]
    industry = PRINTING
    perm_model = None


def _search(qs, request, *fields):
    term = (request.query_params.get('search') or '').strip()
    if not term:
        return qs
    q = Q()
    for f in fields:
        q |= Q(**{f'{f}__icontains': term})
    return qs.filter(q)


class PrintDashboardView(PrintingMixin, APIView):
    """📊 الشاشة الرئيسية للمطبعة: الطلبات المفتوحة، الإنتاج، البروفات، الخامات الناقصة."""
    perm_model = 'printorder'

    def get(self, request):
        now = timezone.now()
        today = timezone.localdate()
        orders = PrintOrder.objects.all()
        by_status = {r['status']: r['c'] for r in orders.filter(status__in=OPEN_ORDER_STATUSES)
                     .values('status').annotate(c=Count('id'))}
        active_jobs = (PrintJob.objects.filter(is_complete=False)
                       .exclude(order__status__in=('draft', 'cancelled', 'delivered')))
        by_stage = {r['stage']: r['c'] for r in active_jobs.values('stage').annotate(c=Count('id'))}
        data = {
            'open_orders': sum(by_status.values()),
            'drafts': by_status.get('draft', 0),
            'confirmed': by_status.get('confirmed', 0),
            'in_progress': by_status.get('in_progress', 0),
            'ready': by_status.get('ready', 0),
            'late_orders': orders.filter(status__in=OPEN_ORDER_STATUSES, date_due__lt=now).count(),
            'due_today': orders.filter(status__in=OPEN_ORDER_STATUSES, date_due__date=today).count(),
            'active_jobs': sum(by_stage.values()),
            'jobs_by_stage': {code: by_stage.get(code, 0) for code in PrintJob.STAGE_FLOW if code != 'done'},
            'pending_proofs': PrintProof.objects.filter(status='pending').count(),
            # المهام اللي العميل طلب عليها تعديل ولسه في التصميم (مهمة واحدة = واحد حتى لو كذا نسخة)
            'changes_requested': (PrintProof.objects.filter(status='changes_requested', job__stage='design',
                                                            job__is_complete=False)
                                  .values('job_id').distinct().count()),
            'low_stock_materials': PrintMaterial.objects.filter(quantity__lte=F('min_stock')).count(),
            'total_customers': PrintCustomer.objects.count(),
            'sales_today': None,
        }
        # 💰 فواتير النهارده (الصافي بالضريبة) لمن يشوف الخزنة بس — نفس تعريف net_total
        if request.user.has_perm('printing.view_printtransaction'):
            net = ExpressionWrapper(F('total_amount') - F('discount') + F('tax_amount'),
                                    output_field=DecimalField(max_digits=14, decimal_places=2))
            total = (orders.exclude(status__in=PrintOrder.NON_DEBT_STATUSES)
                     .filter(date_created__date=today).aggregate(t=Sum(net))['t'])
            data['sales_today'] = (total or Decimal('0')).quantize(Decimal('0.01'))
        return Response(data)


class PrintOrderViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                        mixins.CreateModelMixin, viewsets.GenericViewSet):
    perm_model = 'printorder'
    action_perms = {'order_action': ('printing.change_printorder',)}

    def get_queryset(self):
        qs = PrintOrder.objects.select_related('customer').order_by('-date_created')
        if self.action == 'retrieve':
            qs = qs.prefetch_related('jobs__machine', 'jobs__designer__user')
        st = self.request.query_params.get('status')
        if st == 'open':
            qs = qs.filter(status__in=OPEN_ORDER_STATUSES)
        elif st:
            qs = qs.filter(status=st)
        if self.request.query_params.get('customer'):
            qs = qs.filter(customer_id=self.request.query_params['customer'])
        return _search(qs, self.request, 'order_number', 'customer__name', 'customer__phone')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return PrintOrderDetailSerializer
        if self.action == 'create':
            return PrintOrderCreateSerializer
        return PrintOrderListSerializer

    def create(self, request, *args, **kwargs):
        from printing.quick_invoice import create_quick_invoice
        ser = PrintOrderCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        user = request.user
        paid = d['paid']
        treasury = None
        if paid > 0:
            # 💰 تحصيل فلوس = صلاحية الخزنة (زي الفاتورة السريعة في الويب)
            if not user.has_perm('printing.add_printtransaction'):
                return Response({'detail': 'مش مسموحلك تسجّل تحصيل — سيب المدفوع صفر.'},
                                status=status.HTTP_403_FORBIDDEN)
            treasury = PrintTreasury.objects.filter(
                pk=d.get('treasury'), is_active=True, kind__in=PrintTreasury.MONEY_KINDS).first()
            if treasury is None:
                return Response({'treasury': ['اختار الخزنة اللي الفلوس دخلت فيها.']},
                                status=status.HTTP_400_BAD_REQUEST)
        lines = [(ln['description'], str(ln['quantity']), str(ln['unit_price'])) for ln in d['lines']]
        try:
            order, warnings = create_quick_invoice(
                user=user, lines=lines, customer_id=d.get('customer'),
                customer_name=d['customer_name'], customer_phone=d['customer_phone'],
                discount=str(d['discount']), delivered=d['delivered'], paid=str(paid),
                treasury=treasury, notes=d['notes'])
        except DjangoValidationError as exc:
            return Response({'detail': _errors(exc)}, status=status.HTTP_400_BAD_REQUEST)
        out = PrintOrderDetailSerializer(order, context={'request': request}).data
        out['warnings'] = warnings
        return Response(out, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='action')
    def order_action(self, request, pk=None):
        """confirm (مسودة ← مؤكد) / deliver (اتسلّم) / cancel (من غير مدفوع)."""
        from printing import ledger
        from printing.quick_invoice import deliver_order
        ser = PrintOrderActionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        act = ser.validated_data['action']
        order = self.get_object()
        if act not in allowed_order_actions(order):
            msg = ('الطلب عليه مدفوعات — الإلغاء ورد الفلوس من الويب («إلغاء الطلبات ورد المدفوع»).'
                   if act == 'cancel' and order.paid_amount else
                   f'مينفعش «{act}» والطلب حالته «{order.get_status_display()}».')
            return Response({'detail': msg}, status=status.HTTP_409_CONFLICT)
        warnings = []
        try:
            if act == 'deliver':
                order = deliver_order(order)
            elif act == 'confirm':
                with transaction.atomic():
                    order = PrintOrder.objects.select_for_update().get(pk=order.pk)
                    previous = order.status
                    order.status = 'confirmed'
                    order.save()
                    hold = order.apply_credit_hold(previous_status=previous)
                    if hold:
                        warnings.append(hold)
                    else:
                        order.sync_status_from_jobs()
                ledger.on_order_changed(order.pk)
            else:  # cancel — مفيش فلوس على الطلب (اتأكدنا فوق) فمفيش رد
                with transaction.atomic():
                    order = PrintOrder.objects.select_for_update().get(pk=order.pk)
                    if order.compute_paid():
                        return Response({'detail': 'الطلب عليه مدفوعات — الإلغاء من الويب.'},
                                        status=status.HTTP_409_CONFLICT)
                    order.status = 'cancelled'
                    order.save()
                ledger.on_order_changed(order.pk)
        except DjangoValidationError as exc:
            return Response({'detail': _errors(exc)}, status=status.HTTP_400_BAD_REQUEST)
        order = PrintOrder.objects.get(pk=order.pk)
        out = PrintOrderDetailSerializer(order, context={'request': request}).data
        out['warnings'] = warnings
        return Response(out)


class PrintJobViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """🏭 لوحة الإنتاج: المهام الشغالة + «المرحلة الجاية»."""
    serializer_class = PrintJobSerializer
    perm_model = 'printjob'
    action_perms = {'advance': ('printing.change_printjob',)}

    def get_queryset(self):
        qs = (PrintJob.objects.select_related('order', 'order__customer', 'machine', 'designer__user')
              .order_by(F('order__date_due').asc(nulls_last=True), 'stage_changed_at', 'id'))
        params = self.request.query_params
        if self.action == 'list' and params.get('all') != '1':
            qs = qs.filter(is_complete=False).exclude(order__status__in=('draft', 'cancelled', 'delivered'))
        if params.get('stage'):
            qs = qs.filter(stage=params['stage'])
        if params.get('order'):
            qs = qs.filter(order_id=params['order'])
        if params.get('machine'):
            qs = qs.filter(machine_id=params['machine'])
        return _search(qs, self.request, 'description', 'order__order_number', 'order__customer__name')

    @action(detail=True, methods=['post'])
    def advance(self, request, pk=None):
        job = self.get_object()
        if job.order.status in ('draft', 'cancelled', 'delivered'):
            return Response({'detail': 'الطلب مش في الإنتاج (مسودة أو ملغي أو اتسلّم).'},
                            status=status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                job.advance_stage()
                job.order.sync_status_from_jobs()
        except DjangoValidationError as exc:
            return Response({'detail': _errors(exc)}, status=status.HTTP_409_CONFLICT)
        job.refresh_from_db()
        return Response(PrintJobSerializer(job, context={'request': request}).data)

    @action(detail=True, methods=['get'])
    def proofs(self, request, pk=None):
        if not request.user.has_perm('printing.view_printproof'):
            return Response({'detail': 'صلاحيتك مش كفاية للعملية دي.'}, status=status.HTTP_403_FORBIDDEN)
        job = self.get_object()
        return Response(PrintProofSerializer(job.proofs.order_by('-version'), many=True,
                                             context={'request': request}).data)


class PrintCustomerViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                           mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """العملاء — من غير حذف (الحذف والرصيد الافتتاحي من الويب عشان الدفاتر)."""
    perm_model = 'printcustomer'
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def get_queryset(self):
        return _search(PrintCustomer.objects.order_by('name'), self.request,
                       'name', 'phone', 'whatsapp', 'company')

    def get_serializer_class(self):
        if self.action in ('create', 'partial_update', 'update'):
            return PrintCustomerWriteSerializer
        if self.action == 'retrieve':
            return PrintCustomerDetailSerializer
        return PrintCustomerListSerializer


class PrintMaterialViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                           viewsets.GenericViewSet):
    """الخامات — قراءة (الحركات والتسويات بتتقيّد من الويب)."""
    serializer_class = PrintMaterialSerializer
    perm_model = 'printmaterial'

    def get_queryset(self):
        qs = PrintMaterial.objects.select_related('branch').order_by('category', 'name')
        if self.request.query_params.get('low') == '1':
            qs = qs.filter(quantity__lte=F('min_stock'))
        if self.request.query_params.get('category'):
            qs = qs.filter(category=self.request.query_params['category'])
        return _search(qs, self.request, 'name', 'sku')

    @action(detail=False, methods=['get'], url_path='low-stock')
    def low_stock(self, request):
        qs = PrintMaterial.objects.select_related('branch').filter(quantity__lte=F('min_stock'))
        return Response(self.get_serializer(qs.order_by('name'), many=True).data)


class PriceQuotationViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    serializer_class = PriceQuotationSerializer
    perm_model = 'pricequotation'

    def get_queryset(self):
        qs = PriceQuotation.objects.select_related('customer').prefetch_related('lines').order_by('-created_at')
        if self.request.query_params.get('status'):
            qs = qs.filter(status=self.request.query_params['status'])
        return _search(qs, self.request, 'quote_number', 'title', 'customer_name', 'customer__name')


class PrintProofViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                        viewsets.GenericViewSet):
    """🖼️ البروفات — رابط المشاركة بيتبعت للعميل على واتساب من التطبيق."""
    serializer_class = PrintProofSerializer
    perm_model = 'printproof'

    def get_queryset(self):
        qs = PrintProof.objects.select_related('job', 'job__order').order_by('-created_at')
        if self.request.query_params.get('status'):
            qs = qs.filter(status=self.request.query_params['status'])
        return qs


class MachineProfileViewSet(PrintingMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                            viewsets.GenericViewSet):
    serializer_class = MachineProfileSerializer
    perm_model = 'machineprofile'

    def get_queryset(self):
        return MachineProfile.objects.select_related('branch').order_by('-is_active', 'name')


class PrintTreasuryListView(PrintingMixin, APIView):
    """الخزائن اللي ينفع التحصيل فيها (للفاتورة من الموبايل) — من غير أرصدة."""
    perm_model = None

    def get(self, request):
        if not request.user.has_perm('printing.add_printtransaction'):
            return Response([])
        rows = (PrintTreasury.objects.filter(is_active=True, kind__in=PrintTreasury.MONEY_KINDS)
                .order_by('pk').values('id', 'name', 'kind'))
        return Response(list(rows))
