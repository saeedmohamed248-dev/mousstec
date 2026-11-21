"""🔔 تسجيل جهاز الموبايل لإشعارات Firebase (بعد الدخول) وإلغاؤه (عند الخروج)."""
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import MobileDevice


class DeviceSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)
    platform = serializers.ChoiceField(choices=('android', 'ios', 'web'), default='android')
    language = serializers.ChoiceField(choices=('ar', 'en'), default='ar')


class DeviceView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ser = DeviceSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        # نفس الجهاز ممكن يتنقل لموظف تاني (دخول بحساب مختلف) ⇒ التوكن يتبع آخر مستخدم
        device, created = MobileDevice.objects.update_or_create(
            token=d['token'], defaults={'user': request.user, 'platform': d['platform'],
                                        'language': d['language'], 'is_active': True})
        from ..push import is_configured
        return Response({'id': device.pk, 'push_enabled': is_configured()},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def delete(self, request):
        token = (request.data.get('token') if hasattr(request, 'data') else None) \
            or request.query_params.get('token')
        if not token:
            return Response({'token': ['مطلوب.']}, status=status.HTTP_400_BAD_REQUEST)
        MobileDevice.objects.filter(token=token, user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
