"""
robot/services.py — business logic bridging the hardware to the Mouss Tec ERP.

Pure functions/helpers (no HTTP here — see views.py). Everything that touches a
product price goes through `robot.pricing.safe_product_payload` so wholesale/cost
can never leak to the robot.

Integrations:
  * inventory.Product / Inventory / Branch / SaleInvoice / StockAlert
  * hr.Employee / AttendanceRecord (via robot.security)
  * diagnostics_catalog.DTCDefinition — fault codes ("قرابات الأعطال")
  * bmw_ecu.EcuHardwareProfile / EcuPinoutDiagram — ECU coding/programming refs
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import logging
import math
import os
import re

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import language
from .assistant import part_codes
from .brain import slang, talk
from .brain.arabic_numbers import CODE_IN_TEXT, spoken_codes
from .pricing import safe_product_payload

logger = logging.getLogger("mouss_tec_core")


def shop_name() -> str:
    """The shop's own name — the account's (`connection.tenant.name`) — or ''.

    🐛 [FIX]: the robot said «منورنا في Mouss Tec» to Fixit's customers:
    Mouss Tec is the platform, the shop has its own name, and the robot is
    sold to other shops too."""
    from django.db import connection

    tenant = getattr(connection, "tenant", None)
    if tenant is None or getattr(tenant, "schema_name", "public") == "public":
        return ""
    return str(getattr(tenant, "name", "") or "").strip()


def welcome() -> str:
    """«أهلاً بيك في Fixit» — or «أهلاً بيك» when there's no shop name."""
    shop = shop_name()
    return f"أهلاً بيك في {shop}" if shop else "أهلاً بيك"


# ---------------------------------------------------------------------------
# Part / inventory lookup (branch-aware, retail-only)
# ---------------------------------------------------------------------------

def find_product(query: str):
    """Best-effort match of a scanned code or spoken description to a Product.

    Order: exact part_number / barcode → OEM & additional part numbers → name
    contains. Returns a Product or None. Never returns pricing directly — callers
    serialize via `safe_product_payload`.
    """
    from inventory.models import Product

    q = (query or "").strip()
    if not q:
        return None

    # 1) Exact code hits (part_number, barcode).
    exact = Product.objects.filter(
        Q(part_number__iexact=q) | Q(barcode__iexact=q)
    ).first()
    if exact:
        return exact

    # 2) OEM cross-reference / additional part numbers (JSON contains).
    #    icontains on the JSON text is a pragmatic match across DBs.
    code_hit = Product.objects.filter(
        Q(oem_cross_reference__icontains=q) | Q(additional_part_numbers__icontains=q)
    ).first()
    if code_hit:
        return code_hit

    # 3) Name / model description — only when it can't be another part.
    # 🐛 [FIX]: كانت بترجع أول صنف اسمه فيه الكلام: «الزيت» جابت «خرطوم علوي من موتور لمبرد
    # الزيت» وقالت سعره، و«بعت 2 فلتر» كانت ممكن تمسك أي فلتر. دلوقتي الاسم كله، أو صنف واحد بس
    # والكلام معظم اسمه؛ غير كده None، واللي بينادي بيسأل «تقصد أنهي؟» (match_part_in_sentence).
    named = Product.objects.filter(name__iexact=q).first()
    if named:
        return named
    hits = list(Product.objects.filter(Q(name__icontains=q) | Q(car_model__icontains=q))[:2])
    if len(hits) == 1 and 2 * len(q.split()) >= len((hits[0].name or "").split()):
        return hits[0]
    return None


def inventory_answer(query: str, branch=None, car=None) -> dict:
    """Answer a stock/price question (voice assistant or lookup scan).

    Returns a robot-safe dict: found flag + retail-only product payload, or a
    not-found marker. This is what the workshop assistant speaks back.
    `car`: the customer's car from the conversation (robot/memory.py) — the
    parts for it first when several fit the words.
    """
    # Taught mappings first (exact), then the catalogue, then any shop-slang
    # alias staff taught that appears inside the sentence.
    product = (resolve_from_knowledge(code=query, label=query)
               or find_product(query)
               or learned_alias_in(query))
    choices = []
    if not product:
        product, choices = match_part_in_sentence(query, car=car)
    if not product:
        out = {"found": False, "query": query}
        if choices:
            out["choices"] = [_choice_label(p) for p in choices[:3]]
            # For the robot's memory (robot/memory.py): «التانية» or «E90»
            # answers «تقصد أنهي؟». Taken out before the answer is sent.
            out["choice_parts"] = [{"id": p.pk, "name": p.name,
                                    "model": (getattr(p, "car_model", "") or "").strip()}
                                   for p in choices[:3]]
        return out
    payload = safe_product_payload(product, branch=branch)
    payload["found"] = True
    return payload


# ---------------------------------------------------------------------------
# A part named inside a spoken sentence («يا موس، عندك فلتر زيت E90؟»)
# ---------------------------------------------------------------------------

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ARTICLES = ("وال", "بال", "فال", "لل", "ال")
# Words that frame a question rather than name the part (already normalized:
# ة→ه, ى→ي, أ/إ→ا, «ال» dropped).
_QUESTION_WORDS = frozenset("""
    يا عندك عندكو عندكم عندكوا فيه في هل كام بكام وبكام سعر سعره سعرها بسعر تمن
    تمنه تمنها بتاع بتاعه بتاعت عايز عاوز عايزه عاوزه محتاج محتاجه ممكن لو سمحت
    فضلك من على عن لي ليا ده دي دا دول كده موجود موجوده متوفر متوفره متاح ايه اي
    اللي انا احنا انت عربيه عربيتي قطعه حته و او مع هو هي طب طيب بي ام دبليو
    do you have got any how much is the a an price of for please what bmw
""".split()) | slang.FILLERS                 # «عامل كام يا باشا؟»: the shop's own way of asking


def _part_words(text: str) -> list:
    """Normalized words of a sentence or a product name, articles dropped,
    two-word shop names made one («بريك باد» → «تيل», robot/brain/slang.py)."""
    from .wakename import normalize

    words = []
    for w in slang.rewrite(normalize((text or "").translate(_ARABIC_DIGITS))).split():
        for article in _ARTICLES:
            if w.startswith(article) and len(w) - len(article) >= 2:
                w = w[len(article):]
                break
        words.append(w)
    return words


def part_keys(text: str) -> list:
    """The words of a sentence that could name a part — not the question
    around it: «عندك؟», «بكام؟» have none."""
    return [w for w in dict.fromkeys(_part_words(text)) if len(w) >= 2 and w not in _QUESTION_WORDS]


def names_a_part(text: str) -> bool:
    """Whether a sentence nothing was found for asked for a part (talk.asks_for_a_part,
    on the products here) — «ملقتش القطعة» only then."""
    from inventory.models import Product

    def in_names(words):
        name_q = Q()
        for k in words[:6]:
            name_q |= Q(name__icontains=k)
        return Product.objects.filter(name_q).exists()

    return talk.asks_for_a_part(text, part_keys(text), in_names)


def _spellings(word: str) -> set:
    """How a normalized word may be stored in a product name (ه/ة, ي/ى, ا/أ/إ)."""
    out = {word}
    if word.endswith("ه"):
        out.add(word[:-1] + "ة")
    if word.endswith("ي"):
        out.add(word[:-1] + "ى")
    if word.startswith("ا"):
        out |= {"أ" + word[1:], "إ" + word[1:]}
    return out


def _forms(word: str) -> set:
    """The word, without a written case ending (Vosk heard «فلتر زيت» as
    «فلترا زيت»), and what an Egyptian shop calls the same part: «كاوتش» is
    the catalogue's «إطار», «بوجيهات» its «شمعات» (robot/brain/slang.py)."""
    return slang.forms(word)


def _choice_label(product) -> str:
    model = (getattr(product, "car_model", "") or "").strip()
    return f"{product.name} ({model})" if model and model not in product.name else product.name


