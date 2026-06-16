from django.urls import path

from . import views

app_name = 'ai_rooms'

urlpatterns = [
    path('', views.hub, name='hub'),
    path('history/', views.history, name='history'),
    path('history/<int:conv_id>/', views.conversation_detail, name='detail'),
]
