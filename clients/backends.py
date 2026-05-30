"""
Custom Authentication Backend — Case-Insensitive Email Login
============================================================
Django's ModelBackend does exact (case-sensitive) username lookup.
Since we store email as username (lowercased in signup), but users
may type mixed-case emails in the login form, we need this backend.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

UserModel = get_user_model()


class CaseInsensitiveEmailBackend(ModelBackend):
    """
    Authenticates against username (email) case-insensitively.
    Falls back gracefully if no match is found.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            return None
        # Normalize: lowercase + strip whitespace
        username = username.strip().lower()
        try:
            user = UserModel.objects.get(username__iexact=username)
        except UserModel.DoesNotExist:
            # Run the default password hasher to prevent timing attacks
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            # Edge case: multiple users with same email different case
            user = UserModel.objects.filter(username__iexact=username).order_by('pk').first()

        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
