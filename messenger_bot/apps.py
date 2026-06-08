from django.apps import AppConfig


class MessengerBotConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "messenger_bot"
    verbose_name = "Mousstec Messenger Bot"

    def ready(self):
        from . import signals  # noqa: F401