def match_part_in_sentence(text: str, car=None):
    """The product a spoken question is about → (product, []) or (None, choices).

    `find_product` only matches when the WHOLE sentence is a part number or
    sits inside a product name, so «عندك فلتر زيت؟» found nothing and the
    robot never said a price. Here a part number said anywhere in the
    sentence wins; otherwise the words that aren't question words are matched
    against product names (the car model breaks ties). When several parts fit
    equally — or the only one fits weakly — nothing is picked: a wrong part
    means a wrong price spoken, so the caller asks which one is meant.

    `car` ({"make", "models"}: the customer's, robot/memory.py) breaks what
    the words leave tied: «عندك طرمبة مية؟» after «عندكم قطع بي ام؟» offers
    the BMW pumps, not the Mercedes one; after a part for an F30, the F30's.
    """
    from inventory.models import Product

    words = _part_words(text)
    # 1) A part number said inside the sentence (BMW numbers are often said
    #    in groups: «11 42 7 953 125»).
    codes = [w for w in words if len(w) >= 5 and any(c.isdigit() for c in w)]
    digits = "".join(w for w in words if w.isdigit())
    if len(digits) >= 7:
        codes.append(digits)
    # 🐛 [FIX]: رقم القطعة اللي بيتقال بالكلام («واحد واحد اربعة اتنين…» أو «حداشر اتنين
    # واربعين…» زي ما Vosk بيكتبه) كان بيتدوّر عليه بكلمات «واحد»، «اربعة» ومبيتلاقيش.
    codes += [c for c in spoken_codes(text) if c not in codes]
    for code in codes:
        hit = Product.objects.filter(Q(part_number__iexact=code) | Q(barcode__iexact=code)).first()
        if hit:
            return hit, []
    # The number on the old part's box: an OEM or other number it's known by,
    # whole (the brain matches the same way: knowledge.Knowledge._in_sentence).
    for code in (c for c in codes if len(c) >= CODE_IN_TEXT):
        for product in (Product.objects.filter(Q(oem_cross_reference__icontains=code)
                                               | Q(additional_part_numbers__icontains=code))
                        .order_by("part_number")[:20]):             # several: the lowest number (the brain too)
            if code.lower() in part_codes(product):
                return product, []

    # 2) Name words.
    keys = [w for w in dict.fromkeys(words) if len(w) >= 2 and w not in _QUESTION_WORDS]
    if not keys:
        return None, []
    name_q = Q()
    for w in keys[:8]:
        for spelling in {s for form in _forms(w) for s in _spellings(form)}:
            name_q |= Q(name__icontains=spelling)
    scored = []
    for product in Product.objects.filter(name_q, is_active=True)[:200]:
        name_words = set(_part_words(product.name))
        hits = sum(1 for w in keys if _forms(w) & name_words)
        if not hits:
            continue
        model_words = set(_part_words(getattr(product, "car_model", "") or ""))
        model_hits = sum(1 for w in keys if _forms(w) & model_words and not _forms(w) & name_words)
        # The car written in the name counts too («فلتر زيت - E90 - JP»): an E90 asks for that one.
        fits_car = talk.fit(car, f"{getattr(product, 'car_model', '') or ''} {product.name}",
                            getattr(product, "chassis_compatibility", None)) if car else 0
        scored.append(((hits, model_hits, fits_car), hits / max(len(name_words), 1), product))
    if not scored:
        return None, []
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    best = [s for s in scored if s[0] == scored[0][0]]
    (hits, _, _), coverage, product = best[0]
    if len(best) == 1 and (hits >= 2 or coverage >= 0.5):
        return product, []
    return None, [s[2] for s in best]


def spoken_price(value) -> str:
    """A retail price the way the robot says it: «450 جنيه», «450 جنيه و50 قرش».

    Never rounded — «{price:.0f}» said 451 for 450.50.
    """
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pounds, piasters = divmod(int(amount * 100), 100)
    return f"{pounds} جنيه" + (f" و{piasters} قرش" if piasters else "")


# ---------------------------------------------------------------------------
# Scrap condition → dynamic RETAIL price suggestion
# ---------------------------------------------------------------------------

def suggest_used_price(product, condition_score: float, calibration: float = 1.0) -> Decimal:
    """Suggest a RETAIL selling price for a used part from its condition.

    `condition_score` ∈ [0,1] (0 = destroyed, 1 = like-new) comes from the vision
    wear analysis. We interpolate between the scrap floor and the AI/retail
    ceiling — deliberately anchored to RETAIL figures only, never wholesale.

    ceiling = ai_suggested_price if set else retail_price
    floor   = scrap_price
    price   = floor + condition * (ceiling - floor)
    """
    score = max(0.0, min(1.0, float(condition_score)))
    retail = Decimal(str(product.retail_price or 0))
    ai = Decimal(str(getattr(product, "ai_suggested_price", 0) or 0))
    ceiling = ai if ai > 0 else retail
    floor = Decimal(str(getattr(product, "scrap_price", 0) or 0))
    if ceiling <= 0:
        return floor
    if floor > ceiling:
        floor = ceiling
    price = floor + (Decimal(str(score)) * (ceiling - floor))
    # Learned from what used parts actually sold for (see
    # `used_price_calibration`) — but never outside the retail floor/ceiling.
    if calibration and calibration != 1.0:
        price = min(ceiling, max(floor, price * Decimal(str(calibration))))
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Learned used-price correction is bounded: history nudges the suggestion, it
# can't swing it wildly off one odd sale.
_CALIBRATION_MIN, _CALIBRATION_MAX = 0.7, 1.3
_CALIBRATION_MIN_SALES = 5


def used_price_calibration(*, last: int = 50) -> float:
    """How the robot's used-part suggestions compare with real selling prices.

    Looks at recent scrap scans that turned into sales and takes the median of
    (price actually sold at ÷ price the robot suggested). Staff who keep
    selling above the suggestion teach the robot to suggest higher, and vice
    versa. Returns 1.0 until there are enough sales to learn from.
    """
    from inventory.models import SaleInvoiceItem
    from .models import RobotScanEvent

    ratios = []
    # Compare against the RAW suggestion (before any calibration): measuring
    # against the already-calibrated price would cancel the correction out
    # and make it swing back and forth instead of settling.
    events = (RobotScanEvent.objects
              .filter(purpose="scrap", suggested_price__gt=0,
                      sale_invoice__isnull=False, product__isnull=False)
              .order_by("-created_at")[:last])
    for ev in events:
        sold = (SaleInvoiceItem.objects
                .filter(invoice_id=ev.sale_invoice_id, product_id=ev.product_id)
                .values_list("unit_price", flat=True).first())
        raw = (ev.condition_notes or {}).get("raw_suggested_price") or ev.suggested_price
        if sold and float(raw) > 0:
            ratios.append(float(sold) / float(raw))
    if len(ratios) < _CALIBRATION_MIN_SALES:
        return 1.0
    ratios.sort()
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return max(_CALIBRATION_MIN, min(_CALIBRATION_MAX, median))


def branch_stock(product, branch) -> int:
    """On-hand quantity of `product` at `branch` (0 when never stocked)."""
    from inventory.models import Inventory
    qty = (Inventory.objects.filter(product=product, branch=branch)
           .values_list("quantity", flat=True).first())
    return int(qty or 0)


def shelf_location(product, branch) -> str:
    """Where the part sits in this branch ('' when not recorded)."""
    from inventory.models import Inventory
    loc = (Inventory.objects.filter(product=product, branch=branch)
           .values_list("shelf_location", flat=True).first())
    return (loc or "").strip()


def point_to_shelf(device, location: str, *, issued_by=None):
    """Turn the head toward a shelf using the device's `shelf_map`.

    The longest shelf-map key that prefixes `location` wins ("B3" beats "B").
    Returns the queued MotorCommandLog, or None when the shelf isn't mapped.
    """
    from .models import MotorCommandLog

    loc = (location or "").strip().upper()
    shelf_map = device.shelf_map or {}
    best = ""
    for key in shelf_map:
        k = str(key).strip().upper()
        if k and loc.startswith(k) and len(k) > len(best):
            best = key
    if not best:
        return None
    spec = shelf_map[best] or {}
    try:
        actuator, direction, ms = validate_motor_command(
            "head", spec.get("direction"), spec.get("ms"))
    except ValueError:
        return None
    if direction == "stop":
        return None
    return MotorCommandLog.objects.create(
        device=device, actuator=actuator, direction=direction,
        duration_ms=ms, issued_by=issued_by,
    )


# ---------------------------------------------------------------------------
# Predictive maintenance — the robot watches its own motors
# ---------------------------------------------------------------------------

# Stall-level current per actuator (amps). Above this the motor is jammed or
# at its end stop; the Mega also cuts it in firmware. Tune to your motors.
MOTOR_CURRENT_LIMIT_A = {"head": 8.0, "arm_left": 10.0, "arm_right": 10.0, "track": 15.0}
# A motor drawing this much more than its own running average is wearing out
# (bearings/brushes/gearbox) — worth a look before it fails.
_WEAR_RATIO = 1.5
_WEAR_MIN_SAMPLES = 20


def record_motor_currents(device, currents: dict) -> list:
    """Fold per-actuator current readings into `device.motor_health`.

    Keeps a running average per motor and raises a (deduped) `motor_fault`
    alert on a stall-level reading or a sustained rise above the motor's own
    baseline. Returns the alerts raised.
    """
    from .models import RobotAlert

    health = dict(device.motor_health or {})
    alerts = []
    now = timezone.now()
    for actuator, value in (currents or {}).items():
        if actuator not in MOTOR_DIRECTIONS:
            continue
        try:
            amps = float(value)
        except (TypeError, ValueError):
            continue
        if amps < 0:
            continue
        h = dict(health.get(actuator) or {})
        n = int(h.get("samples", 0))
        avg = float(h.get("avg_a", 0.0))
        problem = None
        if amps >= MOTOR_CURRENT_LIMIT_A[actuator]:
            problem = f"⚠️ موتور {actuator} سحب {amps:.1f} أمبير (زنقة/نهاية مشوار) — اتفصل."
        elif n >= _WEAR_MIN_SAMPLES and avg > 0 and amps > avg * _WEAR_RATIO:
            problem = (f"🔧 موتور {actuator} بيسحب {amps:.1f} أمبير والمعتاد {avg:.1f} — "
                       "غالباً محتاج صيانة (تزييت/فحم/جيربوكس).")
        else:
            # Only healthy readings update the baseline, so a failing motor
            # doesn't slowly teach the robot that failing is normal.
            h["avg_a"] = round((avg * n + amps) / (n + 1), 3)
            h["samples"] = n + 1
        h["last_a"] = amps
        h["at"] = now.isoformat()
        health[actuator] = h
        if problem:
            recent = RobotAlert.objects.filter(
                device=device, kind="motor_fault", message__contains=actuator,
                created_at__gte=now - timedelta(minutes=30),
            ).exists()
            if not recent:
                alerts.append(RobotAlert.objects.create(
                    device=device, kind="motor_fault", message=problem[:255],
                ))
    device.motor_health = health
    device.save(update_fields=["motor_health"])
    return alerts


