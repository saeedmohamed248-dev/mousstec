"""
RepairLog Duration Computation — DMS Backlog #1
================================================
Locks down the `duration_minutes` property which drives Tech Workspace
timer + payroll commission base. The math:

    duration_minutes = (ended_at - started_at - paused_seconds) // 60

Tested across the lifecycle states:
    open    → ended_at=None  → uses now()
    paused  → paused_seconds accumulates while last_paused_at is set
    done    → ended_at is final
    blocked → no time impact (just status flag)
"""
from datetime import timedelta
from django.utils import timezone

from inventory.models import RepairLog, SaleInvoice

from .base import ERPTenantTestCase
from .factories import make_branch, make_customer, make_employee


class RepairLogDurationTests(ERPTenantTestCase):

    def setUp(self):
        self.branch = make_branch()
        self.customer = make_customer()
        _, self.tech_profile = make_employee(
            'rl_tech', role='tech', branch=self.branch,
        )
        self.job_card = SaleInvoice.objects.create(
            customer=self.customer, branch=self.branch,
            invoice_type='maintenance', status='in_progress',
            notes='RepairLog test job',
        )

    def _make_log(self, started_minutes_ago=0, paused_seconds=0, ended=False,
                  status='open'):
        started = timezone.now() - timedelta(minutes=started_minutes_ago)
        ended_at = timezone.now() if ended else None
        return RepairLog.objects.create(
            job_card=self.job_card, technician=self.tech_profile,
            task_title='تغيير الفرامل',
            started_at=started, ended_at=ended_at,
            paused_seconds=paused_seconds, status=status,
        )

    # ── happy path ────────────────────────────────────────────────────
    def test_completed_30min_log_returns_30(self):
        log = self._make_log(started_minutes_ago=30, ended=True, status='done')
        self.assertEqual(log.duration_minutes, 30)

    def test_open_log_uses_now_as_end(self):
        """When ended_at is None, duration counts up to current time."""
        log = self._make_log(started_minutes_ago=15, status='open')
        self.assertEqual(log.duration_minutes, 15)

    # ── pause math ────────────────────────────────────────────────────
    def test_paused_seconds_subtracted_from_duration(self):
        """45 min wallclock - 600 s (10 min) paused = 35 min effective."""
        log = self._make_log(
            started_minutes_ago=45, paused_seconds=600, ended=True, status='done',
        )
        self.assertEqual(log.duration_minutes, 35)

    def test_paused_seconds_exceeding_wallclock_clamps_to_zero(self):
        """Defensive: if paused_seconds > wallclock somehow, duration must
        not go negative (would break commission accrual)."""
        log = self._make_log(
            started_minutes_ago=5, paused_seconds=99999, ended=True, status='done',
        )
        self.assertEqual(log.duration_minutes, 0)

    # ── status-specific behavior ──────────────────────────────────────
    def test_blocked_status_does_not_affect_duration(self):
        """`blocked` is a signal flag; duration still counts to now()."""
        log = self._make_log(started_minutes_ago=20, status='blocked')
        self.assertEqual(log.duration_minutes, 20)

    def test_paused_status_with_last_paused_at_field_set(self):
        """RepairLog.last_paused_at is editable=False — tests just verify it
        can be set without breaking duration math."""
        log = self._make_log(started_minutes_ago=10, paused_seconds=180, status='paused')
        log.last_paused_at = timezone.now()
        log.save()
        # 10 min wallclock - 3 min paused = 7 min
        self.assertEqual(log.duration_minutes, 7)

    # ── role gate on FK ───────────────────────────────────────────────
    def test_technician_limit_choices_documented(self):
        """RepairLog.technician has limit_choices_to={role__in: ['tech', 'engineer']}.
        That's an admin-form hint — the DB doesn't enforce it. This test
        documents the constraint so a future refactor adding DB CHECK is
        intentional."""
        admin_user, admin_profile = make_employee(
            'rl_admin', role='admin', branch=self.branch,
        )
        # No constraint violation expected at the DB level even though admin
        # is outside the limit_choices_to set
        log = RepairLog.objects.create(
            job_card=self.job_card, technician=admin_profile,
            task_title='admin-attached log',
        )
        self.assertEqual(log.technician_id, admin_profile.pk)
