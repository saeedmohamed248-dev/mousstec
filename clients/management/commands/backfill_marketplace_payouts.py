"""
Queue MarketplacePayout rows for escrow holds settled before the payout
center existed.

Holds settled earlier (released_to_seller / refunded_to_buyer / split) left
the platform owing money with no record of whether it was ever sent. Run this
once after deploying, then review the queue at /superadmin/parts/payouts/ —
mark what was already paid by hand (with its reference) and pay the rest.

    python manage.py backfill_marketplace_payouts            # dry run
    python manage.py backfill_marketplace_payouts --commit   # create rows
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context


class Command(BaseCommand):
    help = "Create payout rows for escrow holds settled before the payout center existed."

    def add_arguments(self, parser):
        parser.add_argument('--commit', action='store_true', help='Actually create the rows.')

    def handle(self, *args, commit=False, **options):
        from clients.models import EscrowHold
        from marketplace_b2b.services.payouts import queue_for_hold

        with schema_context('public'):
            holds = (EscrowHold.objects
                     .filter(status__in=('released_to_seller', 'refunded_to_buyer', 'split'))
                     .filter(payouts__isnull=True)
                     .select_related('order', 'order__listing'))
            self.stdout.write(f"{holds.count()} settled hold(s) without payout rows.")
            if not commit:
                for h in holds[:50]:
                    self.stdout.write(f"  - order {h.order.order_code}: {h.status} "
                                      f"seller={h.seller_payout_amount} refund={h.buyer_refund_amount}")
                self.stdout.write("Dry run — pass --commit to create them.")
                return
            created = 0
            for h in holds:
                created += len(queue_for_hold(h, notify=False))
            self.stdout.write(self.style.SUCCESS(f"Created {created} payout row(s)."))
