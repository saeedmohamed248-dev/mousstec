"""
Tenant-isolated file storage for Mouss Tec ERP.
Ensures each tenant's media files are stored in a separate directory
to prevent cross-tenant file access and filename collisions.
"""
from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.conf import settings
import os


class TenantFileSystemStorage(FileSystemStorage):
    """
    A file storage backend that prefixes all file paths with the
    current tenant's schema name. This ensures:
    - Tenant A cannot access Tenant B's uploaded files
    - No filename collisions between tenants
    - Easy per-tenant backup and cleanup

    Directory structure: MEDIA_ROOT/<schema_name>/...
    """

    def _get_tenant_prefix(self):
        schema = getattr(connection, 'schema_name', 'public')
        return schema if schema else 'public'

    def path(self, name):
        tenant_prefix = self._get_tenant_prefix()
        tenant_path = os.path.join(settings.MEDIA_ROOT, tenant_prefix)
        os.makedirs(tenant_path, exist_ok=True)
        return os.path.join(tenant_path, name)

    def url(self, name):
        tenant_prefix = self._get_tenant_prefix()
        return f"{settings.MEDIA_URL}{tenant_prefix}/{name}"

    def save(self, name, content, max_length=None):
        # Ensure the tenant directory exists before saving
        full_path = self.path(name)
        directory = os.path.dirname(full_path)
        os.makedirs(directory, exist_ok=True)
        return super().save(name, content, max_length)

    def exists(self, name):
        return os.path.exists(self.path(name))

    def delete(self, name):
        full_path = self.path(name)
        if os.path.exists(full_path):
            os.remove(full_path)
