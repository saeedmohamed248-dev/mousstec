from django.urls import path

from .views import MessengerWebhookView

app_name = "messenger_bot"

urlpatterns = [
    path("messenger/", MessengerWebhookView.as_view(), name="messenger-webhook"),
]
