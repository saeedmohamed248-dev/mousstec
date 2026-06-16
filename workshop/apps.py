"""
Workshop app — automotive service vertical (OBD ingest, predictive
maintenance, vehicle CRM nudges).

Phase 2A of Wave 2 (see docs/ARCHITECTURE.md). Owns the OBD telemetry
ingest endpoint and the predictive-maintenance engine that powers the
Vehicle Health Passport view and the daily ServiceNudge sweep. Models
(Vehicle, VehicleDiagnosticReport, RepairLog, ServiceReminderRule,
ServiceNudge, ...) still live in inventory.models.* (Wave 1 submodules)
until Phase 2B can move them with `db_table` preserved.
"""
from django.apps import AppConfig


class WorkshopConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'workshop'
    verbose_name = 'Workshop (Automotive Service Vertical)'