def motor_runtime_ms(device, *, days: int = 30) -> dict:
    """Total commanded run time per actuator (ms) — wear counter for the page."""
    from django.db.models import Sum
    from .models import MotorCommandLog
    rows = (MotorCommandLog.objects
            .filter(device=device, created_at__gte=timezone.now() - timedelta(days=days))
            .exclude(direction="stop")
            .values("actuator").annotate(total=Sum("duration_ms")))
    return {r["actuator"]: int(r["total"] or 0) for r in rows}


# ---------------------------------------------------------------------------
# Conversational fallback (LLM) — general questions the router doesn't cover
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = (
    "أنت «{robot}»، روبوت مساعد في محل{shop} لقطع غيار BMW و MINI. رد بالعامية "
    "المصرية في جملة أو اتنين قصيرين لأن ردك هيتقال بصوت. ممنوع تذكر أي سعر أو "
    "تكلفة أو سعر جملة (غير سعر البيع اللي في بيانات القطعة لو اتبعتتلك)، وممنوع "
    "تخترع توفر قطعة أو رقمها: لو السؤال عن قطعة "
    "معينة قول إنك هتسأل حد من الموظفين أو اطلب رقم القطعة. ساعد في الأسئلة "
    "العامة عن الأعطال والصيانة والمواعيد بشكل عام. ولو الكلام دردشة عامة، اتكلم معاه "
    "عادي بلطف وادي معلومة مفيدة لو عندك، من غير ما توعد بحاجة. متوعدش بحاجة مش مكتوبة قدامك "
    "(إن القطع أصلية، خصم، توصيل، تركيب)."
)
# The answer is said while the customer waits: one model this long, all of them this long.
REPLY_TIMEOUT_S = 5
REPLY_BUDGET_S = 7
# 🐛 [FIX]: لما Gemini مردّش (حصته أو موديل بطيء) البوابة العامة كانت بتتسأل بعده وتاخد ٤ ثواني كمان
# وترجع فاضي: ١١–١٢ ثانية والزبون واقف. البوابة بس لو Gemini قال «لأ» بسرعة (مفيش مفتاح مثلاً).
GATEWAY_IF_UNDER_S = 2
_WEEKDAYS = ("الاتنين", "التلات", "الأربع", "الخميس", "الجمعة", "السبت", "الحد")


def _shop_and_time() -> dict:
    """What the prompts are filled with: the shop's own name, and «النهارده
    الحد، والساعة 4:10 العصر» — «الساعة كام؟» and «فاتحين دلوقتي؟» need it."""
    shop = shop_name()
    now = timezone.localtime()
    hour = now.hour % 12 or 12
    part = ("الصبح" if now.hour < 12 else "الضهر" if now.hour < 15
            else "العصر" if now.hour < 18 else "بالليل")
    return {"shop": f" «{shop}»" if shop else "",
            "now": f" النهارده {_WEEKDAYS[now.weekday()]} {now:%d/%m/%Y}، والساعة {hour}:{now:%M} {part}."}


_AI_ENGLISH = (
    " The customer spoke English: reply in short, natural spoken English "
    "instead, with the same rules."
)

# With the part being talked about in front of it (robot/memory.facts).
_AI_FACTS_PROMPT = (
    "\n\nدي بيانات القطعة اللي العميل بيتكلم عنها، من المخزون نفسه. اعتمد عليها "
    "بس: أي سعر تقوله لازم يكون «سعر البيع» المكتوب هنا بالظبط، ومتقولش أي رقم "
    "مش مكتوب. ولو حاجة مش مكتوبة قول إنك هتسأل حد من الموظفين. تقدر تقنع العميل "
    "بالحاجات الحقيقية اللي هنا بس (الضمان، الماركة، التوافق، إنها متوفرة) — من "
    "غير مبالغة ولا وعود ولا خصومات. ولو السؤال عن قطعة تانية، اطلب اسمها أو رقمها.\n"
)

_CURRENCY = re.compile(r"جنيه|جنية|جنيهات|ج\.م|قرش|pounds?|egp|\ble\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def invented_price(reply: str, allowed: str) -> bool:
    """True when `reply` says a sum of money that isn't in `allowed` — the
    part's own data and what the customer said. A price the robot speaks is
    only ever the catalog's."""
    text = (reply or "").translate(_ARABIC_DIGITS)
    known = {n.replace(",", "") for n in _NUMBER.findall((allowed or "").translate(_ARABIC_DIGITS))}
    for m in _NUMBER.finditer(text):
        near = text[max(0, m.start() - 12):m.end() + 14]
        if _CURRENCY.search(near) and m.group().replace(",", "") not in known:
            return True
    return False


def ai_reply(transcript: str, lang: str = "ar", *, facts: str = "", history=(), branch: str = "",
             robot: str = "موس", system: str = "", longest: int = 400) -> str:
    """A short spoken answer from the ERP's LLM gateway, or '' if unavailable.

    Used only when no rule matched. In Egyptian Arabic, or in English when the
    customer spoke English (`lang`). It sees what was just said (`history`:
    [said, replied] pairs, robot/memory.py) and the part being talked about
    (`facts`), so «وده بيعمل إيه؟» is about that part. The reply is scrubbed
    with `redact` so it can never read out a wholesale/cost figure, and
    dropped if it names a price that isn't the part's own.

    `system`: another prompt in place of the robot's own (robot/workshop.py: the repair
    coach, said aloud); `longest`: letters said at most.
    """
    from .pricing import redact
    text = (transcript or "").strip()
    if not text:
        return ""
    # «انت في أنهي فرع؟», «ده فرع إيه؟» — however it's asked.
    place = f" وانت في فرع {branch}." if branch else ""
    filled = _shop_and_time()
    # 🐛 [FIX]: «اسمه Fixit» بعد «محل» اتفهم إنه اسم الروبوت: «أنا فكس إت من الفرع الرئيسي». الروبوت
    # ليه اسمه («موس»)، والمحل اسمه قبل نوعه.
    system = ((system or _AI_SYSTEM_PROMPT.format(shop=filled["shop"], robot=(robot or "موس").strip()))
              + place + filled["now"]
              + (_AI_FACTS_PROMPT + facts if facts else "") + (_AI_ENGLISH if lang == "en" else ""))
    turns = [(str(said)[:200], str(replied)[:300]) for said, replied in list(history or [])[-4:]]
    # 🐛 [FIX]: «بكام سعر الياباني؟» استنى 10 ثواني: بوابة الـ LLM العامة (Together) كانت بتتسأل
    # الأول وGemini بعدها. دلوقتي Gemini الأول (المفتاح اللي المحل مشترك فيه)، بمهلة قصيرة —
    # الرد بيتقال بصوت والزبون واقف — والبوابة بس لو Gemini مردّش.
    import time

    from .audio import gemini_generate
    said_before = "".join(f"العميل: {s}\nانت: {r}\n" for s, r in turns)
    parts = [{"text": system}] + ([{"text": "اللي اتقال قبل كده:\n" + said_before}] if said_before else [])
    from . import llm

    reply, took = "", None
    # Gemini out of time a moment ago (robot/llm.py): not waited on again — the backup model answers.
    if not llm.gemini_down():
        started = time.monotonic()
        reply = gemini_generate(parts + [{"text": text[:500]}], what="reply",
                                timeout=REPLY_TIMEOUT_S, budget=REPLY_BUDGET_S,
                                prefer=os.getenv("ROBOT_REPLY_MODEL", "").strip())
        took = time.monotonic() - started
        llm.note_gemini(bool(reply), took)
    if not reply and llm.has_backup():
        reply = llm.backup(system, text[:500], history=turns)
    elif not reply and took is not None and took < GATEWAY_IF_UNDER_S:
        messages = [{"role": "system", "content": system}]
        for said, replied in turns:
            messages += [{"role": "user", "content": said}, {"role": "assistant", "content": replied}]
        messages.append({"role": "user", "content": text[:500]})
        try:
            from inventory.ai_services import call_llm_layer
            reply = (call_llm_layer(messages, max_retries=1) or "").strip()
        except Exception:
            reply = ""
    if not reply:
        return ""
    # 🐛 [FIX]: from the shop's log — «Could you please confirm the exact model year…» said to an
    # Egyptian customer. An Arabic question's answer in English isn't said.
    if lang != "en" and language.detect(reply) == "en":
        logger.warning("robot ai_reply: dropped an English answer to an Arabic question")
        return ""
    reply = redact(reply[:longest])
    if invented_price(reply, facts + "\n" + text):
        logger.warning("robot ai_reply: dropped an answer with a price not in the catalog")
        return ""
    return reply


# What the shop's people ask that the ERP doesn't know: the internet.
_WEB_PROMPT = (
    "أنت روبوت مساعد في محل{shop} لقطع غيار BMW و MINI، وبتكلم حد من إدارة "
    "المحل. دوّر على النت على اللي بيسأل عنه، ورد بالعامية المصرية في جملتين أو تلاتة "
    "قصيرين لأن ردك هيتقال بصوت: المعلومة نفسها بس، من غير لينكات ولا علامات ولا "
    "مقدمات. لو ملقتش حاجة أكيدة قول كده بصراحة. أي سعر لقيته قول إنه سعر على النت "
    "مش سعر المحل."
)
_MARKUP = re.compile(r"\[\d+(?:,\s*\d+)*\]|[*#_`]+")


def web_answer(question: str, lang: str = "ar", *, facts: str = "", history=()) -> tuple:
    """(a short spoken answer, the pages it came from) — Gemini looking it
    up with Google Search. ('', []) when it can't. Only for the staff who may
    (robot/assistant.web_reply): it's the internet, not the ERP."""
    from .audio import gemini_generate
    from .pricing import redact

    text = (question or "").strip()
    if not text:
        return "", []
    filled = _shop_and_time()
    system = (_WEB_PROMPT.format(shop=filled["shop"]) + filled["now"]
              + ("\n\nالقطعة اللي بنتكلم عنها، من المخزون:\n" + facts if facts else "")
              + (_AI_ENGLISH if lang == "en" else ""))
    said_before = "".join(f"هو: {str(s)[:200]}\nانت: {str(r)[:300]}\n" for s, r in list(history or [])[-4:])
    parts = [{"text": system}] + ([{"text": "اللي اتقال قبل كده:\n" + said_before}] if said_before else [])
    sources: list = []
    reply = gemini_generate(parts + [{"text": text[:500]}], what="web search",
                            tools=[{"google_search": {}}], sources=sources)
    reply = re.sub(r"\s+([.،,!؟?:])", r"\1", " ".join(_MARKUP.sub(" ", reply or "").split()))
    if not reply:
        return "", []
    return redact(reply[:500]), sources


# ---------------------------------------------------------------------------
# POS: create a sale invoice from a scan (RETAIL price enforced explicitly)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_robot_sale(*, product, branch, customer, employee=None,
                      quantity: int = 1, unit_price: Optional[Decimal] = None,
                      payment: str = "cash", post: bool = True):
    """Create a one-line sale invoice from the robot POS flow.

    CRITICAL: we ALWAYS pass an explicit `unit_price` = retail_price. Leaving it
    blank makes `SaleInvoiceItem.save()` auto-fill the B2B *wholesale* price —
    exactly what the robot must never sell at. So `unit_price` defaults to the
    product's retail_price and is passed explicitly.

    Posting order matters: the `execute_sale_posting` signal fires when the
    invoice's status becomes 'posted', and it deducts stock from the invoice's
    ITEMS. So we must create the invoice as a draft quotation, add the item(s),
    then flip to 'posted' — otherwise the signal would run on an empty invoice,
    deduct nothing, and mark it applied. Stock deduction, totals, accrual and
    treasury are then handled by the existing InvoiceService — we don't
    reimplement them.
    """
    if unit_price is None:
        unit_price = Decimal(str(product.retail_price or 0))
    return create_robot_sale_lines(lines=[(product, quantity, unit_price)], branch=branch,
                                   customer=customer, payment=payment, post=post)


