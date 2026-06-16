"""
🔮 Predictive Maintenance Engine

Compares each vehicle's last-done timestamps + mileage against the
ServiceReminderRule catalogue to produce ServiceNudge rows.

Three callers:
    1. Health Passport view → `compute_nudges_for_vehicle(v, persist=False)`
       — read-only, returns the ranked list without persisting.
    2. Daily Celery sweep → `refresh_all_nudges(branch=None)` — bulk-
       compute + upsert for every vehicle, so the CRM dashboard reads
       cheap aggregates.
    3. Post-job-card hook → after a maintenance JC is posted, call
       `recompute_nudges_for_vehicle(v)` so any newly-completed service
       resets the clock.

Heuristics:
    • "last_done" derives from posted SaleInvoiceItem/ServiceItem entries
      whose product or service category matches the rule. For v1 we use
      a coarse keyword-match on product/service names (cheap, no extra
      taxonomy). The accountant can override by stamping a category on
      the product (future enhancement).
    • Urgency:
        — overdue: due_at < today  OR  current_km >= due_km
        — due: due_at within 14 days  OR  within 1000 km
        — upcoming: due within 45 days OR within 3000 km
        — anything further out is silently ignored (no nudge).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("mouss_tec_core")

# Crude keyword map — sufficient for v1; tune as taxonomy matures.
_CATEGORY_KEYWORDS = {
    'engine_oil':       ['زيت محرك', 'engine oil', 'motor oil', 'تغيير زيت'],
    'brake_pads':       ['فرامل', 'brake', 'فحمات', 'pads', 'بريكات'],
    'spark_plugs':      ['بوجي', 'spark', 'بوجيهات'],
    'coolant':          ['تبريد', 'coolant', 'antifreeze', 'مياه التبريد'],
    'transmission_oil': ['زيت فتيس', 'transmission', 'gearbox', 'tiptronic'],
    'timing_belt':      ['كاتينة', 'timing', 'سير', 'تيمنج'],
    'air_filter':       ['فلتر هواء', 'air filter'],
    'cabin_filter':     ['فلتر مكيف', 'cabin filter', 'فلتر تكييف'],
    'battery':          ['بطارية', 'battery'],
    'wipers':           ['مساحات', 'wipers', 'wiper'],
    'general':          [],   # never matches — always falls back to last visit
}

_OVERDUE_WINDOW_DAYS = 0
_DUE_WINDOW_DAYS = 14
_UPCOMING_WINDOW_DAYS = 45
_DUE_WINDOW_KM = 1000
_UPCOMING_WINDOW_KM = 3000


@dataclass
class NudgeRow:
    rule_id: int
    rule_name: str
    category: str
    severity: str
    last_done_at: Optional[datetime]
    last_done_mileage: Optional[int]
    due_at: Optional[datetime]
    due_at_mileage: Optional[int]
    urgency: str
    reason: str

    def to_dict(self):
        return {
            'rule_id': self.rule_id,
            'rule_name': self.rule_name,
            'category': self.category,
            'severity': self.severity,
            'last_done_at': self.last_done_at,
            'last_done_mileage': self.last_done_mileage,
            'due_at': self.due_at,
            'due_at_mileage': self.due_at_mileage,
            'urgency': self.urgency,
            'reason': self.reason,
        }


def _matches_category(text: str, category: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(kw.lower() in t for kw in _CATEGORY_KEYWORDS.get(category, []))


def _last_done_for_category(vehicle, category: str):
    """Return (datetime, mileage) of the most recent posted job-card line
    matching this category. None values when no match."""
    from inventory.models import SaleInvoice

    qs = (SaleInvoice.objects
          .filter(vehicle=vehicle, invoice_type='maintenance', status='posted')
          .prefetch_related('items__product', 'service_items__service')
          .order_by('-date_created'))
    for jc in qs:
        # services first — more semantic
        for s in jc.service_items.all():
            if _matches_category(s.service.name, category):
                return jc.date_created, jc.id, vehicle.last_mileage
        for it in jc.items.all():
            if _matches_category(it.product.name, category):
                return jc.date_created, jc.id, vehicle.last_mileage
    # Fallback for 'general' rules: use vehicle's most recent visit
    if category == 'general' and qs.exists():
        first = qs.first()
        return first.date_created, first.id, vehicle.last_mileage
    return None, None, None


def _classify_urgency(due_at, due_km, *, current_mileage):
    """Return urgency code + a window-comparison reason fragment."""
    today = timezone.localdate()
    parts = []

    by_date = None
    if due_at is not None:
        delta = (due_at.date() if hasattr(due_at, 'date') else due_at) - today
        days = delta.days
        if days < _OVERDUE_WINDOW_DAYS:
            by_date = ('overdue', f"متأخر {abs(days)} يوم عن الموعد")
        elif days <= _DUE_WINDOW_DAYS:
            by_date = ('due', f"مستحق خلال {days} يوم")
        elif days <= _UPCOMING_WINDOW_DAYS:
            by_date = ('upcoming', f"يقترب — خلال {days} يوم")

    by_km = None
    if due_km is not None and current_mileage:
        diff = due_km - current_mileage
        if diff < 0:
            by_km = ('overdue', f"تجاوز الكيلومترات بـ {abs(diff)} كم")
        elif diff <= _DUE_WINDOW_KM:
            by_km = ('due', f"يحتاج خلال {diff} كم")
        elif diff <= _UPCOMING_WINDOW_KM:
            by_km = ('upcoming', f"يقترب — {diff} كم متبقية")

    # Take the worst of the two signals (overdue > due > upcoming)
    rank = {'overdue': 3, 'due': 2, 'upcoming': 1}
    candidates = [c for c in (by_date, by_km) if c]
    if not candidates:
        return None, ''
    winner = max(candidates, key=lambda c: rank[c[0]])
    parts.extend([c[1] for c in candidates if c[0] == winner[0]])
    return winner[0], ' · '.join(parts)


def compute_nudges_for_vehicle(vehicle, *, persist: bool = True) -> list[dict]:
    """Walk every active rule, derive last-done / due, classify urgency.
    Returns a list of dicts ordered by severity (high first) then urgency.

    `persist=True` upserts ServiceNudge rows so the CRM dashboard can read
    cheap aggregates. The Passport view passes persist=False to keep the
    request side-effect-free.
    """
    from inventory.models import ServiceReminderRule, ServiceNudge

    rules = list(
        ServiceReminderRule.objects.filter(is_active=True)
    )
    if not rules:
        return []

    current_mileage = vehicle.last_mileage or 0
    out: list[NudgeRow] = []

    for rule in rules:
        # Brand filter
        if rule.applies_to_brands:
            if (vehicle.brand or '').strip() not in rule.applies_to_brands:
                continue

        last_at, _, last_km = _last_done_for_category(vehicle, rule.category)

        # Compute due_at + due_km
        due_at = None
        if rule.interval_months and last_at:
            due_at = last_at + timedelta(days=rule.interval_months * 30)
        elif rule.interval_months and not last_at:
            # No prior service → assume from vehicle's first record
            due_at = (
                vehicle.date_added if hasattr(vehicle, 'date_added')
                else timezone.now()
            ) + timedelta(days=rule.interval_months * 30) \
                if False else None
            # No reliable anchor — skip the time-based signal
            due_at = None

        due_km = None
        if rule.interval_km and last_km is not None:
            due_km = last_km + rule.interval_km

        urgency, reason = _classify_urgency(
            due_at, due_km, current_mileage=current_mileage,
        )
        if urgency is None:
            continue  # not yet relevant

        out.append(NudgeRow(
            rule_id=rule.id,
            rule_name=rule.name,
            category=rule.category,
            severity=rule.severity,
            last_done_at=last_at,
            last_done_mileage=last_km,
            due_at=due_at,
            due_at_mileage=due_km,
            urgency=urgency,
            reason=reason,
        ))

    # Sort: overdue > due > upcoming, then high > medium > low severity
    sev_rank = {'high': 3, 'medium': 2, 'low': 1}
    urg_rank = {'overdue': 3, 'due': 2, 'upcoming': 1}
    out.sort(key=lambda r: (-urg_rank[r.urgency], -sev_rank[r.severity]))

    if persist and out:
        _upsert_nudges(vehicle, out)

    return [r.to_dict() for r in out]


def _upsert_nudges(vehicle, rows: list[NudgeRow]):
    """Bulk-upsert with status preservation — never clobber a 'dismissed'
    or 'sent' state when re-running the engine."""
    from inventory.models import ServiceNudge

    existing = {
        n.rule_id: n for n in
        ServiceNudge.objects.filter(vehicle=vehicle).select_related('rule')
    }
    with transaction.atomic():
        for row in rows:
            existing_n = existing.get(row.rule_id)
            if existing_n:
                # Refresh computed fields but preserve outreach state.
                existing_n.last_done_at = row.last_done_at
                existing_n.last_done_mileage = row.last_done_mileage
                existing_n.due_at = (
                    row.due_at.date() if row.due_at and hasattr(row.due_at, 'date')
                    else row.due_at
                )
                existing_n.due_at_mileage = row.due_at_mileage
                existing_n.urgency = row.urgency
                existing_n.reason = row.reason[:240]
                existing_n.refreshed_at = timezone.now()
                # If urgency drops to 'upcoming' and we already sent, leave it.
                existing_n.save(update_fields=[
                    'last_done_at', 'last_done_mileage', 'due_at',
                    'due_at_mileage', 'urgency', 'reason', 'refreshed_at',
                ])
            else:
                ServiceNudge.objects.create(
                    vehicle=vehicle,
                    rule_id=row.rule_id,
                    last_done_at=row.last_done_at,
                    last_done_mileage=row.last_done_mileage,
                    due_at=(row.due_at.date() if row.due_at and hasattr(row.due_at, 'date') else row.due_at),
                    due_at_mileage=row.due_at_mileage,
                    urgency=row.urgency,
                    reason=row.reason[:240],
                )


def refresh_all_nudges(branch=None, limit: int = 1000) -> dict:
    """Daily Celery sweep — recompute every active customer's vehicles.
    Caps at `limit` so a runaway loop on a huge tenant can't blow up.
    Returns summary stats for telemetry."""
    from inventory.models import Vehicle

    qs = Vehicle.objects.select_related('customer').order_by('-id')
    if branch is not None:
        qs = qs.filter(customer__last_branch=branch) if False else qs
    qs = qs[:limit]

    total = 0
    nudged = 0
    for v in qs:
        try:
            rows = compute_nudges_for_vehicle(v, persist=True)
            if rows:
                nudged += 1
            total += 1
        except Exception as exc:
            logger.warning("[predictive_engine] vehicle=%s failed: %s",
                           v.chassis_number, exc)
    return {'vehicles_scanned': total, 'vehicles_nudged': nudged}
