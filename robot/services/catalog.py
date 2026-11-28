"""🔎 خدمات الروبوت — اسم المحل، البحث عن القطع والمخزون، القطعة اللي اتقالت في جملة، سعر المستعمل، الرف."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import logging

from django.db.models import Q

from ..assistant import part_codes
from ..brain import slang, talk
from ..brain.arabic_numbers import CODE_IN_TEXT, spoken_codes
from ..pricing import safe_product_payload
from robot import services  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from .device import validate_motor_command
from .learning import learned_alias_in, resolve_from_knowledge

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
    shop = services.shop_name()
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
               or services.find_product(query)
               or learned_alias_in(query))
    choices = []
    if not product:
        product, choices = services.match_part_in_sentence(query, car=car)
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
    from ..wakename import normalize

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


# Saying a choice of parts: robot/brain/talk.py (the robot's brain says it the same, net down).
from ..brain.talk import choices_said, spoken_name  # noqa: E402,F401


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
    # 🐛 [FIX]: «واحد اتنين تلاتة» (رقم سري اتقال بصوت) بقت «123» واتعرضت قطعة في اسمها الرقم ده. أرقام
    # بس وقصيرة مش اسم قطعة (the brain the same).
    if not keys or all(w.isdigit() for w in keys) and len("".join(keys)) < CODE_IN_TEXT:
        return None, []
    name_q = Q()
    for w in keys[:8]:
        for spelling in {s for form in _forms(w) for s in _spellings(form)}:
            name_q |= Q(name__icontains=spelling)
    from ..brain.knowledge import the_part

    def named(word):
        spelled = Q()
        for spelling in {s for form in _forms(word) for s in _spellings(form)}:
            spelled |= Q(name__icontains=spelling)
        return any(_forms(word) & set(_part_words(n))
                   for n in Product.objects.filter(spelled, is_active=True).values_list("name", flat=True)[:30])

    word = the_part(keys, named)
    noun = _forms(word) if word else set()
    scored = []
    for product in Product.objects.filter(name_q, is_active=True)[:200]:
        name_words = set(_part_words(product.name))
        hits = sum(1 for w in keys if _forms(w) & name_words)
        # 🐛 [FIX]: «فانوس خلفي X3» عرض تيل ومساحة ومساعد («خلفي» و«X3» في أساميهم): القطعة اللي
        # اسمها مافيهوش الكلمة اللي بتسمّي المطلوب مبتتعرضش (the brain the same: knowledge.the_part).
        if not hits or (noun and not noun & name_words):
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
    from ..models import RobotScanEvent

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
    from ..models import MotorCommandLog

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