def create_robot_sale_lines(*, lines, branch, customer, payment: str = "cash",
                            post: bool = True, notes: str = ""):
    """`create_robot_sale` for several parts on one invoice: `lines` =
    [(product, quantity, unit_price)], each price explicit (retail — never
    left blank for the item to auto-fill wholesale). Same posting order."""
    from inventory.models import SaleInvoice, SaleInvoiceItem

    # A cash sale with no cash treasury would post as unpaid while the
    # customer has paid: refuse before anything is written.
    treasury = None
    if is_cash_payment(payment):
        treasury = _cash_treasury(branch)
        if treasury is None:
            raise ValueError(f"no active cash treasury for branch {branch}")

    # 1) Draft first (status defaults to 'quotation'; invoice_type must be 'sale').
    invoice = SaleInvoice.objects.create(
        invoice_type="sale",
        status="quotation",
        customer=customer,
        branch=branch,
        sales_channel="in_store",  # only in_store/website exist; robot is in-store
        **({"notes": notes} if notes else {}),
    )
    # 2) Add the lines at the explicit RETAIL price (never auto-filled wholesale).
    for product, quantity, unit_price in lines:
        SaleInvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_price=Decimal(str(unit_price)),
        )
    invoice.update_total()

    # 🐛 [FIX]: طلب لقطعة لسه جاية (allow_backorder) كان بيتعتمد زي البيع —
    #    والاعتماد بيخصم مخزون مش موجود فبيرمي خطأ والطلب كله بيقع بـ 500.
    #    الطلب بيتسجّل عرض سعر/أوردر (من غير خصم ولا تحصيل) لحد ما القطعة توصل.
    if not post:
        invoice.notes = "🤖 طلب من الروبوت — القطعة مش متوفرة حالياً (أوردر)."
        invoice.save(update_fields=["notes"])
        invoice.refresh_from_db()
        return invoice

    # 2b) Payment: "cash" settles the full amount into the branch cash treasury
    #     now (so the drawer/ledger is accurate); "credit" leaves it due (آجل).
    #     The execute_sale posting reads treasury + paid_amount to record the
    #     FinancialTransaction, so set them BEFORE flipping to posted.
    if is_cash_payment(payment):
        invoice.treasury = treasury
        invoice.paid_amount = invoice.total_amount
        invoice.save(update_fields=["treasury", "paid_amount"])

    # 3) Post — NOW the signal deducts stock/accrues (and settles cash) with the
    #    item present.
    invoice.status = "posted"
    invoice.save(update_fields=["status"])
    invoice.refresh_from_db()
    return invoice


def is_cash_payment(payment) -> bool:
    return str(payment).strip().lower() in ("cash", "كاش", "نقدي", "نقدا")


def _cash_treasury(branch):
    """The branch's active cash treasury (for settling a cash sale), or None."""
    try:
        from inventory.models import Treasury
        return (Treasury.objects
                .filter(branch=branch, type="cash", is_active=True)
                .order_by("id")
                .first())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Multi-agent sync: raise a procurement signal on critically-low stock
# ---------------------------------------------------------------------------

def maybe_raise_procurement_signal(*, device, product, branch):
    """If stock at `branch` is at/below the alert level, raise a trigger signal.

    Idempotent per (product, branch) while status='open' (DB constraint). Returns
    the ProcurementSignal or None. The Procurement Agent consumes open signals to
    build the next shipping-container manifest — the robot never orders directly.
    """
    from inventory.models import Inventory
    from .models import ProcurementSignal

    inv = Inventory.objects.filter(product=product, branch=branch).first()
    on_hand = inv.quantity if inv else 0
    min_level = getattr(product, "ai_calculated_min_stock", None) or getattr(
        product, "min_stock_level", 0
    )
    if on_hand > min_level:
        return None

    # Suggested reorder: bring back to ~2x the alert level, at least 1.
    reorder = max((min_level * 2) - on_hand, 1)
    signal, created = ProcurementSignal.objects.get_or_create(
        product=product, branch=branch, status="open",
        defaults={
            "device": device,
            "quantity_on_hand": on_hand,
            "min_stock_level": min_level,
            "suggested_reorder_qty": reorder,
            "manifest_payload": {
                "part_number": product.part_number,
                "oem": getattr(product, "oem_cross_reference", None) or [],
                "name": product.name,
                "branch": branch.name,
                "on_hand": on_hand,
                "min_level": min_level,
                "suggested_qty": reorder,
                "raised_at": timezone.now().isoformat(),
            },
        },
    )

    # Multi-agent hand-off: open a real RFQ so the Procurement Agent / vendors
    # can quote it — the robot triggers procurement, it doesn't order directly.
    # Only on first raise, and only if there isn't already an open RFQ for it.
    if created:
        rfq_id = _open_rfq_for(product, branch, reorder)
        if rfq_id:
            signal.manifest_payload["rfq_id"] = rfq_id
            signal.save(update_fields=["manifest_payload"])
    return signal


