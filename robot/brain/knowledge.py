"""What the robot knows with the internet down: the branch's whole stock and
retail prices, and the answers the server would give.

The catalog is the server's own retail-only snapshot (/sync/pull/), kept on
disk, so the robot knows the stock from the moment it starts — net or not.
The matching and the sentences are the server's (robot/services.py
`inventory_answer` + `match_part_in_sentence`, robot/views.py `_handle_voice`,
robot/wakename.py), ported line for line: robot/tests/test_brain_offline_db.py
asks both the same questions and expects the same words. What only the
server can do (shelf directions, the AI for general questions, stock-takes,
English) waits for the net.
"""

import json
import re
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

# ---- robot/wakename.py
CONVERSATION_SECONDS = 20
DEFAULT_ALIASES = {
    "موس": ["موص", "مووس", "ماوس", "mouss", "mous", "moss", "mouse", "moose", "mose"],
}
_DIACRITICS = re.compile(r"[ً-ْٰـ]")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_VOCATIVES = ("يا", "ياا", "hey", "hi", "ya", "o")


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
    return False, (transcript or "").strip()


# ---- robot/assistant.py: commands that need to know who's talking (the
# server checks the face and the role) — offline they're declined, not
# mistaken for a part name.
_PLAY = re.compile(r"(^|\s)(شغل|شغلي|شغلنا|شغللنا|افتح|حط|حطلنا|ابدا|play)(\s|$)")
_QURAN = re.compile(r"(قران|قرءان|سوره|سور|ايات|quran)")
_MUSIC = re.compile(r"(موسيقي|موسيقا|مزيكا|ميوزك|music)")
_STOP = re.compile(r"(^|\s)(وقف|اوقف|اقفل|بطل|اسكت|اطفي|كفايه|stop)(\s|$)")
_SOUND = re.compile(r"(صوت|قران|موسيقي|موسيقا|مزيكا|تشغيل|اغاني|music|quran)")
_LOUDER = re.compile(r"(^|\s)(علي|ارفع|زود|علا)(\s|$).*صوت")
_QUIETER = re.compile(r"(^|\s)(وطي|قلل|خفض|نزل|واطي)(\s|$).*صوت")
_TODAY = r"(النهارده|انهارده|النهاردا|اليوم|today)"
_SALES = re.compile(rf"(بعنا|بيعنا|اتباع|مبيعات|المبيعات|sold|sales).*{_TODAY}|{_TODAY}.*(بعنا|بيعنا|اتباع|مبيعات|sold|sales)")
_LOW = re.compile(r"(قرب|قربت|قربو|قربوا)\s+(ي|ت)?خلص|النواقص|نواقص|الناقص|الحاجات الناقصه|low stock")


def media_command(text: str):
    """The same parse as robot/assistant.media_command."""
    t = normalize(text)
    if _STOP.search(t) and _SOUND.search(t):
        return {"do": "stop", "what": ""}
    if _LOUDER.search(t):
        return {"do": "volume", "what": "up"}
    if _QUIETER.search(t):
        return {"do": "volume", "what": "down"}
    if _PLAY.search(t):
        if _QURAN.search(t):
            return {"do": "play", "what": "quran"}
        if _MUSIC.search(t):
            return {"do": "play", "what": "calm"}
    return None


def staff_question(text: str):
    t = normalize(text)
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


def part_words(text: str) -> list:
    words = []
    for w in normalize((text or "").translate(_ARABIC_DIGITS)).split():
        for article in _ARTICLES:
            if w.startswith(article) and len(w) - len(article) >= 2:
                w = w[len(article):]
                break
        words.append(w)
    return words


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

    def __post_init__(self):
        self.name_words = set(part_words(self.name))
        self.model_words = set(part_words(self.car_model))

    @property
    def codes(self) -> set:                 # robot/assistant.part_codes
        values = [self.part_number]
        for field_ in (self.oem, self.additional):
            values += [field_] if isinstance(field_, str) else list(field_ or [])
        return {str(v).strip().lower() for v in values if v and str(v).strip()}

    @property
    def label(self) -> str:                 # robot/services._choice_label
        model = (self.car_model or "").strip()
        return f"{self.name} ({model})" if model and model not in self.name else self.name


@dataclass
class Reply:
    text: str                               # what the server would have said
    segments: list                          # the same, as voice-bank clips
    part: Optional[Part] = None
    intent: str = "inventory_query"


# The sentences of robot/views._handle_voice, cut where the voice bank joins clips.
YES = "أيوه،"
AVAILABLE = "متوفر"
NO_PRICE = "بس سعره مش متسجل، اسأل حد من الموظفين"
NOT_HERE = "مش متوفر حالياً في الفرع"
NOT_FOUND = "ملقتش القطعة دي في المخزون. ممكن تقولي رقمها؟"
INSTEAD = "بس عندي بديله"
NEEDS_NET = "الأمر ده محتاج النت علشان أتأكد مين بيكلمني. أول ما يرجع هعمله."
SEVERAL = "عندي أكتر من صنف:"
WHICH = "تقصد أنهي؟"
MEAN = "تقصد"
SAY_FULL = "قول اسمها كامل أو رقمها."
AT_YOUR_SERVICE = "أيوه، تحت أمرك."
SAY_AGAIN = "مسمعتكش كويس، ممكن تعيد؟"
FIXED = [YES, AVAILABLE, NO_PRICE, NOT_HERE, NOT_FOUND, SEVERAL, WHICH, MEAN, SAY_FULL,
         AT_YOUR_SERVICE, SAY_AGAIN, INSTEAD, NEEDS_NET]
