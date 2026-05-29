"""
🔧 إصلاح المستأجرين بدون سجل Domain — يمنع TenantMainMiddleware من توجيه الـ subdomain.
الاستخدام: python manage.py fix_missing_domains
"""
import os
from django.core.management.base import BaseCommand
from clients.models import Client, Domain


class Command(BaseCommand):
    help = 'Create missing Domain records for tenants that lack one'

    def handle(self, *args, **options):
        base_domain = os.getenv('BASE_DOMAIN', 'mousstec.com')
        tenants = Client.objects.exclude(schema_name='public')
        fixed = 0

        for tenant in tenants:
            url_safe_slug = tenant.schema_name.replace('_', '-')
            domain_str = f"{url_safe_slug}.{base_domain}"
            domain, created = Domain.objects.get_or_create(
                domain=domain_str,
                defaults={'tenant': tenant, 'is_primary': True}
            )
            if created:
                fixed += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  ✅ Created domain '{domain_str}' for tenant '{tenant.name}'"
                ))
            else:
                self.stdout.write(f"  ⏭️  Domain '{domain_str}' already exists")

        self.stdout.write(self.style.SUCCESS(
            f"\n🎯 Done. Fixed {fixed} tenant(s) with missing domains."
        ))
