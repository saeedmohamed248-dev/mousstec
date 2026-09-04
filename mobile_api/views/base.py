"""أدوات مشتركة لعروض Mobile API."""
from django.db.models import ProtectedError
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


class ReadOnlyViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """قراءة فقط (list + retrieve) محميّة بالمصادقة."""

    permission_classes = [IsAuthenticated]


class ListOnlyViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]


class SafeDestroyMixin:
    """يمنع خطأ 500 عند محاولة حذف سجل مرتبط بسجلات أخرى (FK محمي)."""

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        try:
            instance.delete()
        except ProtectedError:
            return Response(
                {'detail': 'لا يمكن حذف هذا السجل لارتباطه بسجلات أخرى (فواتير/حركات).'},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class FullCrudViewSet(SafeDestroyMixin, viewsets.ModelViewSet):
    """CRUD كامل (list/retrieve/create/update/partial_update/destroy) للبيانات الرئيسية."""

    permission_classes = [IsAuthenticated]
