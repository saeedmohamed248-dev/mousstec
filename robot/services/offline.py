"""📴 خدمات الروبوت — كتالوج الشغل من غير نت ومزامنة أحداث الأوفلاين."""

from __future__ import annotations


import logging

from django.db import transaction
from django.utils import timezone

from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .stock_take import add_stock_take_count, complete_stock_take, start_stock_take

logger = logging.getLogger("mouss_tec_core")


# ---------------------------------------------------------------------------
# Offline sync — catalog for the robot to work on when the net is down
# ---------------------------------------------------------------------------

def _outage_alert(device, payload: dict):
    """Tell the owner the robot worked through an internet outage, and what it
    kept for later (sent by each board once its backlog is uploaded)."""
    from datetime import datetime, timezone as dt_timezone
    from ..models import RobotAlert

    def when(key):
        try:
            ts = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            return None
        if ts < 1_600_000_000:
            return None
        return timezone.localtime(datetime.fromtimestamp(ts, tz=dt_timezone.utc))

    start, end = when("from"), when("to")
    if start and end:
        span = f"من {start:%H:%M} لـ {end:%H:%M}"
    else:
        try:
            minutes = max(1, int(payload.get("seconds") or 0) // 60)
        except (TypeError, ValueError):
            minutes = 1
        span = f"حوالي {minutes} دقيقة"
    kept = []
    try:
        clips, photos = int(payload.get("clips") or 0), int(payload.get("photos") or 0)
    except (TypeError, ValueError):
        clips = photos = 0
    if clips:
        kept.append(f"سجّل اللي اتقاله ({clips} تسجيل)")
    if photos:
        kept.append(f"الكاميرا صوّرت {photos} لقطة حركة")
    detail = ("، و".join(kept) + "، واتبعتوا للسيستم دلوقتي.") if kept else "ومحدش كلّمه."
    RobotAlert.objects.create(
        device=device, kind="back_online",
        message=f"📵 الروبوت اشتغل من غير نت {span}. {detail}",
    )


def offline_catalog(branch, *, limit: int = 20000) -> dict:
    """A compact, RETAIL-ONLY snapshot the robot caches to work offline.

    Parts (name, codes, retail price, branch stock) + known customer/face hashes
    so it can still answer stock/price and recognize people without the network.
    NEVER includes wholesale/cost — same guarantee as every robot-facing payload.
    """
    from inventory.models import Branch, Inventory, Product

    parts = []
    # الفروع التانية: «بس موجود في فرع X» والجهاز من غير نت، وصفحة الكاشير لما تبيع لفرع تاني —
    # الأكتر الأول زي robot/assistant.branch_counts.
    elsewhere = {}
    for pid, bid, bname, qty in (Inventory.objects.filter(quantity__gt=0).exclude(branch=branch)
                                 .order_by("-quantity", "branch__name")
                                 .values_list("product_id", "branch_id", "branch__name", "quantity")):
        elsewhere.setdefault(pid, []).append({"id": bid, "name": bname, "stock": int(qty)})
    # 🐛 [FIX]: كان `[:limit]` من غير ترتيب، فالمحل اللي عنده أكتر من 5000 صنف كان بيقع منه
    #    أصناف عشوائي من غير ما حد يعرف. دلوقتي اللي في المخزون الأول، و`part_total` بيقول العدد الكامل.
    rows = Inventory.objects.filter(branch=branch)
    inv_rows = list(rows.select_related("product").order_by("-quantity", "product_id")[:limit])
    here = {inv.product_id for inv in inv_rows}
    # What only other branches have («موجود في فرع X» offline too), after this branch's own.
    away = [pid for pid in elsewhere if pid not in here][:max(0, limit - len(inv_rows))]
    inv_rows += [Inventory(product=p, branch=branch, quantity=0)
                 for p in Product.objects.filter(pk__in=away).order_by("pk")]
    for inv in inv_rows:
        p = inv.product
        chassis = getattr(p, "chassis_compatibility", None)
        parts.append({
            "part_number": p.part_number,
            "name": p.name,
            # The brain answers offline the way the server does: the car
            # model breaks ties between same-name parts and labels the
            # choices, a barcode said or scanned finds the part.
            "car_model": getattr(p, "car_model", "") or "",
            "barcode": getattr(p, "barcode", "") or "",
            "active": bool(getattr(p, "is_active", True)),
            "oem": getattr(p, "oem_cross_reference", None) or [],
            "additional": getattr(p, "additional_part_numbers", None) or [],
            "retail_price": float(p.retail_price or 0),
            "stock": inv.quantity,
            # Follow-ups offline (robot/brain/talk.py): «عليه ضمان؟»,
            # «ماركته إيه؟», «بيركب على E90؟», «مكانها فين؟».
            "brand": getattr(p, "brand", "") or "",
            "condition": getattr(p, "condition", "") or "",
            "warranty_months": int(getattr(p, "warranty_months", 0) or 0),
            "chassis": [str(c) for c in chassis if c] if isinstance(chassis, (list, tuple)) else [],
            "shelf": (inv.shelf_location or "").strip(),
            "elsewhere": elsewhere.get(p.pk, []),
        })
    # What staff have taught the robot (shop slang / read codes → part), so it
    # keeps understanding those words offline too.
    from ..models import RobotKnowledge
    aliases = [
        {"kind": kind, "key": key, "part_number": pn}
        for kind, key, pn in (RobotKnowledge.objects
                              .filter(key_kind__in=("code", "label"))
                              .order_by("-hit_count")
                              .values_list("key_kind", "key_value", "product__part_number")[:limit])
    ]
    # Who may use the counter page with the net down, and for what (PIN hashes).
    from .. import teachers
    from ..offline_ledger import staff_for
    from ..replies import for_branch
    lessons = teachers.lessons()
    return {
        "branch": branch.name,
        "branch_id": branch.pk,
        # Every branch: the counter page records on the one picked (robot/brain/counter.py).
        "branches": [{"id": b.pk, "name": b.name} for b in Branch.objects.order_by("name")],
        "shop": services.shop_name(),                 # the receipt on the counter page: the shop's, not the platform's
        "generated_at": timezone.now().isoformat(),
        "parts": parts,
        "part_count": len(parts),
        "part_total": rows.count(),
        "aliases": aliases,
        "staff": staff_for(branch),
        # «الردود الجاهزة»: the hours, the address … said with the net down too.
        "replies": [{k: r[k] for k in ("topic", "questions", "answer")} for r in for_branch(branch)],
        # «🎓 تعلّم السمع»: what the robot's ears write, and what was said —
        # a word or a few, whole sentences (answered at once, no teacher), and the
        # words its ears write right (a sentence of them: at once too).
        "heard_fixes": teachers.fixes(),
        "heard_sentences": _heard_sentences(lessons),
        "heard_trusted": lessons["trusted"],
        "heard_off": lessons.get("off", []),
    }


def _heard_sentences(lessons: dict) -> list:
    """Whole sentences «مخ موس» knows: the teachers' (what was said), then what «🧠 مخ الفهم»
    learned they mean (robot/understand.py) — understood offline too, at once online."""
    from .. import understand

    out = [{"heard": s["heard"], "means": s["means"]} for s in lessons["sentences"]]
    have = {s["heard"] for s in out} | set(lessons.get("off", []))
    try:
        learned = understand.lessons()
    except Exception:                       # the catalog goes on without them
        logger.warning("robot catalog: no understanding lessons", exc_info=True)
        learned = []
    return out + [s for s in learned if s["heard"] not in have]


def catalog_tag(catalog: dict) -> str:
    """An ETag for `catalog`: the same stock, prices, names and PINs → the same
    tag, whenever it was made (`generated_at` aside)."""
    import hashlib
    import json

    body = json.dumps({k: v for k, v in catalog.items() if k != "generated_at"},
                      sort_keys=True, ensure_ascii=False, default=str)
    return '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:32] + '"'


@transaction.atomic
def apply_offline_events(device, events: list) -> dict:
    """Replay a batch of offline events idempotently (by client_uid).

    Each event: {client_uid, kind, payload}. Supported kinds:
      * learn      — payload {code|label, part_number}  → learn_from_confirmation
      * count      — payload {session? , query, counted_qty} (best-effort log)
      * outage     — payload {from, to (Unix s), clips, photos}: a summary of
                     a stretch the robot worked offline, raised as an alert
      * restock    — payload {part_number, phone}: a customer who left a
                     number offline for a part that's out (robot/restock.py)
      * sale       — payload {employee_id, items: [{part_number, qty,
                     unit_price}], customer_name?, receipt, at}: a cash sale
                     on the counter page (robot/offline_ledger.py)
      * expense    — payload {employee_id, amount, description, at}
    A sale or expense the books can't take any more is kept as a failed
    RobotEntry for the manager (applied — never retried into a double).
    Unknown kinds are stored but not applied. Duplicates (same client_uid) are
    skipped so a retried upload never double-applies.
    """
    from inventory.models import Product
    from ..models import RobotSyncEvent

    applied, skipped, failed = 0, 0, 0
    count_session = None
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        uid = str(ev.get("client_uid") or "").strip()[:64]
        if not uid:
            continue
        kind = str(ev.get("kind") or "")[:32]
        payload = ev.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        row, created = RobotSyncEvent.objects.get_or_create(
            device=device, client_uid=uid,
            defaults={"kind": kind, "payload": payload},
        )
        if not created and row.applied:
            skipped += 1
            continue
        if kind == "count" and count_session is None:
            # Created OUTSIDE the event's savepoint: if that event fails, its
            # rollback must not delete the session later counts still use.
            count_session = start_stock_take(
                device, device.branch, instruction="جرد أوفلاين (مزامنة)",
            )
        try:
            # A savepoint per event: one bad event rolls back only itself.
            # Catching a DB error without one would leave the whole batch's
            # transaction broken and fail every event after it.
            with transaction.atomic():
                if kind == "learn":
                    pn = (payload.get("part_number") or "").strip()
                    product = services.find_product(pn) if pn else None
                    if product:
                        services.learn_from_confirmation(
                            product=product,
                            code=payload.get("code", ""),
                            label=payload.get("label", ""),
                        )
                elif kind == "outage":
                    _outage_alert(device, payload)
                elif kind == "restock":
                    from .. import restock
                    from ..brain import talk
                    pn = str(payload.get("part_number") or "").strip()
                    phone = talk.mobile_in(str(payload.get("phone") or ""))
                    product = Product.objects.filter(part_number=pn).first() if pn else None
                    if product is None or not phone:
                        raise ValueError("restock: unknown part or number")
                    restock.register(device, product, phone)
                elif kind == "sale":
                    from .. import offline_ledger
                    offline_ledger.apply_sale(device, payload)
                elif kind == "expense":
                    from .. import offline_ledger
                    offline_ledger.apply_expense(device, payload)
                elif kind == "count":
                    # Counts taken offline land in one stock-take per sync
                    # batch. Like any robot count it only proposes numbers: a
                    # manager still approves the adjustment from the dashboard.
                    query = str(payload.get("query") or "").strip()
                    qty = int(payload.get("counted_qty"))
                    if query and qty >= 0:
                        add_stock_take_count(count_session, query=query, counted_qty=qty)
                row.applied = True
                row.save(update_fields=["applied"])
            applied += 1
        except Exception:
            # Leave unapplied so a later sync can retry.
            failed += 1
    report = None
    if count_session is not None:
        report = complete_stock_take(count_session)
    return {"applied": applied, "skipped_duplicates": skipped,
            "failed": failed, "stock_take": report}


def _open_rfq_for(product, branch, quantity: int):
    """Open an RFQ for a low-stock part, or return None. Deduplicates on open."""
    try:
        from inventory.models import RFQ
    except Exception:
        return None
    existing = RFQ.objects.filter(
        product=product, branch=branch, status=RFQ.STATUS_OPEN,
    ).first()
    if existing:
        return existing.id
    rfq = RFQ.objects.create(
        branch=branch,
        product=product,
        part_number_requested=product.part_number,
        part_name_requested=product.name,
        quantity=max(int(quantity), 1),
        notes="🤖 طلب تلقائي من الروبوت — المخزون وصل حد التنبيه.",
        status=RFQ.STATUS_OPEN,
    )
    return rfq.id