COUNTED_UP_TO = 30                          # «متوفر (3 قطعة)» clips; more is just «متوفر»


def available(stock: int) -> str:
    return f"{AVAILABLE} ({stock} قطعة)" if 0 < stock <= COUNTED_UP_TO else AVAILABLE


def priced(price: float) -> str:
    return f"بسعر {spoken_price(price)}"


def priced_after(price: float) -> str:
    return f"وسعره {spoken_price(price)}"


class Knowledge:
    def __init__(self, catalog: Optional[dict] = None):
        self.update(catalog or {})
        self._heard_at = 0.0                # the follow-up window, like the server's

    # ---- the catalog
    def update(self, catalog: dict) -> None:
        self.catalog = catalog
        self.parts = [Part(**{k: v for k, v in p.items() if k in Part.__dataclass_fields__})
                      for p in catalog.get("parts") or [] if isinstance(p, dict) and p.get("name")]
        self.aliases = list(catalog.get("aliases") or [])
        wake = catalog.get("wake") or {}
        base = (wake.get("name") or "موس").strip()
        names = [base] + list(wake.get("aliases") or []) + DEFAULT_ALIASES.get(base, [])
        self.names = list(dict.fromkeys(normalize(str(n)) for n in names if normalize(str(n))))
        self.by_number = {}
        for part in self.parts:
            for code in [part.part_number, part.barcode]:
                if code:
                    self.by_number.setdefault(code.lower(), part)

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
            if any(low in str(code).lower() for code in part.oem or []):
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
        spellings = {s for w in keys[:8] for s in _spellings(w)}
        scored = []
        for part in self.parts:
            if not part.active or not any(s in part.name.lower() for s in spellings):
                continue
            hits = sum(1 for w in keys if w in part.name_words)
            if not hits:
                continue
            model_hits = sum(1 for w in keys if w in part.model_words and w not in part.name_words)
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
    def answer(self, transcript: str, now: Optional[float] = None) -> Optional[Reply]:
        """The spoken answer, or None when it wasn't said to the robot."""
        now = time.monotonic() if now is None else now
        addressed, rest = strip_name(self.names, transcript)
        if not addressed and now - self._heard_at > CONVERSATION_SECONDS:
            return None
        self._heard_at = now
        text = rest if addressed else transcript.strip()
        if addressed and not rest:
            return Reply(AT_YOUR_SERVICE, [AT_YOUR_SERVICE], intent="wake")
        if not text.strip():
            return Reply(SAY_AGAIN, [SAY_AGAIN], intent="unknown")
        if media_command(text) or staff_question(text):
            return Reply(NEEDS_NET, [NEEDS_NET], intent="command")
        part, choices = self.lookup(text)
        if not part and choices:
            labels = [p.label for p in choices[:3]]
            if len(labels) == 1:
                return Reply(f"{MEAN} {labels[0]}؟ {SAY_FULL}", [MEAN, labels[0], SAY_FULL])
            return Reply(f"{SEVERAL[:-1]}: " + "، ".join(labels) + f". {WHICH}",
                         [SEVERAL] + labels + [WHICH])
        if not part:
            return Reply(NOT_FOUND, [NOT_FOUND])
        price, stock = part.retail_price or 0, part.stock or 0
        if stock > 0:
            said = priced(price) if price > 0 else NO_PRICE
            return Reply(f"أيوه، {part.name} متوفر ({stock} قطعة) {said}.",
                         [YES, part.name, available(stock), said], part)
        text = f"{part.name} {NOT_HERE}" + (f"، {priced_after(price)}." if price > 0 else ".")
        segments = [part.name, NOT_HERE] + ([priced_after(price)] if price > 0 else [])
        alt = self.alternative(part)
        if alt is not None:                 # robot/assistant.out_of_stock_tail
            alt_price = alt.retail_price or 0
            text += f" {INSTEAD} {alt.name} {AVAILABLE}" + (f" {priced(alt_price)}." if alt_price > 0 else ".")
            segments += [INSTEAD, alt.name, AVAILABLE] + ([priced(alt_price)] if alt_price > 0 else [])
        return Reply(text, segments, part)

    def alternative(self, part: Part) -> Optional[Part]:
        """An in-stock part here sharing a number with `part` — the one the
        server picks (most on hand, then part number). Other branches aren't
        in this branch's catalog: that part of the answer waits for the net."""
        codes = part.codes
        if not codes:
            return None
        fits = [p for p in self.parts
                if p is not part and p.active and (p.stock or 0) > 0 and p.codes & codes]
        fits.sort(key=lambda p: (-(p.stock or 0), p.part_number))
        return fits[0] if fits else None

    # ---- what the voice bank should hold
    def clip_texts(self) -> list:
        """Every clip an offline answer can need — what's in stock first."""
        texts = list(FIXED)
        texts += [available(n) for n in range(1, COUNTED_UP_TO + 1)]
        by_name = {}
        for part in self.parts:
            by_name.setdefault(normalize(part.name), []).append(part)
        for part in sorted(self.parts, key=lambda p: -(p.stock or 0)):
            texts.append(part.name)
            if part.retail_price and part.retail_price > 0:
                texts.append(priced(part.retail_price) if part.stock > 0 else priced_after(part.retail_price))
            if len(by_name.get(normalize(part.name), [])) > 1:
                texts.append(part.label)    # asked «تقصد أنهي؟» about
        return list(dict.fromkeys(texts))
