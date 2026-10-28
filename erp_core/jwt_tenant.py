"""
🏢 توكنات JWT مربوطة بالشركة.

🐛 [FIX — ثغرة حرجة]: التوكن كان فيه رقم المستخدم بس ومتوقّع بنفس SECRET_KEY لكل
الشركات، وكل شركة ليها جدول مستخدمين بيبدأ من 1. فأي حد يسجّل شركة جديدة ويدخل
من تطبيق الموبايل كان يقدر يبعت توكنه على دومين شركة تانية ويدخل كمستخدمها رقم 1
(صاحب الشركة غالباً) — كل الـ API (مش الموبايل بس) بيستخدم JWT افتراضياً.

دلوقتي كل توكن بيتختم بـ schema الشركة اللي اتعمل فيها، وأي توكن مالوش الختم ده
أو ختمه لشركة تانية بيترفض (401) — في الطلبات وفي التجديد.
"""
from django.db import connection
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer

TENANT_CLAIM = 'tenant'


def _current_schema():
    return getattr(connection, 'schema_name', None)


def check_token_tenant(token):
    if not token.get(TENANT_CLAIM) or token.get(TENANT_CLAIM) != _current_schema():
        raise InvalidToken('التوكن ده مش بتاع الشركة دي — سجّل دخول تاني.')


class TenantJWTAuthentication(JWTAuthentication):
    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)
        check_token_tenant(token)
        return token


class TenantTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)   # الـ access بيورث الختم من الـ refresh
        token[TENANT_CLAIM] = _current_schema()
        return token


class TenantTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        check_token_tenant(self.token_class(attrs['refresh']))
        from django.contrib.auth import get_user_model
        try:
            return super().validate(attrs)
        except get_user_model().DoesNotExist:
            # مستخدم اتمسح بعد ما أخد التوكن — كانت بتطلّع 500
            raise InvalidToken('الحساب ده مش موجود — سجّل دخول تاني.')