# What each actuator can physically do (mirrors the Mega's handleCommand).
MOTOR_DIRECTIONS = {
    "head": {"left", "right", "stop"},
    "arm_left": {"up", "down", "stop"},
    "arm_right": {"up", "down", "stop"},
    "track": {"forward", "backward", "stop"},
}
# Longest single move, matching the Mega's DEFAULT_PULSE_CAP. A move always
# ends on its own: "0 = run until stop" would run a window motor into its end
# stop and stall it (burning the motor and relay), and a lost stop frame would
# leave the tracks driving.
MOTOR_MAX_MS = 5000
MOTOR_DEFAULT_MS = 500
# A queued move older than this is never sent (see views.motor_pending). The
# bridge polls every 500 ms, so a live command is picked up within a second.
MOTOR_COMMAND_TTL = timedelta(seconds=30)


def validate_motor_command(actuator, direction, duration_ms=None):
    """Normalize a requested move → (actuator, direction, duration_ms).

    Raises ValueError with a spoken-friendly reason for anything the hardware
    can't do. `stop` always has duration 0; any other move is clamped to
    1..MOTOR_MAX_MS (missing/0 → MOTOR_DEFAULT_MS).
    """
    actuator = str(actuator or "").strip().lower()
    direction = str(direction or "").strip().lower()
    if actuator not in MOTOR_DIRECTIONS:
        raise ValueError(f"جزء غير معروف: {actuator or '—'}")
    if direction not in MOTOR_DIRECTIONS[actuator]:
        raise ValueError(f"الاتجاه «{direction or '—'}» غير متاح لـ {actuator}")
    if direction == "stop":
        return actuator, direction, 0
    try:
        ms = int(float(duration_ms)) if duration_ms not in (None, "") else 0
    except (TypeError, ValueError):
        raise ValueError("مدة الحركة غير صالحة")
    if ms <= 0:
        ms = MOTOR_DEFAULT_MS
    return actuator, direction, min(ms, MOTOR_MAX_MS)


# A device silent for this long counts as offline (the dashboard's "online"
# dot uses 90s; alerts wait longer so a Wi-Fi blip doesn't page anyone).
OFFLINE_AFTER_SECONDS = 5 * 60


def note_device_seen(device, now=None):
    """Record that the device just talked to us; alert if it was offline.

    Returns the `back_online` RobotAlert when this call ends an outage, else
    None. Called on every authenticated device request, before `last_seen_at`
    is overwritten.
    """
    from .models import RobotAlert

    now = now or timezone.now()
    last = device.last_seen_at
    if last is None or (now - last).total_seconds() < OFFLINE_AFTER_SECONDS:
        return None
    gone = int((now - last).total_seconds() // 60)
    return RobotAlert.objects.create(
        device=device, kind="back_online",
        message=f"✅ الروبوت رجع أونلاين بعد انقطاع حوالي {gone} دقيقة.",
    )


def raise_offline_alerts(now=None) -> int:
    """Alert once per outage for every active device that went silent.

    Run periodically (see `robot.tasks`). An outage is alerted once: we skip a
    device that already has an `offline` alert newer than its last contact.
    """
    from .models import RobotAlert, RobotDevice

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=OFFLINE_AFTER_SECONDS)
    raised = 0
    for device in RobotDevice.objects.filter(is_active=True, last_seen_at__lt=cutoff):
        already = RobotAlert.objects.filter(
            device=device, kind="offline", created_at__gte=device.last_seen_at,
        ).exists()
        if already:
            continue
        RobotAlert.objects.create(
            device=device, kind="offline",
            message=(f"📡 الروبوت {device.name} فقد الاتصال — آخر اتصال "
                     f"{timezone.localtime(device.last_seen_at):%Y-%m-%d %H:%M}."),
        )
        raised += 1
    return raised


def take_camera_commands(device, *, limit: int = 5) -> list:
    """Hand the camera its pending snapshot/scan commands (marked sent)."""
    from .models import RobotCommand
    cmds = list(device.commands.filter(
        status="pending", kind__in=RobotCommand.CAMERA_KINDS,
    ).order_by("created_at")[:limit])
    if cmds:
        RobotCommand.objects.filter(id__in=[c.id for c in cmds]).update(
            status="sent", sent_at=timezone.now(),
        )
    return [{"command_id": c.id, "kind": c.kind, "payload": c.payload} for c in cmds]


# The head as it's built, calibrated in the shop (the server's .env):
#   ROBOT_HEAD_EDGE_MS  the pulse for a face at the frame's edge — less when the
#                       head swings past the person, more when it creeps up on them
#   ROBOT_HEAD_FLIP=1   it turns away from them (the camera or the relays the other way round)
HEAD_EDGE_MS = 250
HEAD_DEAD_ZONE = 0.15      # already looking at them: a face this near the middle
HEAD_MIN_MS = 40           # shorter, the relay closes and the motor doesn't move


def head_edge_ms() -> int:
    try:
        ms = int(float(os.getenv("ROBOT_HEAD_EDGE_MS") or HEAD_EDGE_MS))
    except ValueError:
        ms = HEAD_EDGE_MS
    return max(HEAD_MIN_MS, min(ms, MOTOR_MAX_MS))


def head_flipped() -> bool:
    return (os.getenv("ROBOT_HEAD_FLIP") or "").strip().lower() in ("1", "true", "yes", "on")


def look_at(device, offset: float, *, issued_by=None):
    """Turn the robot's head toward a target at horizontal `offset` ∈ [-1,1].

    offset < 0 → target is to the LEFT, > 0 → RIGHT, ~0 → centered (no move).
    Emits a head MotorCommandLog whose duration is proportional to how far
    off-center the target is; the ESP32 picks it up from /motor/pending/ and the
    Mega pans the head. Used to face the customer/speaker (robot/headfollow.py).
    """
    from .models import MotorCommandLog

    try:
        offset = float(offset)
    except (TypeError, ValueError):
        return None
    # 🐛 [FIX]: «nan» من الكاميرا كان بيعدّي من الـ clamp كـ 1.0 (min(1.0, nan) = 1.0)
    # والراس تلف لفة كاملة يمين.
    if math.isnan(offset):
        return None
    offset = max(-1.0, min(1.0, offset))
    if abs(offset) < HEAD_DEAD_ZONE:  # already looking at them
        return None
    if head_flipped():
        offset = -offset
    direction = "right" if offset > 0 else "left"
    # 🐛 [FIX]: كانت 700 ms ثابتة لوش على طرف الصورة — موتور عربية بيلف في الوقت ده
    # أبعد بكتير من نص زاوية الكاميرا، فالراس كانت بتعدّي اللي بيتكلم وترجع تتهز.
    # دلوقتي بتتظبط على الراس نفسها من الـ .env (ROBOT_HEAD_EDGE_MS، و ROBOT_HEAD_FLIP لو بتلف عكس).
    duration_ms = max(HEAD_MIN_MS, round(abs(offset) * head_edge_ms()))
    return MotorCommandLog.objects.create(
        device=device, actuator="head", direction=direction,
        duration_ms=duration_ms, issued_by=issued_by,
    )


# ---------------------------------------------------------------------------
# After-hours guard
# ---------------------------------------------------------------------------

def is_after_hours(device, now=None) -> bool:
    """True if `now` (local) falls in the device's guard window (shop closed).

    Uses the device's explicit guard_from/guard_to when set. When they're not,
    falls back to a sane default night window (20:00–08:00) so guarding is armed
    out of the box rather than silently off.
    """
    now = now or timezone.localtime()
    t = now.time()
    start = device.guard_from
    end = device.guard_to
    if start is None or end is None:
        start = start or _dt_time(20, 0)
        end = end or _dt_time(8, 0)
    if start <= end:
        return start <= t <= end
    # Window crosses midnight (e.g. 20:00 → 08:00).
    return t >= start or t <= end


def guard_window(device) -> dict:
    """The guard window as the camera needs it to tell night from day on its
    own during an outage: local "HH:MM" bounds (the same defaults as
    `is_after_hours`) and the current UTC offset in seconds (Cairo changes
    it for summer time; the camera's clock is UTC)."""
    start = device.guard_from or _dt_time(20, 0)
    end = device.guard_to or _dt_time(8, 0)
    offset = timezone.localtime().utcoffset()
    return {"from": start.strftime("%H:%M"), "to": end.strftime("%H:%M"),
            "utc_offset": int(offset.total_seconds()) if offset else 0}


def _dt_time(h, m):
    from datetime import time as _t
    return _t(h, m)


def raise_low_battery_alert(device, percent):
    """Raise a low-battery alert, deduped to one per 30 minutes per device."""
    from .models import RobotAlert
    recent = (RobotAlert.objects
              .filter(device=device, kind="low_battery",
                      created_at__gte=timezone.now() - timedelta(minutes=30))
              .exists())
    if recent:
        return None
    return RobotAlert.objects.create(
        device=device, kind="low_battery",
        message=f"🔋 بطارية الروبوت منخفضة ({percent}%) — محتاجة شحن.",
    )


# «هسأل لك حد من زمايلي» — and nobody was asked. The robot calls out for one, aloud, and the
# dashboard's alerts say who asked what.
STAFF_CALL = "يا موظف! يا موظف! يا موظف!"
STAFF_CALL_EVERY_S = 60              # one call a minute: a customer asking twice isn't two calls
_STAFF_WORDS = re.compile(r"زمايلي|زميلي|زميل|الموظفين|موظف")
_STAFF_ASKED = re.compile(r"هسأل|اسأل|أسأل|نسأل|هند|ند[هي]|ناد|هراجع|هشوف|حالا|حالاً|فورا|فوراً|لحظة|ثانية|يساعد|هيجي|جاي")


