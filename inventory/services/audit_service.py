"""
📋 Audit Service — Owns structured audit logging.

Responsibilities:
- Pre-save snapshot for change detection
- Post-save audit entry (create / update)
- Post-delete audit entry with full snapshot
- Thread-local user/IP extraction (shared utility)
"""

import logging

logger = logging.getLogger('mouss_tec_core')

# Models that are audited
AUDITED_MODELS = [
    'Product', 'Customer', 'Vendor', 'SaleInvoice', 'PurchaseInvoice',
    'Inventory', 'StockTransfer', 'FinancialTransaction', 'Treasury',
    'ChartOfAccount', 'AccountingEntry',
]


class AuditService:
    """Centralized audit trail for all audited domain models."""

    # ------------------------------------------------------------------
    # Thread-local helpers (used by other services too)
    # ------------------------------------------------------------------
    @staticmethod
    def get_request_ip():
        """Extract IP from AuditIPMiddleware thread-local."""
        try:
            from erp_core.middleware import _audit_thread_local
            return getattr(_audit_thread_local, 'ip', None)
        except Exception:
            return None

    @staticmethod
    def get_request_user():
        """Extract user from AuditIPMiddleware thread-local."""
        try:
            from erp_core.middleware import _audit_thread_local
            return getattr(_audit_thread_local, 'user', None)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------
    @staticmethod
    def build_changes_dict(old_instance, new_instance):
        """Build a dict of {field: {old, new}} for changed fields."""
        changes = {}
        if old_instance is None:
            return changes
        for field in new_instance._meta.fields:
            fname = field.name
            old_val = getattr(old_instance, fname, None)
            new_val = getattr(new_instance, fname, None)
            if str(old_val) != str(new_val):
                changes[fname] = {'old': str(old_val), 'new': str(new_val)}
        return changes

    # ------------------------------------------------------------------
    # Signal handlers — called from thin signals
    # ------------------------------------------------------------------
    @staticmethod
    def snapshot_before_save(sender, instance):
        """
        Store a copy of the record before modification so we can diff later.
        Attaches _audit_old_instance to the instance.
        """
        if sender.__name__ not in AUDITED_MODELS:
            return
        if instance.pk:
            try:
                instance._audit_old_instance = sender.objects.get(pk=instance.pk)
            except sender.DoesNotExist:
                instance._audit_old_instance = None
        else:
            instance._audit_old_instance = None

    @staticmethod
    def log_save(sender, instance, created):
        """Log a create or update event to AuditLog."""
        if sender.__name__ not in AUDITED_MODELS:
            return
        from inventory.models import AuditLog

        try:
            action = 'create' if created else 'update'
            old_inst = getattr(instance, '_audit_old_instance', None)
            changes = {} if created else AuditService.build_changes_dict(old_inst, instance)

            # Skip no-op updates
            if action == 'update' and not changes:
                return

            AuditLog.objects.create(
                user=AuditService.get_request_user(),
                action=action,
                model_name=sender.__name__,
                object_id=str(instance.pk),
                object_repr=str(instance)[:255],
                changes_json=changes,
                ip_address=AuditService.get_request_ip(),
            )
        except Exception as e:
            logger.error("[AUDIT] Failed to log %s #%s: %s", sender.__name__, instance.pk, e)

    @staticmethod
    def log_delete(sender, instance):
        """Log a delete event with a full snapshot of the deleted record."""
        if sender.__name__ not in AUDITED_MODELS:
            return
        from inventory.models import AuditLog

        try:
            snapshot = {}
            for field in instance._meta.fields:
                snapshot[field.name] = str(getattr(instance, field.name, ''))

            AuditLog.objects.create(
                user=AuditService.get_request_user(),
                action='delete',
                model_name=sender.__name__,
                object_id=str(instance.pk),
                object_repr=str(instance)[:255],
                changes_json={'deleted_snapshot': snapshot},
                ip_address=AuditService.get_request_ip(),
            )
        except Exception as e:
            logger.error("[AUDIT] Failed to log deletion of %s #%s: %s", sender.__name__, instance.pk, e)
