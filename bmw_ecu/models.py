"""Cloud-sync models: persist every ECU session + state change to Mousstec DB.

Why: a tech's laptop dies mid-coding → next day the senior tech opens the
case file, sees the last successful state, and knows exactly where to resume.

These are tenant-scoped (default tenant context — `django_tenants` routes
the schema). Models are intentionally minimal — heavy detail lives in the
on-disk backup files referenced by `backup_sha256`.
"""
from __future__ import annotations

from django.db import models


class EcuSession(models.Model):
    """One technician's session against one car. Owns many state changes."""

    vin = models.CharField(max_length=17, db_index=True)
    chassis = models.CharField(max_length=8, blank=True, help_text="F30, G20, ...")
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    technician = models.CharField(max_length=64, blank=True)
    transport_kind = models.CharField(max_length=16, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "ECU Session"

    def __str__(self) -> str:
        return f"{self.vin} @ {self.started_at:%Y-%m-%d %H:%M}"


class EcuStateChange(models.Model):
    """An atomic, append-only event in an ECU session."""

    KIND_CHOICES = [
        ("connect", "Connect"),
        ("session", "Diagnostic session"),
        ("security", "Security access"),
        ("read_isn", "ISN read"),
        ("write_isn", "ISN write"),
        ("ews_sync", "EWS sync"),
        ("code", "Coding"),
        ("flash_start", "Flash start"),
        ("flash_done", "Flash done"),
        ("rollback", "Rollback"),
        ("error", "Error"),
    ]

    session = models.ForeignKey(EcuSession, on_delete=models.CASCADE,
                                related_name="state_changes")
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=24, choices=KIND_CHOICES, db_index=True)
    ecu_name = models.CharField(max_length=32, blank=True)
    success = models.BooleanField(default=True)
    backup_sha256 = models.CharField(max_length=64, blank=True, db_index=True,
                                     help_text="Content-addressed reference to the on-disk dump")
    payload_summary = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["at"]
        verbose_name = "ECU State Change"

    def __str__(self) -> str:
        return f"{self.session.vin} · {self.kind} · {self.ecu_name}"


class ExecutionAttempt(models.Model):
    """Audit row for every Manager-orchestrated run (whichever strategy won)."""

    STRATEGY_CHOICES = [
        ("software_only", "Software Only"),
        ("hardware_automation", "Hardware Automation"),
        ("interactive_guided", "Interactive Guided"),
    ]
    OUTCOME_CHOICES = [
        ("success", "Success"),
        ("partial", "Partial"),
        ("suspended", "Suspended"),
        ("failed_rolled_back", "Failed (rolled back)"),
        ("failed_unrecoverable", "Failed (unrecoverable)"),
    ]

    session = models.ForeignKey(EcuSession, on_delete=models.CASCADE,
                                related_name="execution_attempts")
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    profile_name = models.CharField(max_length=32, db_index=True)
    strategy = models.CharField(max_length=24, choices=STRATEGY_CHOICES, db_index=True)
    outcome = models.CharField(max_length=24, choices=OUTCOME_CHOICES, db_index=True)
    exploit_used = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    diagnostics = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Execution Attempt"