def hands_to_staff(reply: str) -> bool:
    """The reply says a member of staff will (or should) help: «هسأل لك حد من
    زمايلي»، «اسأل حد من الموظفين»، «لحظة وهندهلك»."""
    return bool(reply and _STAFF_WORDS.search(reply) and _STAFF_ASKED.search(reply))


def call_staff(device, question: str) -> bool:
    """Ask the staff over — once a minute at most for a robot. True when the
    call is to be said now; the dashboard's alerts keep what was asked."""
    from django.core.cache import cache

    from .models import RobotAlert

    key = f"robot:staff:called:{device.pk}"
    try:
        if not cache.add(key, 1, STAFF_CALL_EVERY_S):
            return False
    except Exception:                    # the cache down: called, as if it were up
        pass
    RobotAlert.objects.create(device=device, kind="other",
                              message=f"🙋 عميل عند {device.name} محتاج موظف: «{(question or '')[:200]}»")
    return True


@transaction.atomic
def raise_after_hours_alert(device, *, snapshot=None, message=""):
    """Create an after-hours motion alert (deduped to one per 5 minutes)."""
    from .models import RobotAlert

    recent = (RobotAlert.objects
              .filter(device=device, kind="after_hours_motion",
                      created_at__gte=timezone.now() - timedelta(minutes=5))
              .exists())
    if recent:
        return None
    return RobotAlert.objects.create(
        device=device, kind="after_hours_motion", snapshot=snapshot,
        message=message or "🚨 حركة مرصودة بعد غلق المحل!",
    )


# ---------------------------------------------------------------------------
# Offline sync — catalog for the robot to work on when the net is down
# ---------------------------------------------------------------------------

