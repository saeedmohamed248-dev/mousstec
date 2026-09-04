"""Core serializers — المستخدم والفرع."""
from django.contrib.auth.models import User
from rest_framework import serializers

from inventory.models.organization import Branch


class UserSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'full_name', 'email', 'is_staff', 'is_superuser')

    def get_full_name(self, obj: User) -> str:
        return obj.get_full_name() or obj.username


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ('id', 'name', 'location', 'phone')
