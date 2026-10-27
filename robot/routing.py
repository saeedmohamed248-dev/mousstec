"""WebSocket routes for the robot: «مخ موس» talking live (robot/consumers.py)."""
from django.urls import path

from robot.consumers import RobotVoiceConsumer

websocket_urlpatterns = [
    path("ws/robot/voice/", RobotVoiceConsumer.as_asgi()),
]
