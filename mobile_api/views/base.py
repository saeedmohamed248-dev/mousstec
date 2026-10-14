"""أدوات مشتركة لعروض Mobile API."""
from django.db.models import ProtectedError
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..permissions import MANAGEMENT, RoleScoped, branch_ids


class RoleScopedMixin:
    """صلاحيات حسب الدور (read_roles/write_roles) + فلتر الفروع للموظف."""
    permission_classes = [IsAuthenticated, RoleScoped]
    read_roles = None           # أي موظف
    write_roles = MANAGEMENT    # الأدمن والمدير

    def scope_branches(self, qs, field='branch_id'):
        """يقصر النتائج على فروع الموظف (الأدمن: كل الفروع)."""
        ids = branch_ids(self.request.user)
        if ids is None:
            return qs
        return qs.filter(**{f'{field}__in': ids})


class ReadOnlyViewSet(RoleScopedMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """قراءة فقط (list + retrieve) محميّة بالمصادقة والدور."""


class ListOnlyViewSet(RoleScopedMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    pass


class SafeDestroyMixin:
    """يمنع خطأ 500 عند محاولة حذف سجل مرتبط بسجلات أخرى (FK محمي)."""

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            self.perform_destroy(instance)   # perform_destroy بتاع كل ViewSet (فحوصاته) بيتنفّذ
        except ProtectedError:
            return Response(
                {'detail': 'لا يمكن حذف هذا السجل لارتباطه بسجلات أخرى (فواتير/حركات).'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class FullCrudViewSet(RoleScopedMixin, SafeDestroyMixin, viewsets.ModelViewSet):
    """CRUD كامل (list/retrieve/create/update/partial_update/destroy) للبيانات الرئيسية."""


class ZeroBalanceDestroyMixin:
    """🐛 [FIX]: عميل/مورد ليه أو عليه رصيد كان بيتمسح من التطبيق (الأدمن بيمنعها)
    — الرصيد يفضل في الدفتر من غير صاحبه. لازم يتسوّى الأول."""

    def perform_destroy(self, instance):
        if instance.balance:
            raise serializers.ValidationError(
                {'detail': f'الرصيد مش صفر ({instance.balance}) — سوّيه الأول قبل الحذف.'})
        instance.delete()