def _outage_alert(device, payload: dict):
    """Tell the owner the robot worked through an internet outage, and what it
    kept for later (sent by each board once its backlog is uploaded)."""
    from datetime import datetime, timezone as dt_timezone
    from .models import RobotAlert

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
    from .models import RobotKnowledge
    aliases = [
        {"kind": kind, "key": key, "part_number": pn}
        for kind, key, pn in (RobotKnowledge.objects
                              .filter(key_kind__in=("code", "label"))
                              .order_by("-hit_count")
                              .values_list("key_kind", "key_value", "product__part_number")[:limit])
    ]
    # Who may use the counter page with the net down, and for what (PIN hashes).
    from . import teachers
    from .offline_ledger import staff_for
    from .replies import for_branch
    lessons = teachers.lessons()
    return {
        "branch": branch.name,
        "branch_id": branch.pk,
        # Every branch: the counter page records on the one picked (robot/brain/counter.py).
        "branches": [{"id": b.pk, "name": b.name} for b in Branch.objects.order_by("name")],
        "shop": shop_name(),                 # the receipt on the counter page: the shop's, not the platform's
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
    from . import understand

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
    from .models import RobotSyncEvent

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
                    product = find_product(pn) if pn else None
                    if product:
                        learn_from_confirmation(
                            product=product,
                            code=payload.get("code", ""),
                            label=payload.get("label", ""),
                        )
                elif kind == "outage":
                    _outage_alert(device, payload)
                elif kind == "restock":
                    from . import restock
                    from .brain import talk
                    pn = str(payload.get("part_number") or "").strip()
                    phone = talk.mobile_in(str(payload.get("phone") or ""))
                    product = Product.objects.filter(part_number=pn).first() if pn else None
                    if product is None or not phone:
                        raise ValueError("restock: unknown part or number")
                    restock.register(device, product, phone)
                elif kind == "sale":
                    from . import offline_ledger
                    offline_ledger.apply_sale(device, payload)
                elif kind == "expense":
                    from . import offline_ledger
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


# ---------------------------------------------------------------------------
# Diagnostics + ECU enrichment (part codes, fault codes, coding refs)
# ---------------------------------------------------------------------------

def lookup_fault_code(code: str) -> dict:
    """Resolve a DTC / fault code to its definition + guided steps + causes.

    Backs the voice assistant when a mechanic asks about a code (e.g. "P0301").
    Reads the shared `diagnostics_catalog.DTCDefinition`.
    """
    try:
        from diagnostics_catalog.models import DTCDefinition
    except Exception:
        return {"found": False, "code": code}

    dtc = DTCDefinition.objects.filter(code__iexact=(code or "").strip()).first()
    if not dtc:
        return {"found": False, "code": code}
    # 🐛 [FIX]: «long_description» مش موجود في الموديل — الوصف التفصيلي والقطع اللي غالباً السبب
    # (`full_description`، `likely_oem_parts`) عمرهم ما وصلوا للروبوت.
    parts = getattr(dtc, "likely_oem_parts", []) or []
    return {
        "found": True,
        "code": dtc.code,
        "severity": getattr(dtc, "severity", None),
        "description": getattr(dtc, "short_description", "") or getattr(dtc, "full_description", ""),
        "details": getattr(dtc, "full_description", "") or "",
        "guided_steps": getattr(dtc, "guided_steps", []) or [],
        "likely_causes": getattr(dtc, "likely_causes", []) or [],
        "likely_parts": [str(p.get("part_number") or p.get("name") or p) if isinstance(p, dict) else str(p)
                         for p in parts],
    }


def _normalize_key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


# ---------------------------------------------------------------------------
# Learning memory — the robot gets better at recognizing parts with use
# ---------------------------------------------------------------------------

def resolve_from_knowledge(*, code: str = "", label: str = "", fingerprint_hash: str = ""):
    """Return a previously-confirmed Product for a code/label/fingerprint, or None.

    Checked before falling back to a fresh catalogue search, so a part the robot
    has been taught once is recognized instantly next time. A fingerprint with
    no exact entry falls back to the nearest learned one (see
    `match_fingerprint`), since two photos of the same part rarely hash equal.
    """
    from .models import RobotKnowledge

    for kind, val in (("code", code), ("label", label), ("fingerprint", fingerprint_hash)):
        val = _normalize_key(val)
        if not val:
            continue
        entry = (
            RobotKnowledge.objects.filter(key_kind=kind, key_value=val)
            .select_related("product")
            .order_by("-hit_count")
            .first()
        )
        if entry:
            return entry.product
    if fingerprint_hash:
        product, _distance = match_fingerprint(fingerprint_hash)
        return product
    return None


# Max differing bits (of 64) for two average-hashes to count as the same part.
# Re-shooting the same part on the same counter moves a handful of bits; a
# different part moves far more. Kept tight because a wrong match names the
# wrong part — the scan flow still asks a human to confirm fingerprint hits.
_FINGERPRINT_MAX_DISTANCE = 5

# How many learned rows a near-match or alias scan looks at (most-confirmed
# first), so a large knowledge table can't turn one request into a full scan.
_KNOWLEDGE_SCAN_LIMIT = 5000


def _hamming(a_hex: str, b_hex: str) -> int:
    """Differing bits between two hex hashes (64 when either is unreadable)."""
    try:
        return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def match_fingerprint(fingerprint_hash: str):
    """Nearest learned product for a visual hash → (product, distance).

    Returns (None, None) when nothing is within `_FINGERPRINT_MAX_DISTANCE`.
    Ties go to the mapping humans confirmed most often.
    """
    from .models import RobotKnowledge

    target = _normalize_key(fingerprint_hash)
    if not target:
        return None, None
    rows = (RobotKnowledge.objects.filter(key_kind="fingerprint")
            .order_by("-hit_count")
            .values_list("key_value", "product_id", "hit_count")[:_KNOWLEDGE_SCAN_LIMIT])
    best = None  # (distance, -hits, product_id)
    for key, product_id, hits in rows:
        d = _hamming(target, key)
        if d > _FINGERPRINT_MAX_DISTANCE:
            continue
        cand = (d, -(hits or 0), product_id)
        if best is None or cand < best:
            best = cand
    if best is None:
        return None, None
    from inventory.models import Product
    return Product.objects.filter(pk=best[2]).first(), best[0]


def nearest_fingerprint_key(fingerprint_hash: str, product) -> str:
    """The learned hash of `product` closest to `fingerprint_hash` (within the
    match radius), or '' — the key a look-alone match actually came from."""
    from .models import RobotKnowledge

    target = _normalize_key(fingerprint_hash)
    if not target or product is None:
        return ""
    best_key, best_d = "", _FINGERPRINT_MAX_DISTANCE + 1
    for key in (RobotKnowledge.objects
                .filter(key_kind="fingerprint", product=product)
                .values_list("key_value", flat=True)[:_KNOWLEDGE_SCAN_LIMIT]):
        d = _hamming(target, key)
        if d < best_d:
            best_key, best_d = key, d
    return best_key


def image_hash(image_bytes: bytes) -> str:
    """64-bit average-hash (hex) of an image — the visual learning key, or ''."""
    if not image_bytes:
        return ""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((8, 8))
        px = list(img.getdata())
        avg = sum(px) / len(px)
        bits = "".join("1" if p >= avg else "0" for p in px)
        return f"{int(bits, 2):016x}"
    except Exception:
        return ""


def scan_fingerprint(event) -> str:
    """Visual hash of a stored scan's photo ('' when it has none)."""
    if not getattr(event, "image", None):
        return ""
    try:
        f = event.image.open("rb")
        try:
            return image_hash(f.read())
        finally:
            f.close()
    except Exception:
        return ""


def learned_alias_in(text: str):
    """Product whose taught name/alias appears inside a spoken sentence.

    Staff teach the robot shop slang ("الطرمبة" → a water-pump SKU); customers
    then say it inside a longer question, so we look for the longest learned
    label contained in the sentence rather than an exact match. None if no
    alias of 3+ characters appears.
    """
    from .models import RobotKnowledge

    import re as _re
    # Whole words only ("abs" must not match inside "tabs"); an Arabic
    # article/conjunction prefix is allowed ("طرمبة" matches "الطرمبة").
    norm = " " + " ".join(_re.sub(r"[^\w\s]", " ", _normalize_key(text)).split()) + " "
    if len(norm.strip()) < 3:
        return None
    rows = (RobotKnowledge.objects.filter(key_kind="label")
            .order_by("-hit_count")
            .values_list("key_value", "product_id")[:_KNOWLEDGE_SCAN_LIMIT])
    best_key, best_pid = "", None
    for key, product_id in rows:
        if len(key) < 3 or len(key) <= len(best_key):
            continue
        pattern = r"\s(?:ال|وال|بال|لل|و|ب)?" + _re.escape(key) + r"\s"
        if _re.search(pattern, norm):
            best_key, best_pid = key, product_id
    if best_pid is None:
        return None
    from inventory.models import Product
    return Product.objects.filter(pk=best_pid).first()


def unlearn(*, product, code: str = "", label: str = "", fingerprint_hash: str = ""):
    """A human said this code/label/look is NOT `product`: weaken that memory.

    Each correction takes one confirmation off the wrong mapping and forgets it
    entirely at zero, so a single mistaken confirmation can't keep mislabeling
    a part forever. Returns how many mappings were weakened or removed.
    """
    from .models import RobotKnowledge

    touched = 0
    for kind, val in (("code", code), ("label", label), ("fingerprint", fingerprint_hash)):
        val = _normalize_key(val)
        if not val:
            continue
        entry = RobotKnowledge.objects.filter(
            key_kind=kind, key_value=val, product=product,
        ).first()
        if entry is None:
            continue
        if (entry.hit_count or 0) <= 1:
            entry.delete()
        else:
            entry.hit_count -= 1
            entry.save(update_fields=["hit_count", "updated_at"])
        touched += 1
    return touched


@transaction.atomic
def teach_scan_correction(event, *, product, employee=None) -> dict:
    """Staff correct a scan: "this photo is really THAT part".

    Weakens whatever the robot wrongly guessed for this scan's code/label/look,
    learns the right mapping, and re-points the scan at the right product — so
    the same mistake gets less likely every time someone fixes it.
    """
    fp = scan_fingerprint(event)
    code = event.recognized_part_number or ""
    label = event.recognized_label or ""
    wrong = event.product
    forgotten = 0
    if wrong is not None and wrong.pk != product.pk:
        # A look-alone guess came from a NEARBY learned hash, stored under a
        # different key than this photo's — weaken that one.
        wrong_fp = nearest_fingerprint_key(fp, wrong) or fp
        forgotten = unlearn(product=wrong, code=code, label=label, fingerprint_hash=wrong_fp)
    learn_from_confirmation(
        product=product, code=code, label=label, fingerprint_hash=fp,
        employee=employee, details={"source": "correction", "scan_id": event.pk},
    )
    event.product = product
    event.save(update_fields=["product"])
    return {
        "ok": True,
        "scan_id": event.pk,
        "product_id": product.pk,
        "name": product.name,
        "was": getattr(wrong, "name", None),
        "forgot_wrong_mappings": forgotten,
    }


def learn_from_confirmation(*, product, code: str = "", label: str = "",
                            fingerprint_hash: str = "", details: dict = None,
                            employee=None):
    """Record that a human confirmed code/label/fingerprint → product.

    Idempotent per (kind, value, product): repeat confirmations bump `hit_count`
    (rising confidence) instead of duplicating rows.
    """
    from .models import RobotKnowledge

    created_entries = []
    for kind, val in (("code", code), ("label", label), ("fingerprint", fingerprint_hash)):
        val = _normalize_key(val)
        if not val:
            continue
        entry, created = RobotKnowledge.objects.get_or_create(
            key_kind=kind, key_value=val, product=product,
            defaults={"details": details or {}, "confirmed_by": employee},
        )
        if not created:
            entry.hit_count = models_F_increment(entry)
            entry.save(update_fields=["hit_count", "updated_at"])
        created_entries.append(entry)
    return created_entries


def models_F_increment(entry):
    """Return hit_count+1 as a plain int (kept simple; avoids F() refresh dance)."""
    return (entry.hit_count or 0) + 1


# ---------------------------------------------------------------------------
# Goods intake — "صوّر القطعة وسجّلها": photograph → white bg → add/increment
# ---------------------------------------------------------------------------

@transaction.atomic
def intake_part(*, device, branch, image_bytes: bytes = None, name: str = "",
                part_number: str = "", retail_price=None, quantity: int = 1,
                car_model: str = "", part_category: str = "", employee=None):
    """Photograph a part, register it, and add stock — the core intake flow.

    Steps:
      1. If an image is given, read the part number via vision (unless supplied)
         and produce a clean studio-white version for the product photo.
      2. Find the product (knowledge memory → catalogue by code/OEM/name).
      3. If found → increment on-hand quantity at this branch (inventory_service.
         set_branch_stock: the movement, the average cost, the ledger).
      4. If NOT found → create a new Product with its details, retail price and
         white-background photo, then set the branch stock.
      5. Learn the mapping (code/label) → product so next time is instant.

    Returns a dict describing what happened.
    """
    from django.core.files.base import ContentFile
    from inventory.models import Inventory, Product, ProductImage
    from .models import RobotScanEvent
    from . import vision

    # 1) Vision: read code + make white-background image.
    white_bytes = None
    if image_bytes:
        if not part_number:
            _label, code, conf = vision.identify_part(image_bytes)
            if conf >= 0.75 and code:
                part_number = code
        white_bytes = vision.white_background(image_bytes)

    # 2) Resolve existing product (learning memory first, then catalogue).
    product = resolve_from_knowledge(code=part_number, label=name)
    if product is None and part_number:
        product = find_product(part_number)
    if product is None and name:
        product = find_product(name)

    created_new = False
    if product is None:
        # 4) Create a brand-new product with sensible defaults for required fields.
        if not part_number:
            # Deterministic fallback SKU so intake never fails for lack of a code.
            from django.utils.crypto import get_random_string
            part_number = f"ROBOT-{get_random_string(8).upper()}"
        product = Product.objects.create(
            name=name or part_number,
            part_number=part_number,
            part_category=part_category or "mechanical",
            car_model=car_model or "غير محدد",
            car_year="غير محدد",
            retail_price=Decimal(str(retail_price or 0)),
        )
        created_new = True
        _attach_photo(product, white_bytes or image_bytes, ProductImage, ContentFile)
    else:
        # Update retail price if a new one was dictated, and ensure a photo.
        if retail_price not in (None, "", 0):
            product.retail_price = Decimal(str(retail_price))
            product.save(update_fields=["retail_price"])
        if (white_bytes or image_bytes) and not product.images.exists():
            _attach_photo(product, white_bytes or image_bytes, ProductImage, ContentFile)

    # 3/4) Add stock at the branch, through the ERP's own stock setting.
    # 🐛 [FIX]: the quantity was written straight in — no ledger entry (the books' inventory drifted
    # from the shelf with every intake) and no average cost weighed in, and the movement came out as
    # an unexplained «manual edit». set_branch_stock does all three, as the product screen's edit
    # and the voice's «ضيف للمخزون» (entries.record_stock) do.
    from inventory.services.inventory_service import set_branch_stock

    here = (Inventory.objects.select_for_update().filter(product=product, branch=branch)
            .values_list("quantity", flat=True).first() or 0)
    set_branch_stock(product, branch, here + int(quantity), user=getattr(employee, "user", None),
                     reference_type="RobotIntake", note="🤖 إدخال بضاعة بالصورة")
    inv = Inventory.objects.get(product=product, branch=branch)

    # 5) Learn + audit — including a VISUAL fingerprint so the same physical
    #    part can be re-identified from a photo next time, not just its code.
    fp_hash, fp_details = _image_fingerprint(image_bytes, product) if image_bytes else ("", {})
    learn_from_confirmation(
        product=product, code=part_number, label=name,
        fingerprint_hash=fp_hash, details=fp_details, employee=employee,
    )
    RobotScanEvent.objects.create(
        device=device, purpose="intake",
        recognized_label=name, recognized_part_number=part_number,
        confidence=1.0, product=product, created_by=employee,
        quantity_added=int(quantity), created_new_product=created_new,
    )

    # Low-stock is unlikely right after intake, but keep the ecosystem in sync.
    maybe_raise_procurement_signal(device=device, product=product, branch=branch)

    return {
        "ok": True,
        "created_new_product": created_new,
        "product_id": product.id,
        "part_number": product.part_number,
        "name": product.name,
        "quantity_added": int(quantity),
        "on_hand": inv.quantity,
        "retail_price": float(product.retail_price or 0),
    }


def _attach_photo(product, image_bytes, ProductImage, ContentFile):
    """Save an image (white-bg preferred) as the product's primary photo."""
    if not image_bytes:
        return
    fname = f"{product.part_number}.jpg"
    img = ProductImage(product=product, is_primary=True)
    img.image.save(fname, ContentFile(image_bytes), save=True)


def _image_fingerprint(image_bytes: bytes, product=None):
    """Return (avg_hash_hex, details) for visual re-identification learning.

    The key is a perceptual average-hash (8x8 grayscale, thresholded at the
    mean) — near-identical photos of the same part yield the same hash, so
    `resolve_from_knowledge(fingerprint_hash=...)` re-recognizes it. `details`
    additionally carries the ERP's richer Gemini fingerprint when available.
    """
    ahash = image_hash(image_bytes)
    details = {}
    try:
        from . import vision
        details = vision.fingerprint(image_bytes) or {}
    except Exception:
        details = {}
    return ahash, details


# ---------------------------------------------------------------------------
# Stock-take (جرد) — "اجرد كذا وكذا"
# ---------------------------------------------------------------------------

@transaction.atomic
def run_stock_take(*, device, branch, counts: list, instruction: str = "", employee=None):
    """Reconcile a list of counted items against on-hand inventory.

    `counts` = [{"query": "<code/name>", "counted_qty": <int>}, ...]. Creates a
    RobotStockTakeSession with a reconciled line per resolvable item. Does NOT
    auto-adjust stock — a supervisor approves variances from the dashboard.
    """
    from inventory.models import Inventory
    from .models import RobotStockTakeSession, RobotStockTakeLine

    session = RobotStockTakeSession.objects.create(
        device=device, branch=branch, instruction=instruction,
        status="completed", started_by=employee, completed_at=timezone.now(),
    )
    lines, unresolved = [], []
    for row in counts or []:
        product = resolve_from_knowledge(code=row.get("query", ""), label=row.get("query", ""))
        if product is None:
            product = find_product(row.get("query", ""))
        if product is None:
            unresolved.append(row.get("query", ""))
            continue
        inv = Inventory.objects.filter(product=product, branch=branch).first()
        expected = inv.quantity if inv else 0
        line = RobotStockTakeLine.objects.create(
            session=session, product=product,
            expected_qty=expected, counted_qty=int(row.get("counted_qty", 0) or 0),
        )
        lines.append(line)

    return {
        "session_id": session.id,
        "counted_items": len(lines),
        "matches": sum(1 for ln in lines if ln.matched),
        "variances": [
            {"product": ln.product.name, "expected": ln.expected_qty,
             "counted": ln.counted_qty, "variance": ln.variance}
            for ln in lines if not ln.matched
        ],
        "unresolved": [u for u in unresolved if u],
    }


def get_open_stock_take(device):
    """The device's currently-open stock-take session (last 2 hours), or None."""
    from .models import RobotStockTakeSession
    since = timezone.now() - timedelta(hours=2)
    return (RobotStockTakeSession.objects
            .filter(device=device, status="open", created_at__gte=since)
            .order_by("-created_at")
            .first())


def start_stock_take(device, branch, *, instruction="", employee=None):
    """Open a new conversational stock-take session for the device."""
    from .models import RobotStockTakeSession
    return RobotStockTakeSession.objects.create(
        device=device, branch=branch, instruction=instruction,
        status="open", started_by=employee,
    )


def add_stock_take_count(session, *, query: str, counted_qty: int):
    """Add/replace one counted line in an open session. Returns (line, product)."""
    from inventory.models import Inventory
    from .models import RobotStockTakeLine

    product = resolve_from_knowledge(code=query, label=query) or find_product(query)
    if product is None:
        return None, None
    inv = Inventory.objects.filter(product=product, branch=session.branch).first()
    expected = inv.quantity if inv else 0
    line, _created = RobotStockTakeLine.objects.update_or_create(
        session=session, product=product,
        defaults={"expected_qty": expected, "counted_qty": int(counted_qty)},
    )
    return line, product


def complete_stock_take(session):
    """Close a session and return its reconciliation report."""
    session.status = "completed"
    session.completed_at = timezone.now()
    session.save(update_fields=["status", "completed_at"])
    lines = list(session.lines.select_related("product"))
    return {
        "session_id": session.id,
        "counted_items": len(lines),
        "matches": sum(1 for ln in lines if ln.matched),
        "variances": [
            {"product": ln.product.name, "expected": ln.expected_qty,
             "counted": ln.counted_qty, "variance": ln.variance}
            for ln in lines if not ln.matched
        ],
    }


@transaction.atomic
def apply_stock_take(session, *, employee=None):
    """Apply a completed stock-take: correct Inventory to the counted numbers.

    Each variance line sets the branch on-hand to the counted quantity; the
    existing inventory signal records the movement (reason 'adjustment' via the
    manual path). Idempotent — a session already 'applied' is a no-op.
    """
    from inventory.models import Inventory

    if session.status == "applied":
        return {"applied": False, "reason": "already applied"}
    # Only a finished count may move stock: an open session is still being
    # counted (half the shelf would be "corrected" to zero), and a cancelled
    # one was thrown away on purpose.
    if session.status != "completed":
        return {"applied": False, "reason": f"session is {session.status}, not completed"}

    from inventory.models import InventoryMovement
    from inventory.services.accounting_service import AccountingService

    adjusted = 0
    for line in session.lines.select_related("product"):
        if line.matched:
            continue
        inv, _created = Inventory.objects.select_for_update().get_or_create(
            product=line.product, branch=session.branch, defaults={"quantity": 0},
        )
        before = inv.quantity
        inv.quantity = int(line.counted_qty)
        inv.save(update_fields=["quantity"])
        diff = inv.quantity - before
        if diff:
            InventoryMovement.objects.create(
                product=line.product, branch=session.branch, reason="adjustment",
                quantity_change=diff, quantity_before=before, quantity_after=inv.quantity,
                reference_type="RobotStockTake", reference_id=session.pk,
                note=f"جرد الروبوت #{session.pk}",
                created_by=getattr(employee, "user", None),
            )
            # 📒 العجز/الزيادة في الجرد بيتقيّد على المخزون — من غيره حساب
            #    المخزون في الدفتر بيبعد عن البضاعة الفعلية بعد كل جرد.
            try:
                with transaction.atomic():
                    AccountingService.post_stock_adjustment(
                        product=line.product, quantity_change=diff, branch=session.branch,
                        reference=f"STOCKTAKE-{session.pk}-{line.product_id}",
                        description=f"فرق جرد الروبوت #{session.pk} — {line.product.name} ({diff:+d})",
                    )
            except Exception:  # the ledger never blocks a stock correction
                logger.exception("robot: stock-take ledger posting failed")
        adjusted += 1

    session.status = "applied"
    session.save(update_fields=["status"])
    return {"applied": True, "adjusted_lines": adjusted}


def parse_count_utterance(text: str):
    """Parse "<part name/code> <number>" from a spoken count, e.g. "كنترول ٣".

    Returns (query, qty) or (None, None). Handles Arabic-Indic digits and a
    trailing integer.
    """
    import re
    if not text:
        return None, None
    # Normalize Arabic-Indic digits to ASCII.
    trans = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    t = text.translate(trans).strip()
    m = re.search(r"(.+?)\s*(\d+)\s*$", t)
    if not m:
        return None, None
    query = m.group(1).strip(" :،,-")
    try:
        qty = int(m.group(2))
    except ValueError:
        return None, None
    return (query or None), qty


def lookup_ecu_profile(ecu_name: str) -> dict:
    """Return ECU hardware/coding reference (pinout + physical steps) for a module.

    Backs the "programming/coding" requirement — the robot can read out the
    verified physical steps and pinout callouts for an ECU from `bmw_ecu`.
    """
    try:
        from bmw_ecu.models import EcuHardwareProfile
    except Exception:
        return {"found": False, "ecu_name": ecu_name}

    profile = EcuHardwareProfile.objects.filter(
        ecu_name__iexact=(ecu_name or "").strip()
    ).first()
    if not profile:
        return {"found": False, "ecu_name": ecu_name}
    return {
        "found": True,
        "ecu_name": profile.ecu_name,
        "board_revision": getattr(profile, "board_revision", ""),
        "verified": getattr(profile, "verified", False),
        "callouts": getattr(profile, "callouts", []) or [],
        "physical_steps_ar": getattr(profile, "physical_steps_ar", []) or [],
        "physical_steps_en": getattr(profile, "physical_steps_en", []) or [],
    }
