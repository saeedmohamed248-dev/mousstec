"""
🔍 Smart Parts Finder — VIN → OEM → inventory.Product
=======================================================
بـ ياخد:
  - dtc_code (للحصول على likely_oem_parts من DTCDefinition)
  - vehicle (للـ make/model/year context)
وبـ يـ query inventory.Product بـ:
  - part_number
  - oem_cross_reference (JSONField في الـ Product موجود بالفعل)

يـ return list of (Product, score, in_stock_qty) — مرتبة بـ الـ relevance.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.db.models import Sum, Q

logger = logging.getLogger('mouss_tec_core')


@dataclass
class PartMatch:
    product_id: int
    name: str
    part_number: str
    oem_codes: list
    in_stock_qty: int
    matched_oem: str
    matched_by: str  # 'part_number' | 'oem_cross_reference'


class SmartPartsFinder:
    """Stateless — single entry method find()."""

    @classmethod
    def find_for_dtc(
        cls,
        dtc_code: str,
        vehicle=None,
        limit: int = 20,
    ) -> list[PartMatch]:
        """Pipeline:
          1. resolve DTCDefinition.likely_oem_parts (may be empty)
          2. query inventory.Product by OEM codes
          3. annotate stock from Inventory
        """
        from diagnostics_catalog.models import DTCDefinition
        from inventory.models import Product, Inventory

        defn = DTCDefinition.objects.filter(code=dtc_code.upper()).first()
        oem_codes = (defn.likely_oem_parts if defn else []) or []
        if not oem_codes:
            return []

        # Build a Q query against part_number OR any value inside oem_cross_reference JSON.
        # We can't search inside JSONField list values portably across Postgres versions
        # without GIN indexes — so we fetch candidates by part_number then filter
        # in Python for OEM membership. Tolerable since likely_oem_parts is small.
        part_number_q = Q(part_number__in=oem_codes)
        candidates = list(Product.objects.filter(part_number_q)[:limit])

        # Also pull products whose oem_cross_reference *contains* any of the codes
        # — use icontains on a string cast for compat with both PG/SQLite.
        oem_q = Q()
        for code in oem_codes:
            oem_q |= Q(oem_cross_reference__icontains=code)
        extra = list(
            Product.objects.filter(oem_q)
            .exclude(id__in=[p.id for p in candidates])[:limit]
        )

        product_ids = [p.id for p in candidates + extra]
        stock_map = dict(
            Inventory.objects
            .filter(product_id__in=product_ids)
            .values('product_id')
            .annotate(qty=Sum('quantity'))
            .values_list('product_id', 'qty')
        )

        matches: list[PartMatch] = []
        for p in candidates:
            matches.append(PartMatch(
                product_id=p.id,
                name=p.name,
                part_number=p.part_number,
                oem_codes=p.oem_cross_reference or [],
                in_stock_qty=int(stock_map.get(p.id, 0) or 0),
                matched_oem=p.part_number,
                matched_by='part_number',
            ))
        for p in extra:
            xref = p.oem_cross_reference or []
            matched = next((c for c in oem_codes if c in (xref if isinstance(xref, list) else [])), '')
            matches.append(PartMatch(
                product_id=p.id,
                name=p.name,
                part_number=p.part_number,
                oem_codes=xref if isinstance(xref, list) else [],
                in_stock_qty=int(stock_map.get(p.id, 0) or 0),
                matched_oem=matched or (oem_codes[0] if oem_codes else ''),
                matched_by='oem_cross_reference',
            ))

        # Sort: in_stock first, then by name
        matches.sort(key=lambda m: (-m.in_stock_qty, m.name))
        return matches[:limit]