class WizardSession(models.Model):
    """Persisted state of an InteractiveGuidedStrategy run between requests."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    state = models.CharField(max_length=32, db_index=True)
    vin = models.CharField(max_length=17, db_index=True)
    ecu_name = models.CharField(max_length=32)
    captured_isn_hex = models.CharField(max_length=128, blank=True)
    technician_id = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True)
    error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Wizard Session"


class EcuPinoutDiagram(models.Model):
    """One pinout image + callouts per ECU. Editable from Django admin."""

    ecu_name = models.CharField(max_length=32, unique=True)
    image_url = models.CharField(max_length=512)
    callouts = models.JSONField(default=list, blank=True,
                                help_text='[{"pin": 18, "label": "BOOT", "color": "red"}]')
    description = models.TextField(blank=True)

    class Meta:
        verbose_name = "ECU Pinout Diagram"

    def __str__(self) -> str:
        return self.ecu_name


class EcuHardwareProfile(models.Model):
    """A board-revision-specific bench profile keyed on the live Hardware ID.

    The dynamic hardware catalog ships a small bundled seed in code; this
    table lets a workshop register confirmed real board revisions (pins,
    images, steps) from the Django admin without a code change. Lookup is
    DB-first, falling back to the bundled catalog.
    """

    hardware_id = models.CharField(max_length=32, unique=True,
                                   help_text="Live HWEL / part number, e.g. 8606229")
    ecu_name = models.CharField(max_length=64, help_text="Marketing name, e.g. MEVD17.2.9")
    board_revision = models.CharField(max_length=64, help_text="e.g. Rev B (N20 pre-LCI)")
    family = models.CharField(max_length=32, blank=True, help_text="MEVD17 | FEM | CAS ...")
    protocol = models.CharField(max_length=32, blank=True, help_text="BootMode | BDM | JTAG")

    power_pin = models.PositiveIntegerField(help_text="KL30 / +12V")
    ground_pin = models.PositiveIntegerField(help_text="KL31 / GND")
    boot_pin = models.PositiveIntegerField(null=True, blank=True,
                                           help_text="Ground/probe to enter BSL/BDM")
    can_h_pin = models.PositiveIntegerField(null=True, blank=True)
    can_l_pin = models.PositiveIntegerField(null=True, blank=True)
    k_line_pin = models.PositiveIntegerField(null=True, blank=True)

    pcb_image_url = models.CharField(max_length=512, blank=True,
                                     help_text="Full PCB photo for orientation")
    boot_image_url = models.CharField(max_length=512, blank=True,
                                      help_text="Close-up of the boot pad/pin")
    callouts = models.JSONField(default=list, blank=True,
                                help_text='[{"pin": 24, "label": "BOOT", "color": "yellow"}]')
    physical_steps_ar = models.JSONField(default=list, blank=True,
                                         help_text="Variant-specific steps (Arabic)")
    physical_steps_en = models.JSONField(default=list, blank=True,
                                         help_text="Variant-specific steps (English)")
    notes = models.TextField(blank=True)
    verified = models.BooleanField(default=False,
                                   help_text="True once confirmed on real hardware")

    class Meta:
        verbose_name = "ECU Hardware Profile (bench)"

    def __str__(self) -> str:
        return f"{self.hardware_id} — {self.ecu_name} {self.board_revision}"


class DiagnosticFeeCharge(models.Model):
    """Pay-Per-Success ledger: 450 EGP per unlocked VIN.

    Lifecycle:
        authorized → captured  (charge finalised; SUCCESS)
        authorized → released  (charge cancelled; rolled back / failed)
        authorized → expired   (stale; reaper task should release)

    Idempotency: only one row per (vin, status='authorized') at any time.
    Re-calling authorize() returns the existing open row.
    """

    STATUS_CHOICES = [
        ("authorized", "Authorized"),
        ("captured", "Captured"),
        ("released", "Released"),
        ("expired", "Expired"),
        ("declined", "Declined"),
    ]

    vin = models.CharField(max_length=17, db_index=True)
    operation_type = models.CharField(max_length=16, default="isn",
                                      db_index=True,
                                      help_text="isn | coding")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EGP")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default="authorized", db_index=True)
    authorization_ref = models.CharField(max_length=64, unique=True,
                                         help_text="Idempotency key")
    session = models.ForeignKey("EcuSession", on_delete=models.SET_NULL,
                                null=True, blank=True,
                                related_name="fee_charges")
    authorized_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finalised_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-authorized_at"]
        verbose_name = "Diagnostic Fee Charge"
        constraints = [
            models.UniqueConstraint(
                fields=["vin"], condition=models.Q(status="authorized"),
                name="bmw_ecu_one_open_auth_per_vin",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vin} · {self.amount} {self.currency} · {self.status}"


class GiftCredit(models.Model):
    """Promotional grant from Mousstec management to a specific workshop.

    Two kinds:
      - CODING_CREDITS / ISN_CREDITS — counted, decrement on each use.
      - SUBSCRIPTION_WINDOW — time-bounded, unlimited use during window.

    Always tried FIRST before wallet/Paymob settlement. Idempotency at the
    issuing layer is enforced by the admin endpoint (one open grant of
    the same kind per tenant unless `allow_stack=True`).
    """

    GRANT_TYPE_CHOICES = [
        ("coding_credits", "Coding credits (count)"),
        ("isn_credits", "ISN credits (count)"),
        ("subscription_window", "Subscription window (time-bounded)"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),
        ("consumed", "Consumed"),
        ("expired", "Expired"),
        ("revoked", "Revoked by admin"),
    ]

    tenant_schema = models.CharField(max_length=64, db_index=True,
                                     help_text="clients.Client.schema_name")
    grant_type = models.CharField(max_length=24, choices=GRANT_TYPE_CHOICES,
                                  db_index=True)
    credits_total = models.PositiveIntegerField(default=0,
                                                help_text="0 for time-based")
    credits_remaining = models.PositiveIntegerField(default=0)
    valid_from = models.DateTimeField(auto_now_add=True, db_index=True)
    valid_until = models.DateTimeField(null=True, blank=True, db_index=True,
                                       help_text="null = no time bound")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default="active", db_index=True)
    note = models.CharField(max_length=255, blank=True)
    granted_by = models.CharField(max_length=64, blank=True,
                                  help_text="username of issuing super-admin")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-granted_at"]
        verbose_name = "Gift Credit"
        indexes = [
            models.Index(fields=["tenant_schema", "status", "grant_type"]),
        ]

    def __str__(self) -> str:
        if self.grant_type == "subscription_window":
            return f"{self.tenant_schema} · subscription · until {self.valid_until:%Y-%m-%d}"
        return f"{self.tenant_schema} · {self.grant_type} · {self.credits_remaining}/{self.credits_total}"

    def is_consumable(self) -> bool:
        from django.utils import timezone
        if self.status != "active":
            return False
        if self.valid_until and self.valid_until < timezone.now():
            return False
        if self.grant_type == "subscription_window":
            return True
        return self.credits_remaining > 0


class GiftCreditUsage(models.Model):
    """Audit row each time a gift credit is consumed (or window-touched)."""

    gift = models.ForeignKey(GiftCredit, on_delete=models.CASCADE,
                             related_name="usages")
    vin = models.CharField(max_length=17, db_index=True)
    operation_type = models.CharField(max_length=16, db_index=True)
    used_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reference = models.CharField(max_length=64, blank=True,
                                 help_text="DiagnosticFeeCharge.authorization_ref")

    class Meta:
        ordering = ["-used_at"]
        verbose_name = "Gift Credit Usage"

    def __str__(self) -> str:
        return f"{self.vin} · {self.operation_type} · {self.used_at:%Y-%m-%d}"


class CodingEntitlementHold(models.Model):
    """Audit row for Coding operations attempted without an active subscription.

    Created by entitlement.DefaultEntitlementProvider when a Coding
    request lands but the tenant has no Coding add-on. The hold is a
    placeholder so when pricing is finalised the finance team can chase
    these and convert them to charges OR drop them.

    Idempotency: one open hold per (vin, operation_type).
    """

    STATUS_CHOICES = [
        ("open", "Open (awaiting policy)"),
        ("converted", "Converted to charge"),
        ("dropped", "Dropped (not charged)"),
    ]

    vin = models.CharField(max_length=17, db_index=True)
    operation_type = models.CharField(max_length=16, db_index=True)
    tenant_schema = models.CharField(max_length=64, blank=True, db_index=True)
    hold_ref = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default="open", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Coding Entitlement Hold"
        constraints = [
            models.UniqueConstraint(
                fields=["vin", "operation_type"],
                condition=models.Q(status="open"),
                name="bmw_ecu_one_open_hold_per_vin_op",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vin} · {self.operation_type} · {self.status}"


class BmwEcuSettlement(models.Model):
    """Audit row for every settlement attempt against a captured fee.

    Created by LocalBillingGate.on_captured. Decouples the diagnostic
    capture (which is final once it lands) from the money movement
    (which can succeed, fail, or end up as a Paymob iframe waiting on
    the workshop to pay).
    """

    MODE_CHOICES = [
        ("gift", "Gift Credit"),
        ("wallet", "Wallet Deduct"),
        ("paymob", "Paymob Iframe"),
        ("failed", "Settlement Failed"),
    ]

    charge = models.OneToOneField(
        "DiagnosticFeeCharge", on_delete=models.CASCADE,
        related_name="settlement",
    )
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, db_index=True)
    succeeded = models.BooleanField(default=False, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EGP")
    wallet_before = models.DecimalField(max_digits=15, decimal_places=2,
                                        null=True, blank=True)
    wallet_after = models.DecimalField(max_digits=15, decimal_places=2,
                                       null=True, blank=True)
    paymob_iframe_url = models.URLField(max_length=1024, blank=True)
    gift = models.ForeignKey("GiftCredit", on_delete=models.SET_NULL,
                             null=True, blank=True,
                             related_name="settlements")
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "BMW ECU Settlement"

    def __str__(self) -> str:
        return f"{self.charge.vin} · {self.mode} · {self.amount} {self.currency}"


#############################################################################
# Granular SaaS Billing — Feature catalog, Packages, and per-tenant Grants.
#
# Layered on top of the existing CodingEntitlementHold / GiftCredit machinery,
# NOT a replacement. The legacy settings whitelist (BMW_ECU_CODING_ENTITLED_*)
# still applies — when no feature_code is asked for, the old code path runs
# unchanged. When a feature_code IS specified, the new tables decide.
#
# Mental model:
#   Feature                 → atomic capability ("frm_repair", "key_programming")
#   SubscriptionPackage     → admin-defined bundle of Features ("Key Master")
#   TenantPackageGrant      → workshop bought / was gifted a Package instance
#   TenantFeatureGrant      → workshop got one Feature directly (no package)
#
# Both grant tables carry the SAME limit semantics (time-bound OR usage-bound)
# so a single helper can answer "is this tenant currently entitled to X?".
#############################################################################
class Feature(models.Model):
    """Atomic capability the platform sells. Admin-managed catalog row."""

    CATEGORY_CHOICES = [
        ("coding", "Coding (CAFD / FA-VO)"),
        ("repair", "Module Repair (FRM, footwell, BDC)"),
        ("key_programming", "Key Programming"),
        ("isn_reset", "ISN Reset (EGS / used module swap)"),
        ("crash_reset", "Crash Reset (Airbag / SRS)"),
        ("battery", "Battery / CBS Management"),
        ("flash", "Flashing / Firmware"),
        ("diagnostic", "Diagnostic / Read-only"),
        ("other", "Other"),
    ]
    # Mirrors bmw_ecu.services.entitlement.OperationType so legacy callers
    # that only know "coding" / "isn" still resolve correctly when a Feature
    # is asked for as part of one of those flows.
    OP_TYPE_CHOICES = [
        ("coding", "Coding"),
        ("isn", "ISN"),
        ("flash", "Flash"),
        ("repair", "Repair"),
        ("reset", "Reset"),
    ]

    code = models.SlugField(max_length=64, unique=True,
                            help_text="Stable machine identifier, e.g. 'frm_repair'.")
    name = models.CharField(max_length=128)
    category = models.CharField(max_length=24, choices=CATEGORY_CHOICES,
                                default="other", db_index=True)
    default_operation_type = models.CharField(
        max_length=16, choices=OP_TYPE_CHOICES, default="coding",
        help_text="Coarse-grained op family — keeps the legacy ISN-vs-Coding gate happy.",
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True,
                                    help_text="False hides this feature from new package bundles.")
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "code"]
        verbose_name = "Feature"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class SubscriptionPackage(models.Model):
    """A bundle of Features the Super Admin can sell as one product.

    Packages carry DEFAULT limits — actual TenantPackageGrant rows snapshot
    those defaults at purchase time so a price/quota change to the Package
    catalog never silently mutates an already-active customer subscription.
    """

    BILLING_MODE_CHOICES = [
        ("time", "Time-bound (e.g. 30 days)"),
        ("usage", "Usage-bound (e.g. 10 successful runs)"),
        ("hybrid", "Hybrid (whichever runs out first)"),
        ("unlimited", "Unlimited (lifetime)"),
    ]

    code = models.SlugField(max_length=64, unique=True,
                            help_text="e.g. 'pkg_key_master', 'pkg_full_suite'.")
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    features = models.ManyToManyField(
        Feature, related_name="packages", blank=True,
        help_text="Atomic features bundled into this package.",
    )

    billing_mode = models.CharField(max_length=16, choices=BILLING_MODE_CHOICES,
                                    default="time", db_index=True)
    default_duration_days = models.PositiveIntegerField(
        default=30, help_text="Time-bound: how long the grant is valid. 0 = unlimited.",
    )
    default_usage_quota = models.PositiveIntegerField(
        default=0, help_text="Usage-bound: total successful operations allowed. 0 = unlimited.",
    )
    price_egp = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Sticker price the storefront card shows.",
    )
    currency = models.CharField(max_length=3, default="EGP")
    is_active = models.BooleanField(default=True, db_index=True)
    is_featured = models.BooleanField(
        default=False, help_text="Highlight on the storefront ('Most popular').",
    )
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "code"]
        verbose_name = "Subscription Package"

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    def feature_codes(self) -> list[str]:
        """Convenience — list of feature codes contained in this package."""
        return list(self.features.filter(is_active=True).values_list("code", flat=True))


class _AbstractTenantGrant(models.Model):
    """Shared limit/lifecycle plumbing for TenantPackageGrant & TenantFeatureGrant.

    Lifecycle states mirror GiftCredit so the front-end can render both
    tables in one timeline without a special-case per type.
    """

    STATUS_CHOICES = [
        ("active", "Active"),
        ("expired", "Expired (time)"),
        ("exhausted", "Exhausted (usage)"),
        ("revoked", "Revoked by admin"),
    ]
    BILLING_MODE_CHOICES = SubscriptionPackage.BILLING_MODE_CHOICES

    tenant_schema = models.CharField(max_length=64, db_index=True,
                                     help_text="clients.Client.schema_name")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default="active", db_index=True)
    billing_mode = models.CharField(max_length=16, choices=BILLING_MODE_CHOICES,
                                    default="time", db_index=True)

    valid_from = models.DateTimeField(auto_now_add=True, db_index=True)
    valid_until = models.DateTimeField(null=True, blank=True, db_index=True,
                                       help_text="null = unlimited time")
    usage_quota = models.PositiveIntegerField(default=0,
                                              help_text="0 = unlimited usage")
    usage_used = models.PositiveIntegerField(default=0)

    price_paid_egp = models.DecimalField(max_digits=10, decimal_places=2,
                                         default=0)
    granted_by = models.CharField(max_length=64, blank=True,
                                  help_text="Super-admin username or 'paymob:<txn>'")
    note = models.CharField(max_length=255, blank=True)

    granted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-granted_at"]

    # ---- limit-state helpers ----------------------------------------------
    def usage_remaining(self) -> int | None:
        """None when unlimited, else int >= 0."""
        if self.usage_quota == 0:
            return None
        return max(0, self.usage_quota - self.usage_used)

    def is_time_expired(self) -> bool:
        from django.utils import timezone
        return bool(self.valid_until and self.valid_until < timezone.now())

    def is_usage_exhausted(self) -> bool:
        return self.usage_quota > 0 and self.usage_used >= self.usage_quota

    def is_currently_valid(self) -> bool:
        """Active + within time window + has usage left (mode-aware)."""
        if self.status != "active":
            return False
        if self.billing_mode in ("time", "hybrid") and self.is_time_expired():
            return False
        if self.billing_mode in ("usage", "hybrid") and self.is_usage_exhausted():
            return False
        return True


class TenantPackageGrant(_AbstractTenantGrant):
    """A specific tenant ↔ package binding with its own limits + audit trail."""

    package = models.ForeignKey(
        SubscriptionPackage, on_delete=models.PROTECT,
        related_name="tenant_grants",
    )

    class Meta(_AbstractTenantGrant.Meta):
        verbose_name = "Tenant Package Grant"
        indexes = [
            models.Index(fields=["tenant_schema", "status"]),
            models.Index(fields=["tenant_schema", "package", "status"]),
        ]

    def __str__(self) -> str:
        suffix = self.valid_until.strftime("%Y-%m-%d") if self.valid_until else "∞"
        return f"{self.tenant_schema} · {self.package.code} · until {suffix}"


class TenantFeatureGrant(_AbstractTenantGrant):
    """A specific tenant ↔ single-feature binding (a-la-carte purchases)."""

    feature = models.ForeignKey(
        Feature, on_delete=models.PROTECT,
        related_name="tenant_grants",
    )

    class Meta(_AbstractTenantGrant.Meta):
        verbose_name = "Tenant Feature Grant"
        indexes = [
            models.Index(fields=["tenant_schema", "status"]),
            models.Index(fields=["tenant_schema", "feature", "status"]),
        ]

    def __str__(self) -> str:
        suffix = self.valid_until.strftime("%Y-%m-%d") if self.valid_until else "∞"
        return f"{self.tenant_schema} · {self.feature.code} · until {suffix}"


class FeatureUsageEvent(models.Model):
    """Append-only consumption ledger — one row per successful feature run.

    Decrements `usage_used` on the source grant via the entitlement service
    (NOT via signals, to keep the transaction in one place). Powers usage
    reports + lets accounting trace "which VIN ate which subscription unit".
    """

    GRANT_KIND_CHOICES = [
        ("package", "Package grant"),
        ("feature", "Feature grant"),
    ]

    tenant_schema = models.CharField(max_length=64, db_index=True)
    feature = models.ForeignKey(Feature, on_delete=models.PROTECT,
                                related_name="usage_events")
    grant_kind = models.CharField(max_length=8, choices=GRANT_KIND_CHOICES,
                                  db_index=True)
    package_grant = models.ForeignKey(TenantPackageGrant,
                                      on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      related_name="usage_events")
    feature_grant = models.ForeignKey(TenantFeatureGrant,
                                      on_delete=models.SET_NULL,
                                      null=True, blank=True,
                                      related_name="usage_events")
    vin = models.CharField(max_length=17, db_index=True, blank=True)
    operation_ref = models.CharField(max_length=64, blank=True,
                                     help_text="DiagnosticFeeCharge.authorization_ref or session id")
    used_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-used_at"]
        verbose_name = "Feature Usage Event"
        indexes = [
            models.Index(fields=["tenant_schema", "feature", "used_at"]),
        ]
        constraints = [
            # Idempotency at the DB level: a second consume() call with the
            # same (tenant, feature, operation_ref) triple cannot insert a
            # duplicate even under racy concurrent retries. Empty operation_ref
            # is excluded so callers that don't pass an idempotency key can
            # still book unlimited events (e.g. legacy/anonymous flows).
            models.UniqueConstraint(
                fields=["tenant_schema", "feature", "operation_ref"],
                condition=~models.Q(operation_ref=""),
                name="bmw_ecu_unique_consume_per_op_ref",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_schema} · {self.feature.code} · {self.used_at:%Y-%m-%d}"


class EcuBackupRef(models.Model):
    """Index of on-disk backups so the cloud knows what's safely stored."""

    vin = models.CharField(max_length=17, db_index=True)
    ecu_name = models.CharField(max_length=32)
    memory_region = models.CharField(max_length=16)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveIntegerField()
    path = models.CharField(max_length=512)
    captured_at = models.DateTimeField()
    uploaded_to_s3 = models.BooleanField(default=False)

    class Meta:
        ordering = ["-captured_at"]
        verbose_name = "ECU Backup Reference"

    def __str__(self) -> str:
        return f"{self.vin}/{self.ecu_name}/{self.sha256[:12]}"
