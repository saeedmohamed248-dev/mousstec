"""
robot/memory.py — what the robot remembers of the conversation in front of it.

Two minutes of it, per robot (the cache, per workshop): the part being talked
about, the parts it just offered, the last few exchanges. So:

* «عندي أكتر من صنف: طرمبة مية (E90)، طرمبة مية (F30). تقصد أنهي؟» →
  «E90» or «التانية» picks it, instead of «ملقتش القطعة دي»;
* «وبكام؟», «فيه أرخص؟», «فيه بديل؟», «عليه ضمان؟», «ماركته إيه؟»,
  «بيركب على E90؟», «مكانها فين؟» are about that part — answered from its
  real data, never guessed;
* a question no rule covers goes to the LLM with what was just said and that
  part's real data in front of it (services.ai_reply) — a price only ever
  from that data.

When a sentence is a follow-up, and the words that answer one, are in
robot/brain/talk.py: the robot's brain uses the same file with the net down.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .brain import talk

logger = logging.getLogger("mouss_tec_core")

TURNS = 4                            # exchanges kept for the LLM
SHOWN_ALTERNATIVES = 6               # in-stock equivalents looked at
QUIET = 30                           # seconds of silence before «يا موس» / a face is a new customer


# ---------------------------------------------------------------------------
# Remembering
# ---------------------------------------------------------------------------

def _key(device) -> str:
    return f"robot:talk:{device.pk}"


# Memory is an extra: a cache that's down (or a value it can't keep) never
# costs the answer itself — the robot just answers each sentence on its own.

def recall(device) -> dict:
    from django.core.cache import cache
    try:
        ctx = cache.get(_key(device)) or {}
    except Exception:
        logger.warning("robot memory: can't recall", exc_info=True)
        return {}
    return ctx if isinstance(ctx, dict) else {}


def remember(device, **fields) -> dict:
    from django.core.cache import cache
    ctx = recall(device)
    ctx.update(fields)
    ctx["at"] = time.time()
    try:
        cache.set(_key(device), ctx, talk.TTL)
    except Exception:
        logger.warning("robot memory: can't remember", exc_info=True)
    return ctx


def forget(device) -> None:
    from django.core.cache import cache
    try:
        cache.delete(_key(device))
    except Exception:
        logger.warning("robot memory: can't forget", exc_info=True)


def note_turn(device, said: str, reply: str) -> None:
    """One exchange, for the LLM's next answer («وده بيعمل إيه؟»)."""
    if not (said or "").strip() or not (reply or "").strip():
        return
    turns = (recall(device).get("turns") or [])[-(TURNS - 1):]
    turns.append([str(said)[:200], str(reply)[:300]])
    remember(device, turns=turns)


def fresh_start(device) -> None:
    """A new customer — the camera greeted someone, or «يا موس» on its own
    after a pause: the last one's part and car aren't theirs. Mid-talk
    (under QUIET seconds) nothing is dropped."""
    ctx = recall(device)
    if ctx and time.time() - float(ctx.get("at") or 0) >= QUIET:
        forget(device)


def last_reply(device) -> str:
    """What the robot said last («قول تاني»)."""
    turns = recall(device).get("turns") or []
    return str(turns[-1][1]) if turns and len(turns[-1]) > 1 else ""


# ---------------------------------------------------------------------------
# The customer's car («عندكم قطع بي ام؟»، or the car of the part they took)
# ---------------------------------------------------------------------------

def car(device) -> dict:
    found = recall(device).get("car")
    return found if isinstance(found, dict) else {}


def hear_car(device, text: str) -> dict:
    """The customer's car once `text` was said (talk.car_in) — kept for the
    next question: «عندك طرمبة مية؟» right after is the pump for it."""
    said = talk.car_in(text)
    if not said:
        return car(device)
    now = talk.with_car(car(device), said)
    remember(device, car=now)
    return now


def took_part(device, product) -> None:
    """The part the customer took is for their car (unless it fits the one they said)."""
    try:
        part_car = talk.car_of(getattr(product, "car_model", "") or "",
                               getattr(product, "chassis_compatibility", None))
    except Exception:            # memory is an extra: never worth the answer itself
        logger.warning("robot memory: no car for %s", getattr(product, "pk", "?"), exc_info=True)
        return
    if part_car:
        remember(device, car=talk.took(car(device), part_car))


def remember_part(device, product_id) -> None:
    """The part now being talked about — the choices offered are answered."""
    if not isinstance(product_id, int):
        return
    remember(device, product_id=product_id, choices=[], restock_for=None)


def remember_choices(device, choices: list, *, keep_part: bool = False) -> None:
    """The parts just offered ([{"id", "name", "model"}]), for «التانية»."""
    fields = {"choices": [{"id": c["id"], "name": str(c.get("name", "")), "model": str(c.get("model", ""))}
                          for c in choices if isinstance(c.get("id"), int)]}
    if not keep_part:
        fields["product_id"] = None
    remember(device, **fields)


def choice(product) -> dict:
    return {"id": product.pk, "name": product.name, "model": (getattr(product, "car_model", "") or "").strip()}


