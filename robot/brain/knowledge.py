"""What the robot knows with the internet down: the branch's whole stock and
retail prices, and the answers the server would give.

The catalog is the server's own retail-only snapshot (/sync/pull/), kept on
disk, so the robot knows the stock from the moment it starts — net or not.
The matching and the sentences are the server's (robot/services.py
`inventory_answer` + `match_part_in_sentence`, robot/views.py `_handle_voice`,
robot/wakename.py), ported line for line: robot/tests/test_brain_offline_db.py
asks both the same questions and expects the same words — follow-ups too
(«التانية», «وبكام؟», «عليه ضمان؟»: talk.py, the server's own rules). What
only the server can do (turning the head to the shelf, other branches, the AI
for general questions, stock-takes, English) waits for the net.
"""

import json
import re
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

from .slang import FILLERS, forms, rewrite

# ---- robot/wakename.py
CONVERSATION_SECONDS = 20
DEFAULT_ALIASES = {
    "موس": ["موص", "مووس", "ماوس", "mouss", "mous", "moss", "mouse", "moose", "mose"],
}
_DIACRITICS = re.compile(r"[ً-ْٰـ]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_VOCATIVES = ("يا", "ياا", "hey", "hi", "ya", "o")
# Mishearings that are also real words: the name only as the sentence's first
# word, or right after «يا» (robot/wakename._OPENING_ALIASES).
_OPENING_ALIASES = {"موس": ["موسي", "موسا", "موز", "يمس", "يموس"]}
_AFTER_YA_ONLY = {"موس": ["موسيقي", "موسيقا"]}


def normalize(text: str) -> str:
    t = _DIACRITICS.sub("", (text or "").lower())
    t = (t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
          .replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي"))
    t = _NON_WORD.sub(" ", t)
    return " ".join(t.split())


def strip_name(names: list, transcript: str):
    """(addressed, the rest in its original spelling) — robot/wakename.strip_name."""
    raw = (transcript or "").split()
    norm = [normalize(w) for w in raw]
    split = [n.split() for n in names]
    for i in range(len(norm)):
        for parts in split:
            if len(parts) == 1 and norm[i] == "يا" + parts[0]:
                return True, " ".join(raw[:i] + raw[i + 1:]).strip(" ،,.!؟?:-")
            if norm[i:i + len(parts)] == parts:
                start = i - 1 if i > 0 and norm[i - 1] in _VOCATIVES else i
                rest = raw[:start] + raw[i + len(parts):]
                return True, " ".join(rest).strip(" ،,.!؟?:-")
    # 🐛 [FIX]: «موسى زيك فيه، …» (Whisper) and «يمس عندك فلترا زيت» (Vosk)
    # were «يا موس، …» taken for chatter.
    base = normalize(names[0]) if names else "موس"
    after_ya = 1 if norm and norm[0] in _VOCATIVES else 0
    if len(norm) > after_ya and (norm[after_ya] in _OPENING_ALIASES.get(base, [])
                                 or (after_ya and norm[1] in _AFTER_YA_ONLY.get(base, []))):
        return True, " ".join(raw[after_ya + 1:]).strip(" ،,.!؟?:-")
    return False, (transcript or "").strip()


# ---- robot/assistant.py: commands that need to know who's talking (the
# server checks the face and the role) — offline they're declined, not
# mistaken for a part name.
# Commands are said as commands: the verb first (after «ممكن» / «لو سمحت» and
# the like), its object right after it. Matched anywhere, «بسأل على كاتم صوت»
# — «على» normalizes to «علي» — was a volume command, and «الزيت قرب يخلص»
# (a customer's oil) was the staff's stock question.
_LEAD = re.compile(r"^((ممكن|لو سمحت|من فضلك|يا ريت|ياريت|please|طب|طيب|بقولك|معلش|بالله)\s+)+")
_VERB_PLAY = r"(?:ت|ن)?(?:شغل|شغلي|شغلنا|شغللنا|افتح|افتحلنا|حط|حطلنا|play)"
_VERB_STOP = r"(?:(?:ت|ن)?(?:وقف|اوقف|اقفل|بطل|اسكت|اطفي|stop)|كفايه)"
_VERB_UP = r"(?:ت|ن)?(?:علي|ارفع|زود|علا|عليلي|علينا)"
_VERB_DOWN = r"(?:ت|ن)?(?:وطي|قلل|خفض|نزل|وطيلي|وطينا)"
_GAP = r"(?:\s+\S+){0,2}?\s+"
_PLAY = re.compile(rf"^{_VERB_PLAY}{_GAP}(?:ال)?(?P<what>قران|قرءان|سوره|ايات|موسيقي|موسيقا|مزيكا|ميوزك|اغاني|quran|music)")
_STOP = re.compile(rf"^{_VERB_STOP}{_GAP}(?:ال)?(?:صوت|قران|موسيقي|موسيقا|مزيكا|تشغيل|اغاني|music|quran)(?:\s|$)")
_LOUDER = re.compile(rf"^{_VERB_UP}{_GAP}(?:ال)?صوت(?:\s|$)")
_QUIETER = re.compile(rf"^{_VERB_DOWN}{_GAP}(?:ال)?صوت(?:\s|$)")
_TODAY = r"(النهارده|انهارده|النهاردا|اليوم|today)"
_SALES = re.compile(rf"^(?:(?:احنا|هو)\s+)?(?:بعنا|بيعنا|اتباع|المبيعات|مبيعات|sold|sales).*{_TODAY}"
                    rf"|^{_TODAY}.*(?:بعنا|بيعنا|اتباع|مبيعات|sold|sales)")
_LOW = re.compile(r"^(?:(?:ايه|ايش|اي|انهي)\s+(?:اللي|الحاجات اللي|الاصناف اللي|الحاجات|الاصناف)?\s*"
                  r"(?:قرب|قربت|قربو|قربوا)\s+(?:ي|ت)?خلص"
                  r"|(?:ايه\s+)?(?:ال)?نواقص|ايه\s+(?:ال)?ناقص|(?:ال)?حاجات\s+(?:ال)?ناقصه|low stock)")



# robot/entries.py: an expense, a purchase or a sale said to be written down.
_ENTRY = re.compile(r"^(?:(?:سجل|سجلي|اعمل|اكتب|ضيف|اضف)\s+)?(?:مصروف|مصاريف|صرف|صرفنا|صرفت|دفعنا|دفعت"
                    r"|فاتوره شراء|فاتوره بيع|شراء|مشتريات|اشترينا|اشتريت|بيع|بعت|بعنا|بعتله|بعتلها)\s")


def _said(text: str) -> str:
    return _LEAD.sub("", normalize(text))


def media_command(text: str):
    """The same parse as robot/assistant.media_command."""
    t = _said(text)
    if _STOP.search(t):
        return {"do": "stop", "what": ""}
    if _LOUDER.search(t):
        return {"do": "volume", "what": "up"}
    if _QUIETER.search(t):
        return {"do": "volume", "what": "down"}
    m = _PLAY.search(t)
    if m:
        quran = m.group("what") in ("قران", "قرءان", "سوره", "ايات", "quran")
        return {"do": "play", "what": "quran" if quran else "calm"}
    return None


def entry_command(text: str) -> bool:
    """«سجل مصروف 200 …», «اشترينا …», «بعت 2 فلتر زيت كاش» — the books, which
    need the server (who's speaking, the treasury, the branch question)."""
    return bool(_ENTRY.search(_said(text) + " "))


def staff_question(text: str):
    t = _said(text)
    if _SALES.search(t):
        return "sales_today"
    if _LOW.search(t):
        return "low_stock"
    return None


# ---- robot/services.py
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ARTICLES = ("وال", "بال", "فال", "لل", "ال")
_QUESTION_WORDS = frozenset("""
    يا عندك عندكو عندكم عندكوا فيه في هل كام بكام وبكام سعر سعره سعرها بسعر تمن
    تمنه تمنها بتاع بتاعه بتاعت عايز عاوز عايزه عاوزه محتاج محتاجه ممكن لو سمحت
    فضلك من على عن لي ليا ده دي دا دول كده موجود موجوده متوفر متوفره متاح ايه اي
    اللي انا احنا انت عربيه عربيتي قطعه حته و او مع هو هي طب طيب بي ام دبليو
    do you have got any how much is the a an price of for please what bmw
""".split())
_QUESTION_WORDS = _QUESTION_WORDS | FILLERS          # robot/brain/slang.py


def part_words(text: str) -> list:
    words = []
    for w in rewrite(normalize((text or "").translate(_ARABIC_DIGITS))).split():
        for article in _ARTICLES:
            if w.startswith(article) and len(w) - len(article) >= 2:
                w = w[len(article):]
                break
        words.append(w)
    return words


def _forms(word: str) -> set:
    """robot/services._forms: «فلترا» is «فلتر», «كاوتش» is «إطار» (slang.py)."""
    return forms(word)


def _spellings(word: str) -> set:
    out = {word}
    if word.endswith("ه"):
        out.add(word[:-1] + "ة")
    if word.endswith("ي"):
        out.add(word[:-1] + "ى")
    if word.startswith("ا"):
        out |= {"أ" + word[1:], "إ" + word[1:]}
    return out


def _key(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def spoken_price(value) -> str:
    """«450 جنيه و50 قرش» — never rounded."""
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pounds, piasters = divmod(int(amount * 100), 100)
    return f"{pounds} جنيه" + (f" و{piasters} قرش" if piasters else "")


@dataclass
class Part:
    part_number: str = ""
    name: str = ""
    car_model: str = ""
    barcode: str = ""
    active: bool = True
    oem: list = field(default_factory=list)
    additional: list = field(default_factory=list)
    retail_price: float = 0.0
    stock: int = 0
    # What a follow-up asks about (talk.py): «عليه ضمان؟», «ماركته إيه؟»,
    # «بيركب على E90؟», «مكانها فين؟».
    brand: str = ""
    condition: str = ""
    warranty_months: int = 0
    chassis: list = field(default_factory=list)
    shelf: str = ""

    def __post_init__(self):
        self.name_words = set(part_words(self.name))
        self.model_words = set(part_words(self.car_model))

    @property
    def extra_codes(self) -> list:          # oem_cross_reference + additional_part_numbers
        values = []
        for field_ in (self.oem, self.additional):
            if isinstance(field_, str):
                field_ = [field_]
            if isinstance(field_, (list, tuple)):
                values += [str(v).strip().lower() for v in field_ if v and str(v).strip()]
        return values

    @property
    def codes(self) -> set:                 # robot/assistant.part_codes
        own = (self.part_number or "").strip().lower()
        return set(self.extra_codes) | ({own} if own else set())

    @property
    def label(self) -> str:                 # robot/services._choice_label
        model = (self.car_model or "").strip()
        return f"{self.name} ({model})" if model and model not in self.name else self.name

    @property
    def label_segments(self) -> list:
        model = (self.car_model or "").strip()
        return [self.name, model] if model and model not in self.name else [self.name]

    @property
    def choice(self) -> dict:               # robot/memory.choice
        return {"name": self.name, "model": (self.car_model or "").strip()}

    @property
    def models(self) -> list:
        from . import talk
        return talk.models_of(self.chassis, self.car_model)


@dataclass
class Reply:
    text: str                               # what the server would have said
    segments: list                          # the same, as voice-bank clips
    part: Optional[Part] = None
    intent: str = "inventory_query"
    # What the server must hear about when the net is back (outbox.py):
    # {"kind": "restock", "payload": {"part_number", "phone"}}.
    event: Optional[dict] = None


# The sentences of robot/views._handle_voice, cut where the voice bank joins clips.
YES = "أيوه،"
# 🐛 [FIX]: «متوفر (3 قطعة)»، «مش متوفر حالياً في الفرع» كانوا فصحى في ودن الزبون:
# الكلام بقى مصري — «موجود، عندي منه 3 حتت»، «مش موجود هنا دلوقتي».
AVAILABLE = "موجود"
NO_PRICE = "بس سعره مش متسجل، اسأل حد من الموظفين"
NOT_HERE = "مش موجود هنا دلوقتي"
NOT_FOUND = "ملقتش القطعة دي في المخزون. ممكن تقولي رقمها؟"
INSTEAD = "بس عندي بديله"
IN_SHELF = "موجود في الرف"
NEEDS_NET = "الأمر ده محتاج النت علشان أتأكد مين بيكلمني. أول ما يرجع هعمله."
WEB_NEEDS_NET = "النت فاصل دلوقتي، فمقدرش أدوّر. أول ما يرجع اسألني تاني."
ENTRY_NEEDS_NET = "النت فاصل، فمقدرش أسجل بالصوت دلوقتي. سجّلها من صفحة الكاشير على الموبايل."
SEVERAL = "عندي أكتر من صنف:"
WHICH = "تقصد أنهي؟"
MEAN = "تقصد"
SAY_FULL = "قول اسمها كامل أو رقمها."
AT_YOUR_SERVICE = "أيوه، تحت أمرك."
SAY_AGAIN = "مسمعتكش كويس، ممكن تعيد؟"
FIXED = [YES, AVAILABLE, NO_PRICE, NOT_HERE, NOT_FOUND, SEVERAL, WHICH, MEAN, SAY_FULL,
         AT_YOUR_SERVICE, SAY_AGAIN, INSTEAD, NEEDS_NET, IN_SHELF, WEB_NEEDS_NET, ENTRY_NEEDS_NET]
COUNTED_UP_TO = 30                          # «موجود، عندي منه 3 حتت» clips; more is just «موجود»


def pieces(n: int) -> str:
    """«حتة واحدة», «حتتين», «3 حتت», «11 حتة» — counted the Egyptian way."""
    if n == 1:
        return "حتة واحدة"
    if n == 2:
        return "حتتين"
    return f"{n} {'حتت' if n <= 10 else 'حتة'}"


def available(stock: int, upto: Optional[int] = COUNTED_UP_TO) -> str:
    """«موجود، عندي منه 3 حتت» — the count while there's a clip for it
    (`upto`; the server, None, always says it) — else «موجود»."""
    if stock > 0 and (upto is None or stock <= upto):
        return f"{AVAILABLE}، عندي منه {pieces(stock)}"
    return AVAILABLE


def priced(price: float) -> str:
    return f"بسعر {spoken_price(price)}"


def priced_after(price: float) -> str:
    return f"وسعره {spoken_price(price)}"


class Knowledge:
    def __init__(self, catalog: Optional[dict] = None):
        self.update(catalog or {})
        self._heard_at = 0.0                # the follow-up window, like the server's
        # robot/memory.py: the part the conversation is on, the parts just offered.
        self._on: Optional[Part] = None
        self._choices: list = []
        self._restock_for: Optional[Part] = None   # «قولي رقم موبايلك» asked for it
        self._talked_at = -1e9

    # ---- the catalog
    def update(self, catalog: dict) -> None:
        self.catalog = catalog
        self.parts = [Part(**{k: v for k, v in p.items() if k in Part.__dataclass_fields__})
                      for p in catalog.get("parts") or [] if isinstance(p, dict) and p.get("name")]
        self.aliases = list(catalog.get("aliases") or [])
        self.replies = [r for r in catalog.get("replies") or [] if isinstance(r, dict)]   # «الردود الجاهزة»
        wake = catalog.get("wake") or {}
        base = (wake.get("name") or "موس").strip()
        names = [base] + list(wake.get("aliases") or []) + DEFAULT_ALIASES.get(base, [])
        self.names = list(dict.fromkeys(normalize(str(n)) for n in names if normalize(str(n))))
        self.by_number = {}
        for part in self.parts:
            for code in [part.part_number, part.barcode]:
                if code:
                    self.by_number.setdefault(code.lower(), part)
        # 🐛 [FIX]: with the catalog fetched every minute ([robot] answer_here),
        # «وبكام؟» right after one said the price of the part as it was: what
        # the conversation is on follows the new catalog.
        fresh = lambda part: (self.by_number.get((part.part_number or "").lower()) or part) if part else part  # noqa: E731
        if getattr(self, "_on", None) is not None:
            self._on = fresh(self._on)
        if getattr(self, "_choices", None):
            self._choices = [fresh(p) for p in self._choices]
        if getattr(self, "_restock_for", None) is not None:
            self._restock_for = fresh(self._restock_for)

    @classmethod
    def load(cls, path: Path) -> "Knowledge":
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path) -> None:
        """Written whole, then swapped in: a power cut mid-write keeps the old one."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.catalog, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @property
    def generated_at(self) -> str:
        return str(self.catalog.get("generated_at") or "")

    # ---- robot/services.inventory_answer, on the catalog
    def _part(self, part_number: str) -> Optional[Part]:
        return self.by_number.get((part_number or "").lower())

    def _taught(self, query: str) -> Optional[Part]:         # resolve_from_knowledge
        val = _key(query)
        for kind in ("code", "label"):
            for alias in self.aliases:
                if alias.get("kind") == kind and _key(alias.get("key")) == val:
                    part = self._part(alias.get("part_number"))
                    if part:
                        return part
        return None

    def _find(self, query: str) -> Optional[Part]:           # find_product
        q = (query or "").strip()
        if not q:
            return None
        hit = self.by_number.get(q.lower())
        if hit:
            return hit
        low = q.lower()
        for part in self.parts:
            if any(low in code for code in part.extra_codes):
                return part
        for part in self.parts:
            if low in part.name.lower() or low in part.car_model.lower():
                return part
        return None

    def _alias_inside(self, text: str) -> Optional[Part]:    # learned_alias_in
        norm = " " + " ".join(re.sub(r"[^\w\s]", " ", _key(text)).split()) + " "
        if len(norm.strip()) < 3:
            return None
        best_key, best = "", None
        for alias in self.aliases:
            key = str(alias.get("key") or "")
            if alias.get("kind") != "label" or len(key) < 3 or len(key) <= len(best_key):
                continue
            if re.search(r"\s(?:ال|وال|بال|لل|و|ب)?" + re.escape(key) + r"\s", norm):
                part = self._part(alias.get("part_number"))
                if part:
                    best_key, best = key, part
        return best

    def _in_sentence(self, text: str):                      # match_part_in_sentence
        words = part_words(text)
        codes = [w for w in words if len(w) >= 5 and any(c.isdigit() for c in w)]
        digits = "".join(w for w in words if w.isdigit())
        if len(digits) >= 7:
            codes.append(digits)
        for code in codes:
            if code.lower() in self.by_number:
                return self.by_number[code.lower()], []
        keys = [w for w in dict.fromkeys(words) if len(w) >= 2 and w not in _QUESTION_WORDS]
        if not keys:
            return None, []
        spellings = {s for w in keys[:8] for form in _forms(w) for s in _spellings(form)}
        scored = []
        for part in self.parts:
            if not part.active or not any(s in part.name.lower() for s in spellings):
                continue
            hits = sum(1 for w in keys if _forms(w) & part.name_words)
            if not hits:
                continue
            model_hits = sum(1 for w in keys if _forms(w) & part.model_words and not _forms(w) & part.name_words)
            scored.append(((hits, model_hits), hits / max(len(part.name_words), 1), part))
            if len(scored) >= 200:
                break
        if not scored:
            return None, []
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        best = [s for s in scored if s[0] == scored[0][0]]
        (hits, _), coverage, part = best[0]
        if len(best) == 1 and (hits >= 2 or coverage >= 0.5):
            return part, []
        return None, [s[2] for s in best]

    def lookup(self, query: str):
        part = self._taught(query) or self._find(query) or self._alias_inside(query)
        if part:
            return part, []
        return self._in_sentence(query)

    # ---- robot/wakename.gate + the inventory branch of robot/views._handle_voice
    def answer(self, transcript: str, now: Optional[float] = None,
               said: Optional[float] = None) -> Optional[Reply]:
        """The spoken answer, or None when it wasn't said to the robot.
        `said`: when they started saying it — the follow-up window is checked
        then, not after the seconds a slow computer took to hear it."""
        now = time.monotonic() if now is None else now
        addressed, rest = strip_name(self.names, transcript)
        if not addressed and (now if said is None else said) - self._heard_at > CONVERSATION_SECONDS:
            return None
        self._heard_at = now
        text = rest if addressed else transcript.strip()
        if addressed and not rest:
            return Reply(AT_YOUR_SERVICE, [AT_YOUR_SERVICE], intent="wake")
        if not text.strip():
            return Reply(SAY_AGAIN, [SAY_AGAIN], intent="unknown")
        heard = self._restock_hear(text, now)       # robot/restock.hear
        if heard is not None:
            return heard
        if media_command(text) or staff_question(text):
            return Reply(NEEDS_NET, [NEEDS_NET], intent="command")
        if entry_command(text):              # the counter page takes them offline
            return Reply(ENTRY_NEEDS_NET, [ENTRY_NEEDS_NET], intent="command")
        from . import talk
        if talk.web_question(text):          # «دوّر على النت …»: nothing to look in here
            return Reply(WEB_NEEDS_NET, [WEB_NEEDS_NET], intent="command")
        followed = self._follow_up(text, now)
        if followed is not None:
            return followed
        said = self._shop_reply(text)                # robot/replies.answer
        if said is not None:
            return Reply(said, [said], intent="shop_reply")
        part, choices = self.lookup(text)
        if not part and choices:
            picked = choices[:3]
            self._remember(now, part=None, choices=picked)
            labels = [p.label for p in picked]
            # Said as the name and then the model: one clip per name and one
            # per car model cover every choice, not one per pair.
            spoken = [seg for p in picked for seg in p.label_segments]
            if len(labels) == 1:
                return Reply(f"{MEAN} {labels[0]}؟ {SAY_FULL}", [MEAN] + spoken + [SAY_FULL])
            return Reply(f"{SEVERAL[:-1]}: " + "، ".join(labels) + f". {WHICH}",
                         [SEVERAL] + spoken + [WHICH])
        if not part:
            return Reply(NOT_FOUND, [NOT_FOUND])
        return self._part_reply(part, now)

    def _shop_reply(self, text: str) -> Optional[str]:
        """«الردود الجاهزة» the owner wrote, as the server says them — not
        when the rest of what was said is a part («فيه ضمان على الطرمبة؟»)."""
        from . import replies

        found = replies.match(text, self.replies)
        if found is None:
            return None
        reply, rest = found
        if rest:
            part, choices = self.lookup(rest)
            if part or choices:
                return None
        return str(reply.get("answer") or "").strip() or None

    # ---- robot/memory.py: «التانية», «وبكام؟», «عليه ضمان؟», «فيه أرخص؟»
    def _remember(self, now: float, *, part=None, choices=None, keep_part: bool = False) -> None:
        if not keep_part:
            self._on = part
        self._choices = list(choices or [])
        self._talked_at = now

    def _follow_up(self, text: str, now: float) -> Optional[Reply]:
        from . import talk

        if now - self._talked_at > talk.TTL:
            self._on, self._choices = None, []
            return None
        self._talked_at = now                # every turn keeps the conversation (note_turn)
        i = talk.pick_choice(text, [p.choice for p in self._choices])
        if i is not None and self._choices[i].active:
            return self._part_reply(self._choices[i], now)
        part = self._on
        kind = talk.follow_up(text) if part is not None else None
        if kind is None:
            return None
        if kind == "price":
            return self._part_reply(part, now)
        if kind == "branches":                # the other branches aren't in this branch's catalog
            return Reply(*talk.branches(part.name, part.stock or 0, None), part)
        if kind == "where":
            stock = part.stock or 0
            return Reply(*talk.where(part.name, part.shelf if stock > 0 else "", stock), part)
        if kind == "warranty":
            return Reply(*talk.warranty(part.name, part.warranty_months), part)
        if kind == "brand":
            return Reply(*talk.brand(part.name, part.brand, part.condition, part.warranty_months), part)
        if kind == "fits":
            return Reply(*talk.fits(part.name, part.models, talk.models_said(text)), part)
        alts = self.alternatives(part, limit=6)
        said, clips, picked = talk.alternatives(kind, part.name, part.retail_price or 0,
                                                [(a.name, a.retail_price or 0) for a in alts])
        if picked:
            self._remember(now, choices=[alts[i] for i in picked], keep_part=True)
        return Reply(said, clips, part)

    def _restock_hear(self, text: str, now: float) -> Optional[Reply]:
        from . import talk

        part = self._restock_for
        if part is None or now - self._talked_at > talk.TTL:
            return None
        phone = talk.mobile_in(text)
        if phone:
            self._restock_for, self._talked_at = None, now
            said, clips = talk.restock_saved(part.name)
            return Reply(said, clips, part, intent="command",
                         event={"kind": "restock", "payload": {"part_number": part.part_number, "phone": phone}})
        if talk.just_yes(text):
            return Reply(talk.RESTOCK_SAY_IT, [talk.RESTOCK_SAY_IT], intent="command")
        if talk.tried_a_number(text):
            return Reply(talk.RESTOCK_AGAIN, [talk.RESTOCK_AGAIN], intent="command")
        return None

    def _part_reply(self, part: Part, now: float) -> Reply:
        """robot/views._part_reply, on the catalog."""
        from . import talk

        self._remember(now, part=part)
        self._restock_for = None
        price, stock = part.retail_price or 0, part.stock or 0
        if stock > 0:
            said = priced(price) if price > 0 else NO_PRICE
            text = f"أيوه، {part.name} {available(stock)}، {said}."
            segments = [YES, part.name, available(stock), said]
            if part.shelf:
                text += f" {IN_SHELF} {part.shelf}."
                segments += [IN_SHELF, part.shelf]
            return Reply(text, segments, part)
        text = f"{part.name} {NOT_HERE}" + (f"، {priced_after(price)}." if price > 0 else ".")
        segments = [part.name, NOT_HERE] + ([priced_after(price)] if price > 0 else [])
        alt = self.alternative(part)
        if alt is not None:                 # robot/assistant.out_of_stock_tail
            alt_price = alt.retail_price or 0
            text += f" {INSTEAD} {alt.name} {AVAILABLE}" + (f" {priced(alt_price)}." if alt_price > 0 else ".")
            segments += [INSTEAD, alt.name, AVAILABLE] + ([priced(alt_price)] if alt_price > 0 else [])
        else:                               # robot/restock.offer
            text += f" {talk.RESTOCK_ASK}"
            segments.append(talk.RESTOCK_ASK)
            self._restock_for = part
        return Reply(text, segments, part)

    def alternative(self, part: Part) -> Optional[Part]:
        """An in-stock part here sharing a number with `part` — the one the
        server picks (most on hand, then part number). Other branches aren't
        in this branch's catalog: that part of the answer waits for the net."""
        found = self.alternatives(part, limit=1)
        return found[0] if found else None

    def alternatives(self, part: Part, limit: int = 6) -> list:
        """robot/assistant.alternatives_here, on the catalog."""
        codes = part.codes
        if not codes:
            return []
        long_codes = [c for c in codes if len(c) >= 5]

        def found(p):                       # the server's prefilter: own number, or a long code
            if (p.part_number or "").lower() in codes:
                return True
            return any(c in e for c in long_codes for e in p.extra_codes)

        fits = [p for p in self.parts
                if p is not part and p.active and (p.stock or 0) > 0 and found(p) and p.codes & codes]
        fits.sort(key=lambda p: (-(p.stock or 0), p.part_number))
        return fits[:limit]

    # ---- what the voice bank should hold
    def clip_texts(self) -> list:
        """Every clip an offline answer can need — what's in stock first."""
        from . import talk

        texts = list(FIXED) + list(talk.PHRASES)
        texts += [str(r.get("answer") or "").strip() for r in self.replies]    # «الردود الجاهزة»
        texts += [available(n) for n in range(1, COUNTED_UP_TO + 1)]
        texts += [pieces(n) for n in range(1, COUNTED_UP_TO + 1)]     # «في أنهي فرع؟»
        facts = []                                  # after every part's name and price
        for part in sorted(self.parts, key=lambda p: -(p.stock or 0)):
            texts.extend(part.label_segments)       # the name (and the model, for «تقصد أنهي؟»)
            if part.retail_price and part.retail_price > 0:
                texts.append(priced(part.retail_price) if part.stock > 0 else priced_after(part.retail_price))
                if part.stock > 0:                  # «فيه أرخص؟» says it among the others
                    facts.append(priced(part.retail_price))
            facts += [part.shelf, part.brand] + part.models
            if (part.warranty_months or 0) > 0:
                facts.append(talk.months(part.warranty_months))
        return list(dict.fromkeys(t for t in texts + facts if t))
