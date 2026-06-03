"""Make the Celery app available so @shared_task pickups + workers work."""
from .celery import app as celery_app

__all__ = ('celery_app',)