# ---------------------------------------------------------------------------
# Hearing a follow-up
# ---------------------------------------------------------------------------

def follow_up(text: str, device) -> tuple:
    """(product, kind) when `text` follows on what was just said — kind
    "pick" (an answer to «تقصد أنهي؟»), "price", "branches", "where", "warranty",
    "fits", "cheaper", "alternatives", "brand" — else (None, None)."""
    from inventory.models import Product

    ctx = recall(device)
    choices = ctx.get("choices") or []
    i = talk.pick_choice(text, choices)
    if i is not None:
        product = Product.objects.filter(pk=choices[i]["id"], is_active=True).first()
        if product is not None:
            return product, "pick"
    if not ctx.get("product_id"):
        return None, None
    kind = talk.follow_up(text)
    if kind is None:
        return None, None
    product = Product.objects.filter(pk=ctx["product_id"]).first()
    return (product, kind) if product is not None else (None, None)


def talked_about(device):
    """The part the conversation is on, or None."""
    from inventory.models import Product

    pid = recall(device).get("product_id")
    return Product.objects.filter(pk=pid).first() if pid else None


# ---------------------------------------------------------------------------
# Answering one — from the part's own data
# ---------------------------------------------------------------------------

def answer(kind: str, text: str, product, device) -> tuple:
    """(reply, payload) for a follow-up other than its price."""
    from . import assistant, services

    branch = device.branch
    payload = {"action": "follow_up", "kind": kind, "product_id": product.pk}
    if kind == "branches":
        others = assistant.branch_counts(product, branch)
        payload["branches"] = [{"branch": name, "stock": qty} for name, qty in others]
        return talk.branches(product.name, services.branch_stock(product, branch), others)[0], payload
    if kind == "where":
        stock = services.branch_stock(product, branch)
        shelf = services.shelf_location(product, branch) if stock > 0 else ""
        reply, _clips = talk.where(product.name, shelf, stock)
        if stock <= 0:
            names = assistant.other_branches(product, branch)
            if names:
                reply = reply[:-1] + f"، بس موجود في فرع {' وفرع '.join(names)}."
        elif shelf:
            payload["shelf_location"] = shelf
            services.point_to_shelf(device, shelf)
        return reply, payload
    if kind == "warranty":
        return talk.warranty(product.name, getattr(product, "warranty_months", 0))[0], payload
    if kind == "brand":
        return talk.brand(product.name, getattr(product, "brand", ""), getattr(product, "condition", ""),
                          getattr(product, "warranty_months", 0))[0], payload
    if kind == "fits":
        models = talk.models_of(getattr(product, "chassis_compatibility", None),
                                getattr(product, "car_model", ""))
        return talk.fits(product.name, models, talk.models_said(text))[0], payload
    # «فيه بديل؟» / «فيه أرخص؟»: the in-stock equivalents here.
    alts = assistant.alternatives_here(product, branch, limit=SHOWN_ALTERNATIVES)
    reply, _clips, said = talk.alternatives(kind, product.name, product.retail_price or 0,
                                            [(a.name, a.retail_price or 0) for a in alts])
    if said:
        # «التانية» / «بكام الأولاني؟» now picks among them; the part asked
        # about stays the one the conversation is on.
        remember_choices(device, [choice(alts[i]) for i in said], keep_part=True)
        payload["alternatives"] = [{"id": alts[i].pk, "name": alts[i].name,
                                    "retail_price": float(alts[i].retail_price or 0)} for i in said]
    return reply, payload


# ---------------------------------------------------------------------------
# Thinking: what the LLM may say about the part
# ---------------------------------------------------------------------------

def facts(product, branch) -> str:
    """The part being talked about, for the LLM — its retail price only."""
    from .pricing import safe_product_payload
    from .services import spoken_price

    p = safe_product_payload(product, branch=branch)
    lines = [f"القطعة اللي بنتكلم عنها: {p['name']} (رقم {p['part_number']})"]
    price = p.get("retail_price") or 0
    lines.append(f"سعر البيع: {spoken_price(price)}" if price > 0 else "سعر البيع: مش متسجل")
    lines.append(f"المتوفر في الفرع: {p.get('stock', 0)} قطعة")
    brand = (getattr(product, "brand", "") or "").strip()
    condition = talk.CONDITIONS.get(getattr(product, "condition", "") or "", "")
    if brand or condition:
        lines.append("الماركة والحالة: " + "، ".join(x for x in (brand, condition) if x))
    n = int(getattr(product, "warranty_months", 0) or 0)
    lines.append(f"الضمان: {talk.months(n)}" if n > 0 else "الضمان: مش متسجل")
    models = talk.models_of(getattr(product, "chassis_compatibility", None), getattr(product, "car_model", ""))
    if models:
        lines.append("بيركب على: " + "، ".join(models[:8]))
    return "\n".join(lines)


def thinking_context(device) -> tuple:
    """(facts, turns) for services.ai_reply — what was just said, and the
    part it was about."""
    ctx = recall(device)
    product: Optional[object] = talked_about(device) if ctx.get("product_id") else None
    return (facts(product, device.branch) if product is not None else ""), list(ctx.get("turns") or [])
