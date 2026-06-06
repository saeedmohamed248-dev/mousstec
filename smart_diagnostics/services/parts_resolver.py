"""
🔧 DTC → Part Number → Live Stock Resolver

Closes the loop the AI started: the diagnostic session names the parts,
this service surfaces them with real per-branch stock so the advisor can
push them straight onto the Job Card.

Pipeline per VehicleDiagnosticReport:
    1. Dedupe + cap DTCs (max 10 per resolve call — LLM cost guard).
    2. For each DTC, call `predict_parts_from_dtc` (semantic-cached 30 days).
    3. Aggregate by P/N across DTCs:
         • cumulative_probability = max() across appearances (not sum —
           sum overstates confidence when 3 DTCs share one part).
         • dtcs_mentioning = list of source DTCs (so the advisor sees why
           the part was suggested).
    4. Cross-reference each P/N against the tenant's `Product` table
       (case-insensitive, whitespace-stripped).
    5. For matched products, aggregate `Inventory` per branch.

Returns a JSON-friendly list — caller decides whether to render or POST
to /add/ for one-click insertion.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("mouss_tec_core")

_MAX_DTCS_PER_RESOLVE = 10
_MAX_PARTS_RETURNED = 24

# Min probability to surface — anything below is noise.
_MIN_PROBABILITY = 25


def _normalise_pn(pn: str) -> str:
    """Match the workshop's casual P/N formatting against AI output."""
    return (pn or "").strip().upper().replace(" ", "").replace("-", "")


def _aggregate_predictions(dtcs: list[str]) -> "OrderedDict[str, dict]":
    """For each unique DTC call the cached AI predictor, return a dict
    keyed by normalised P/N. The order preserves the highest-probability
    suggestion first."""
    from inventory.ai_services import predict_parts_from_dtc

    per_pn: dict[str, dict] = {}
    unique_dtcs = list(dict.fromkeys(c.strip().upper() for c in dtcs if c))
    for dtc in unique_dtcs[:_MAX_DTCS_PER_RESOLVE]:
        try:
            result = predict_parts_from_dtc(dtc) or {}
        except Exception as exc:
            logger.warning("[parts_resolver] AI predict failed dtc=%s: %s",
                           dtc, exc)
            continue
        for rec in (result.get("recommendations") or [])[:8]:
            pn_raw = rec.get("p_n") or ""
            pn_key = _normalise_pn(pn_raw)
            if not pn_key:
                continue
            try:
                prob = int(rec.get("probability") or 0)
            except (TypeError, ValueError):
                prob = 0
            if prob < _MIN_PROBABILITY:
                continue
            entry = per_pn.get(pn_key)
            if entry is None:
                entry = {
                    "p_n_normalised": pn_key,
                    "p_n_display": pn_raw.strip().upper(),
                    "part_name": (rec.get("part_name") or "").strip(),
                    "probability": prob,
                    "dtcs_mentioning": [dtc],
                }
                per_pn[pn_key] = entry
            else:
                entry["probability"] = max(entry["probability"], prob)
                if dtc not in entry["dtcs_mentioning"]:
                    entry["dtcs_mentioning"].append(dtc)
                # Prefer a populated name over an empty one
                if not entry["part_name"] and rec.get("part_name"):
                    entry["part_name"] = rec["part_name"].strip()

    # Sort: probability DESC, then number of DTCs DESC (multi-DTC matches
    # are stronger signals).
    sorted_items = sorted(
        per_pn.items(),
        key=lambda kv: (-kv[1]["probability"], -len(kv[1]["dtcs_mentioning"])),
    )
    return OrderedDict(sorted_items)


def _attach_stock(predictions: "OrderedDict[str, dict]") -> list[dict]:
    """Cross-reference each P/N against the tenant's `Product` table and
    aggregate per-branch `Inventory`. Mutates entries in place + returns
    a list capped at _MAX_PARTS_RETURNED."""
    from inventory.models import Product, Inventory

    pn_keys = list(predictions.keys())
    if not pn_keys:
        return []

    # Single OR-chained iexact query — Django has no `iexact__in`, but
    # with ≤24 P/Ns this is still one round-trip and the existing
    # `part_number` unique index keeps it cheap.
    from django.db.models import Q
    candidate_pns = {p["p_n_display"] for p in predictions.values()}
    q = Q()
    for pn in candidate_pns:
        q |= Q(part_number__iexact=pn)
    products = list(
        Product.objects.filter(q).only(
            'id', 'name', 'part_number', 'retail_price',
        )
    ) if q else []
    products_by_key = {_normalise_pn(p.part_number): p for p in products}

    # Per-branch stock for matched products only.
    product_ids = [p.id for p in products]
    stock_rows = (Inventory.objects
                  .filter(product_id__in=product_ids)
                  .select_related('branch')
                  .values('product_id', 'branch_id',
                          'branch__name', 'quantity'))
    stock_by_product: dict[int, list[dict]] = {}
    for row in stock_rows:
        stock_by_product.setdefault(row['product_id'], []).append({
            'branch_id': row['branch_id'],
            'branch_name': row['branch__name'],
            'qty': int(row['quantity'] or 0),
        })

    out: list[dict] = []
    for pn_key, entry in predictions.items():
        product = products_by_key.get(pn_key)
        if product is not None:
            branches = stock_by_product.get(product.id, [])
            total_stock = sum(b['qty'] for b in branches)
            entry.update({
                'product_id': product.id,
                'product_name_db': product.name,
                'product_pn_db': product.part_number,
                'retail_price': float(product.retail_price or 0),
                'total_stock': total_stock,
                'by_branch': branches,
                'status': 'in_stock' if total_stock > 0 else 'out_of_stock',
            })
        else:
            entry.update({
                'product_id': None,
                'product_name_db': None,
                'product_pn_db': None,
                'retail_price': None,
                'total_stock': 0,
                'by_branch': [],
                'status': 'not_in_catalogue',
            })
        out.append(entry)
        if len(out) >= _MAX_PARTS_RETURNED:
            break
    return out


def resolve_parts_for_report(report) -> list[dict]:
    """Public entry point — takes a VehicleDiagnosticReport instance
    (or anything with a `.fault_codes` iterable) and returns the ranked
    suggested-parts list with live stock."""
    dtcs = list(getattr(report, 'fault_codes', None) or [])
    if not dtcs:
        return []
    predictions = _aggregate_predictions(dtcs)
    return _attach_stock(predictions)


def resolve_parts_for_job_card(job_card) -> list[dict]:
    """Aggregate suggestions across all diagnostic reports on a single
    Job Card — what the accountant sees on the Review screen."""
    all_dtcs: list[str] = []
    for r in job_card.diagnostic_reports.all():
        all_dtcs.extend(r.fault_codes or [])
    if not all_dtcs:
        return []
    predictions = _aggregate_predictions(all_dtcs)
    return _attach_stock(predictions)
