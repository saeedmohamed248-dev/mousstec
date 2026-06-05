"""
HR + Tech Workspace Role Gate Tests — DMS Backlog #1
=====================================================
Covers the exact regression we saw during the audit:

  • Issue #1: clicking HR button on dashboard logged users out because
    role_required returned JSON 403 to a browser navigation.
  • Issue: tech roles could see HR data and vice versa.

The fix lives in `role_required` (HTML response for browser nav, JSON only
for AJAX/API). These tests lock that behavior down.
"""
from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.messages.middleware import MessageMiddleware

from inventory.views_hr import hr_workspace
from inventory.views_tech import tech_workspace

from .base import ERPTenantTestCase
from .factories import make_branch, make_employee


def _wire(user, tenant, path='/', headers=None):
    rf = RequestFactory()
    extra = {}
    if headers:
        for k, v in headers.items():
            extra[f'HTTP_{k.upper().replace("-", "_")}'] = v
    req = rf.get(path, **extra)
    SessionMiddleware(lambda r: None).process_request(req)
    req.session.save()
    AuthenticationMiddleware(lambda r: None).process_request(req)
    MessageMiddleware(lambda r: None).process_request(req)
    req.user = user
    req.tenant = tenant
    return req


class HRWorkspaceRoleGateTests(ERPTenantTestCase):
    """Allowed: hr / admin / manager. Forbidden: tech / engineer / cashier / sales."""

    def setUp(self):
        self.branch = make_branch()

    def _user_for_role(self, role):
        user, _ = make_employee(f'u_{role}', role=role, branch=self.branch)
        return user

    # ── allowed roles ────────────────────────────────────────────────
    def test_hr_role_allowed(self):
        r = hr_workspace(_wire(self._user_for_role('hr'), self.tenant, '/system/hr-workspace/'))
        self.assertEqual(r.status_code, 200)

    def test_admin_role_allowed(self):
        r = hr_workspace(_wire(self._user_for_role('admin'), self.tenant, '/system/hr-workspace/'))
        self.assertEqual(r.status_code, 200)

    def test_manager_role_allowed(self):
        r = hr_workspace(_wire(self._user_for_role('manager'), self.tenant, '/system/hr-workspace/'))
        self.assertEqual(r.status_code, 200)

    # ── forbidden roles ──────────────────────────────────────────────
    def test_tech_role_forbidden_html(self):
        """Issue #1 guard: browser nav must get HTML 403, not JSON."""
        user = self._user_for_role('tech')
        r = hr_workspace(_wire(user, self.tenant, '/system/hr-workspace/'))
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'<!DOCTYPE html>', r.content[:50])

    def test_cashier_role_forbidden(self):
        user = self._user_for_role('cashier')
        r = hr_workspace(_wire(user, self.tenant, '/system/hr-workspace/'))
        self.assertEqual(r.status_code, 403)

    def test_sales_role_forbidden(self):
        user = self._user_for_role('sales')
        r = hr_workspace(_wire(user, self.tenant, '/system/hr-workspace/'))
        self.assertEqual(r.status_code, 403)

    # ── AJAX clients get JSON ────────────────────────────────────────
    def test_ajax_client_gets_json_403(self):
        """role_required must keep JSON path for AJAX/API clients."""
        user = self._user_for_role('tech')
        r = hr_workspace(_wire(
            user, self.tenant, '/system/hr-workspace/',
            headers={'X-Requested-With': 'XMLHttpRequest'},
        ))
        self.assertEqual(r.status_code, 403)
        # JSON body shape (not the HTML fallback)
        self.assertIn(b'"error"', r.content)


class TechWorkspaceRoleGateTests(ERPTenantTestCase):
    """Allowed: tech / engineer. Forbidden: hr / cashier / sales."""

    def setUp(self):
        self.branch = make_branch()

    def _user_for_role(self, role):
        user, _ = make_employee(f'u_{role}', role=role, branch=self.branch)
        return user

    def test_tech_role_allowed(self):
        r = tech_workspace(_wire(self._user_for_role('tech'), self.tenant, '/system/tech-workspace/'))
        self.assertEqual(r.status_code, 200)

    def test_engineer_role_allowed(self):
        r = tech_workspace(_wire(self._user_for_role('engineer'), self.tenant, '/system/tech-workspace/'))
        self.assertEqual(r.status_code, 200)

    def test_hr_role_forbidden(self):
        r = tech_workspace(_wire(self._user_for_role('hr'), self.tenant, '/system/tech-workspace/'))
        self.assertEqual(r.status_code, 403)

    def test_cashier_role_forbidden(self):
        r = tech_workspace(_wire(self._user_for_role('cashier'), self.tenant, '/system/tech-workspace/'))
        self.assertEqual(r.status_code, 403)
