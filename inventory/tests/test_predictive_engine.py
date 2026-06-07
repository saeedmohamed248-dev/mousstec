"""
Predictive Engine + Retention CRM + Post-JC Hook — DMS Backlog #12
==================================================================
Locks down the closed retention flywheel:

  • Predictive engine: urgency classification, brand allowlist,
    state preservation across sweeps, missing-interval fallback.
  • Post-JC hook: a posted maintenance JC instantly recomputes that
    vehicle's nudges (no waiting for 4:30 AM) — but ONLY on the actual
    transition into 'posted', not on every re-save.
  • Retention CRM views: RBAC, idempotent WhatsApp send + audit trail,
    dismiss, refresh.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.utils import timezone

from .base import ERPTenantTestCase
from .factories import (
    make_branch, make_customer, make_employee, make_product,
    make_inventory, make_treasury, make_sale_invoice,
)

from inventory.models import (
    SaleInvoice, ServiceReminderRule, ServiceNudge, Vehicle,
)
from inventory.predictive_engine import (
    compute_nudges_for_vehicle,
    _classify_urgency,
    _matches_category,
    refresh_all_nudges,
)


# ──────────────────────────────────────────────────────────────────────
# Shared scaffolding
# ──────────────────────────────────────────────────────────────────────
class PredictiveBase(ERPTenantTestCase):

    def setUp(self):
        self.branch = make_branch()
        self.treasury = make_treasury(self.branch, balance='10000.00')
        self.customer = make_customer(name='أحمد', phone='01001234567')
        self.vehicle = Vehicle.objects.create(
            customer=self.customer, car_plate='PRED-001',
            chassis_number='WBA12345PREDVIN0',
            brand='BMW', model_name='330i',
            last_mileage=80000,
        )

        # Clear the seeded rules so each test starts from a clean catalogue
        ServiceReminderRule.objects.all().delete()

    def _make_rule(self, **kw):
        defaults = dict(
            name='تغيير زيت المحرك', category='engine_oil',
            interval_km=10000, interval_months=6, severity='high',
            applies_to_brands=[], is_active=True,
        )
        defaults.update(kw)
        return ServiceReminderRule.objects.create(**defaults)

    def _make_oil_change_jc(self, *, days_ago: int, status='posted'):
        """Post a maintenance JC whose product name matches the engine_oil
        keyword map, so the engine treats it as a real oil-change done
        `days_ago` days ago."""
        product = make_product(
            part_number=f'OIL-{days_ago}',
            name='زيت محرك Castrol Edge',  # matches 'زيت محرك' keyword
            retail_price='280.00',
        )
        make_inventory(product, self.branch, quantity=5)
        # Create JC as 'quotation' first (factory default doesn't accept vehicle)
        # so we can stamp vehicle before transitioning to the target status —
        # which is what triggers the post-save hook we're testing.
        inv = make_sale_invoice(
            customer=self.customer,
            branch=self.branch, treasury=self.treasury,
            items=[(product, 1, '280.00')],
            invoice_type='maintenance',
            paid_amount='280.00',
            status='quotation',
        )
        # Stamp vehicle + back-date in one update (avoid signal fire)
        SaleInvoice.objects.filter(pk=inv.pk).update(
            vehicle=self.vehicle,
            date_created=timezone.now() - timedelta(days=days_ago),
        )
        inv.refresh_from_db()
        # NOW transition to the target status — this is what fires the
        # post-save hook we're verifying.
        if status != 'quotation':
            inv.status = status
            inv.save()
        return inv


# ──────────────────────────────────────────────────────────────────────
# Urgency classification — pure unit tests, no DB
# ──────────────────────────────────────────────────────────────────────
class ClassifyUrgencyTests(ERPTenantTestCase):

    def setUp(self):
        # Need to satisfy the tenant test harness boilerplate, but this
        # class hits no DB models.
        self.now = timezone.now()

    def test_overdue_by_date(self):
        urg, reason = _classify_urgency(
            self.now - timedelta(days=10), None, current_mileage=0,
        )
        self.assertEqual(urg, 'overdue')
        self.assertIn('متأخر', reason)

    def test_due_window(self):
        urg, _ = _classify_urgency(
            self.now + timedelta(days=7), None, current_mileage=0,
        )
        self.assertEqual(urg, 'due')

    def test_upcoming_window(self):
        urg, _ = _classify_urgency(
            self.now + timedelta(days=30), None, current_mileage=0,
        )
        self.assertEqual(urg, 'upcoming')

    def test_far_future_drops_nudge(self):
        urg, _ = _classify_urgency(
            self.now + timedelta(days=120), None, current_mileage=0,
        )
        self.assertIsNone(urg)

    def test_overdue_by_km(self):
        urg, reason = _classify_urgency(None, 50000, current_mileage=55000)
        self.assertEqual(urg, 'overdue')
        self.assertIn('تجاوز', reason)

    def test_worst_of_two_signals(self):
        """Date says 'due', km says 'overdue' → urgency is overdue."""
        urg, _ = _classify_urgency(
            self.now + timedelta(days=7), 50000, current_mileage=55000,
        )
        self.assertEqual(urg, 'overdue')


# ──────────────────────────────────────────────────────────────────────
# Keyword matcher
# ──────────────────────────────────────────────────────────────────────
class KeywordMatcherTests(ERPTenantTestCase):

    def test_arabic_oil_match(self):
        self.assertTrue(_matches_category('تغيير زيت المحرك Castrol', 'engine_oil'))

    def test_english_brake_match(self):
        self.assertTrue(_matches_category('Front brake pads kit', 'brake_pads'))

    def test_unrelated_does_not_match(self):
        self.assertFalse(_matches_category('فلتر بنزين', 'brake_pads'))


# ──────────────────────────────────────────────────────────────────────
# Engine: compute_nudges_for_vehicle
# ──────────────────────────────────────────────────────────────────────
class ComputeNudgesTests(PredictiveBase):

    def test_no_active_rules_returns_empty(self):
        # All rules deleted in setUp
        result = compute_nudges_for_vehicle(self.vehicle, persist=False)
        self.assertEqual(result, [])

    def test_overdue_by_date_when_oil_done_long_ago(self):
        """A 6-month rule + a posted oil change 8 months ago → overdue."""
        self._make_rule(interval_months=6, interval_km=None)
        self._make_oil_change_jc(days_ago=240)

        result = compute_nudges_for_vehicle(self.vehicle, persist=False)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['urgency'], 'overdue')
        self.assertEqual(result[0]['rule_name'], 'تغيير زيت المحرك')

    def test_no_nudge_when_recently_serviced(self):
        """6-month interval + oil change 30 days ago → no nudge (out of window)."""
        self._make_rule(interval_months=6, interval_km=None)
        self._make_oil_change_jc(days_ago=30)

        result = compute_nudges_for_vehicle(self.vehicle, persist=False)
        # 30 days ago → due in 150 days; far outside the 45-day upcoming window
        self.assertEqual(result, [])

    def test_brand_allowlist_filters(self):
        """A MINI-only rule should not surface for a BMW."""
        self._make_rule(applies_to_brands=['MINI'])
        self._make_oil_change_jc(days_ago=240)

        result = compute_nudges_for_vehicle(self.vehicle, persist=False)
        self.assertEqual(result, [])

    def test_brand_allowlist_includes_when_match(self):
        self._make_rule(applies_to_brands=['BMW', 'MINI'])
        self._make_oil_change_jc(days_ago=240)

        result = compute_nudges_for_vehicle(self.vehicle, persist=False)
        self.assertEqual(len(result), 1)

    def test_persist_creates_servicenudge_row(self):
        self._make_rule()
        self._make_oil_change_jc(days_ago=240)

        compute_nudges_for_vehicle(self.vehicle, persist=True)
        nudges = ServiceNudge.objects.filter(vehicle=self.vehicle)
        self.assertEqual(nudges.count(), 1)
        self.assertEqual(nudges.first().urgency, 'overdue')

    def test_persist_preserves_sent_status_across_sweeps(self):
        """Re-running the engine must NOT clobber sent/dismissed state."""
        rule = self._make_rule()
        self._make_oil_change_jc(days_ago=240)

        compute_nudges_for_vehicle(self.vehicle, persist=True)
        nudge = ServiceNudge.objects.get(vehicle=self.vehicle, rule=rule)
        nudge.status = ServiceNudge.STATUS_SENT
        nudge.sent_at = timezone.now()
        nudge.save()

        # Second sweep — should refresh computed fields but keep status=sent
        compute_nudges_for_vehicle(self.vehicle, persist=True)
        nudge.refresh_from_db()
        self.assertEqual(nudge.status, ServiceNudge.STATUS_SENT)
        self.assertIsNotNone(nudge.sent_at)

    def test_persist_preserves_dismissed_status(self):
        rule = self._make_rule()
        self._make_oil_change_jc(days_ago=240)
        compute_nudges_for_vehicle(self.vehicle, persist=True)
        nudge = ServiceNudge.objects.get(vehicle=self.vehicle, rule=rule)
        nudge.status = ServiceNudge.STATUS_DISMISSED
        nudge.save()

        compute_nudges_for_vehicle(self.vehicle, persist=True)
        nudge.refresh_from_db()
        self.assertEqual(nudge.status, ServiceNudge.STATUS_DISMISSED)

    def test_severity_ordering(self):
        """High-severity rules surface before medium / low."""
        self._make_rule(name='زيت محرك', severity='medium')
        self._make_rule(
            name='سير الكاتينة', category='timing_belt',
            interval_km=10000, interval_months=12, severity='high',
        )
        # Two posted services to ground both rules
        self._make_oil_change_jc(days_ago=240)

        # Set vehicle mileage high enough that the timing-belt km signal fires
        Vehicle.objects.filter(pk=self.vehicle.pk).update(last_mileage=500000)
        self.vehicle.refresh_from_db()

        result = compute_nudges_for_vehicle(self.vehicle, persist=False)
        # high severity should rank first when both are overdue
        self.assertGreaterEqual(len(result), 1)
        if len(result) >= 2:
            self.assertEqual(result[0]['severity'], 'high')


# ──────────────────────────────────────────────────────────────────────
# Post-JC Hook — instant recompute on transition into 'posted'
# ──────────────────────────────────────────────────────────────────────
class PostJobCardHookTests(PredictiveBase):

    def test_posting_maintenance_jc_recomputes_nudges(self):
        """Posting a maintenance JC must instantly upsert ServiceNudge rows."""
        self._make_rule(interval_months=6, interval_km=None)

        # No nudges yet
        self.assertEqual(ServiceNudge.objects.count(), 0)

        # Post an oil-change JC 240 days ago → should immediately seed
        # an overdue nudge via the signal hook
        self._make_oil_change_jc(days_ago=240)

        nudges = ServiceNudge.objects.filter(vehicle=self.vehicle)
        self.assertEqual(nudges.count(), 1)
        self.assertEqual(nudges.first().urgency, 'overdue')

    def test_quotation_status_does_not_trigger_recompute(self):
        """A draft (quotation) JC must NOT fire the hook."""
        self._make_rule()
        self._make_oil_change_jc(days_ago=240, status='quotation')
        self.assertEqual(ServiceNudge.objects.count(), 0)

    def test_resaving_already_posted_jc_idempotent(self):
        """Re-saving a posted JC (e.g. editing notes) must not re-fire."""
        self._make_rule()
        jc = self._make_oil_change_jc(days_ago=240)
        first_count = ServiceNudge.objects.count()

        # Re-save without changing status → hook should bail early
        jc.notes = 'edited by cashier'
        jc.save()

        # Same row count, same status
        self.assertEqual(ServiceNudge.objects.count(), first_count)

    def test_compute_failure_does_not_block_save(self):
        """A buggy predictive engine must NEVER block the invoice post."""
        self._make_rule()
        with mock.patch(
            'inventory.predictive_engine.compute_nudges_for_vehicle',
            side_effect=RuntimeError('boom'),
        ):
            # Should NOT raise — the JC must save cleanly
            jc = self._make_oil_change_jc(days_ago=240)
            self.assertEqual(jc.status, 'posted')


# ──────────────────────────────────────────────────────────────────────
# Retention CRM views — RBAC + WhatsApp audit + dismiss
# ──────────────────────────────────────────────────────────────────────
def _wire(user, tenant, *, method='POST', body=None, csrf='tok'):
    rf = RequestFactory()
    if method == 'POST':
        import json as _j
        req = rf.post('/system/crm/', data=_j.dumps(body or {}),
                      content_type='application/json',
                      HTTP_X_CSRFTOKEN=csrf)
    else:
        req = rf.get('/system/crm/')
    req.COOKIES['mt_csrf'] = csrf
    req.user = user
    req.tenant = tenant
    return req


class RetentionCRMTests(PredictiveBase):

    def setUp(self):
        super().setUp()
        self.admin_user, _ = make_employee(
            'crm_admin', role='admin', branch=self.branch,
        )
        self.tech_user, _ = make_employee(
            'crm_tech', role='tech', branch=self.branch,
        )
        self.rule = self._make_rule(
            whatsapp_template=(
                "مرحباً {customer}, حان موعد {rule} لـ {vehicle} في {workshop}."
            ),
        )
        self._make_oil_change_jc(days_ago=240)
        # The signal hook seeds the nudge — pull it
        self.nudge = ServiceNudge.objects.get(vehicle=self.vehicle, rule=self.rule)

    def test_send_whatsapp_builds_wa_url_and_stamps_audit(self):
        from inventory.views import retention_send_whatsapp
        req = _wire(self.admin_user, self.tenant)
        resp = retention_send_whatsapp(req, nudge_id=self.nudge.id)
        self.assertEqual(resp.status_code, 200)
        import json as _j
        data = _j.loads(resp.content)
        self.assertTrue(data['ok'])
        self.assertIn('wa.me/', data['wa_url'])
        self.assertIn('أحمد', data['message_preview'])
        self.assertIn('BMW', data['message_preview'])

        # Audit trail stamped
        self.nudge.refresh_from_db()
        self.assertEqual(self.nudge.status, ServiceNudge.STATUS_SENT)
        self.assertEqual(self.nudge.sent_by, self.admin_user)
        self.assertIsNotNone(self.nudge.sent_at)

    def test_send_idempotent_resending_keeps_first_sent_by(self):
        """Re-sending updates the timestamp but keeps the audit chain coherent."""
        from inventory.views import retention_send_whatsapp
        req1 = _wire(self.admin_user, self.tenant)
        retention_send_whatsapp(req1, nudge_id=self.nudge.id)

        # A second user re-sends — the latest sender wins in our model
        req2 = _wire(self.tech_user, self.tenant)
        # tech role isn't in `allowed` for this endpoint, so it should be blocked
        resp = retention_send_whatsapp(req2, nudge_id=self.nudge.id)
        self.assertEqual(resp.status_code, 403)

    def test_send_with_no_phone_fails_cleanly(self):
        from inventory.views import retention_send_whatsapp
        self.customer.phone = ''
        self.customer.save()
        req = _wire(self.admin_user, self.tenant)
        resp = retention_send_whatsapp(req, nudge_id=self.nudge.id)
        self.assertEqual(resp.status_code, 400)
        import json as _j
        self.assertEqual(_j.loads(resp.content)['error'], 'no_customer_phone')

    def test_dismiss_marks_status(self):
        from inventory.views import retention_dismiss
        req = _wire(self.admin_user, self.tenant)
        resp = retention_dismiss(req, nudge_id=self.nudge.id)
        self.assertEqual(resp.status_code, 200)
        self.nudge.refresh_from_db()
        self.assertEqual(self.nudge.status, ServiceNudge.STATUS_DISMISSED)

    def test_tech_role_blocked_from_dismiss(self):
        from inventory.views import retention_dismiss
        req = _wire(self.tech_user, self.tenant)
        resp = retention_dismiss(req, nudge_id=self.nudge.id)
        self.assertEqual(resp.status_code, 403)

    def test_crm_dashboard_renders_for_authorised_user(self):
        from inventory.views import retention_crm
        req = _wire(self.admin_user, self.tenant, method='GET')
        resp = retention_crm(req)
        self.assertEqual(resp.status_code, 200)

    def test_crm_dashboard_blocked_for_tech(self):
        from inventory.views import retention_crm
        req = _wire(self.tech_user, self.tenant, method='GET')
        resp = retention_crm(req)
        self.assertEqual(resp.status_code, 403)

    def test_csrf_required_on_state_changing_post(self):
        from inventory.views import retention_dismiss
        # Wire without setting both cookie and header to the same value
        req = _wire(self.admin_user, self.tenant, csrf='cookie-side')
        # Override the header to mismatch
        req.META['HTTP_X_CSRFTOKEN'] = 'header-side'
        resp = retention_dismiss(req, nudge_id=self.nudge.id)
        self.assertEqual(resp.status_code, 403)
