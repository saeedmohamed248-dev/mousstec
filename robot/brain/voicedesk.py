"""A cash sale and an expense, said to the robot with the net down.

Offline the books were the counter page's only («النت فاصل… سجّلها من صفحة الكاشير»). The
same rules, said aloud now (robot/brain/counter.py does the writing, so there's one set):

    «يا موس، سجل مصروف ميتين جنيه بنزين»  → «قولي الرقم السري» → «مصروف ميتين جنيه، أسجله؟» → «أيوه»
    «يا موس، بعت 2 فلتر زيت كاش»          → … → «بيع حتتين فلتر زيت بإجمالي تسعمية جنيه، أسجله؟» → «أيوه»

  • cash only — offline nobody can check a customer's account («الآجل محتاج النت»);
  • a part that's out isn't sold (the counter's own count: the catalog minus what was sold here);
  • who: the employee's PIN (the counter page's), said digit by digit, remembered STAFF_S;
    three wrong ones lock it LOCK_S; what's said for it is never logged (`secret`);
  • nothing is written before «أيوه» to the amount read back — Vosk's ears aren't a teacher's.

It goes to the outbox like a sale on the page: written to the ERP when the net is back
(robot/offline_ledger.py), by the ERP's own posting, or kept as failed with the reason.
Everything else about the books (credit, a collection, a return…) still waits for the net.
"""

import re
import time
from decimal import Decimal

from . import pins, saynumber
from .arabic_numbers import said_digits, to_digits
from .knowledge import ENTRY_NEEDS_NET, Reply, normalize, pieces

STAFF_S = 10 * 60
STEP_S = 90                        # a question unanswered this long: forgotten
TRIES = 3
LOCK_S = 15 * 60
MOST_QTY = 30                      # «3 حتت» clips go this far (knowledge.COUNTED_UP_TO)

ASK_PIN = "قولي الرقم السري بتاعك، رقم رقم."
WRONG_PIN = "الرقم ده مش صح. قوله تاني رقم رقم."
PIN_LOCKED = "اتقال رقم سري غلط كذا مرة. استنى ربع ساعة."
NOT_ALLOWED = "معلش، الأمر ده مش من صلاحياتك."
CREDIT_NEEDS_NET = "الآجل محتاج النت. من غير نت بسجل الكاش بس."
SAY_AMOUNT = "قول المصروف بالمبلغ، مثلاً: سجل مصروف ميتين جنيه بنزين."
WHICH_PART = "مش عارف أنهي قطعة. قول اسمها كامل، أو رقمها."
NOT_ENOUGH = "القطعة دي مش متاحة بالعدد ده، فمسجلتش حاجة."
NO_PRICE_HERE = "القطعة دي سعرها مش متسجل، فمسجلتش حاجة."
DONE = "تمام، سجلته على الجهاز، وهيترحل للسيستم أول ما النت يرجع."
CANCELLED = "ماشي، لغيت ومسجلتش حاجة."
YES_OR_NO = "أسجله؟ قول أيوه أو الغي."
EXPENSE, SALE, TOTAL, POUNDS, RECORD_IT = "مصروف", "بيع", "بإجمالي", "جنيه،", "أسجله؟"
FIXED = [ASK_PIN, WRONG_PIN, PIN_LOCKED, NOT_ALLOWED, CREDIT_NEEDS_NET, SAY_AMOUNT, WHICH_PART, NOT_ENOUGH,
         NO_PRICE_HERE, DONE, CANCELLED, YES_OR_NO, EXPENSE, SALE, TOTAL, POUNDS, RECORD_IT]

_EXPENSE = re.compile(r"^(?:(?:سجل|سجلي|اكتب|ضيف|اضف)\s+)?(?:مصروف|مصاريف|صرفنا|صرفت)(?:\s+(.+))?$")
_SALE = re.compile(r"^(?:(?:سجل|سجلي|اعمل|اكتب)\s+)?(?:فاتوره بيع|بعت|بعنا|بيع)\s+(.+)$")
_CREDIT = re.compile(r"(?:^|\s)(?:اجل|علي الحساب|علي حسابه)(?:\s|$)")
_CASH = re.compile(r"(?:^|\s)(?:كاش|نقدي|نقدا)(?:\s|$)")
_MONEY = re.compile(r"(\d+)\s*(?:جنيه|جنيها|جنيهات)")
_YES = frozenset("ايوه ايوا اه اها تمام ماشي سجل سجله اوكي yes ok".split())
_NO = frozenset("لا لاء الغي الغيه الغيها بلاش كنسل cancel no".split())
_COUNTS = {"واحد": 1, "واحده": 1, "حته": 1, "اتنين": 2, "حتتين": 2, "قطعتين": 2}


def clips() -> list:
    """Every clip these replies need besides the parts' names (the bank fetches them online)."""
    return FIXED + saynumber.clips()


def _said(text: str) -> str:
    return normalize(to_digits(text or ""))


class VoiceDesk:
    """One robot's offline desk. `counter`: robot/brain/counter.Counter — its staff, its stock
    rule, its outbox. `log`: the brain's (never given a PIN)."""

    def __init__(self, counter, lookup, *, offline=lambda: True, log=print, clock=time.time):
        self.counter, self.lookup, self.log, self.clock = counter, lookup, log, clock
        self.offline = offline             # with the net up the server's wizard does all this
        self._step = None                  # {"step": "pin"|"confirm", "want": {...}, "at": t}
        self._staff = None                 # (who, until)
        self._wrong, self._locked_until = 0, 0.0

    # ---- for the brain
    @property
    def secret(self) -> bool:
        """A PIN is being said: what's heard now is never logged."""
        return bool(self._open()) and self._step["step"] == "pin"

    def hear(self, text: str):
        """The reply to `text` when it's the desk's — a request it takes, or the answer to its
        own question — else None (someone else's sentence)."""
        if not self.offline():
            self._step = None
            return None
        said = _said(text)
        step = self._open()
        if step is not None:
            return self._on_pin(text) if step["step"] == "pin" else self._on_confirm(said)
        want = self._wanted(said)
        if want is None:
            return None
        if isinstance(want, Reply):
            return want
        who = self._known()
        if who is None:
            if self.clock() < self._locked_until:
                return _say(PIN_LOCKED)
            self._step = {"step": "pin", "want": want, "at": self.clock()}
            return _say(ASK_PIN)
        return self._confirm(want, who)

    # ---- what's asked for
    def _wanted(self, said: str):
        m = _EXPENSE.match(said)
        if m:
            rest = m.group(1) or ""
            money = _MONEY.search(rest) or re.search(r"(\d+)", rest)
            if not money or not 0 < int(money.group(1)) <= saynumber.MOST:
                return _say(SAY_AMOUNT)
            about = " ".join((rest[:money.start()] + " " + rest[money.end():]).split())
            about = re.sub(r"^(?:علي|عشان|في|ل|لل)\s+", "", about).strip() or "مصروف"
            return {"kind": "expense", "amount": int(money.group(1)), "description": about[:200]}
        m = _SALE.match(said)
        if not m:
            return None
        rest = m.group(1)
        if _CREDIT.search(rest):
            return _say(CREDIT_NEEDS_NET)
        if re.search(r"(?:^|\s)(?:ل|لل)\S+\s", rest) and not _CASH.search(rest):
            return _say(ENTRY_NEEDS_NET)             # «بعت لأحمد …»: an account's business
        rest = _CASH.sub(" ", rest).strip()
        words, qty = rest.split(), 1
        if words and words[0].isdigit():
            qty, words = int(words[0]), words[1:]
        elif words and words[0] in _COUNTS:
            qty, words = _COUNTS[words[0]], words[1:]
        if not words or not 1 <= qty <= MOST_QTY:
            return _say(WHICH_PART)
        part, _choices = self.lookup(" ".join(words))
        if part is None:
            return _say(WHICH_PART)
        return {"kind": "sale", "part": part.part_number, "qty": qty}

    # ---- who's asking
    def _known(self):
        if self._staff and self._staff[1] > self.clock():
            fresh = next((s for s in self.counter.staff() if s["id"] == self._staff[0]["id"]), None)
            if fresh is not None:
                return fresh
        self._staff = None
        return None

    def _on_pin(self, text: str):
        want = self._step["want"]
        if set(_said(text).split()) & _NO:
            self._step = None
            return _say(CANCELLED)
        pin = said_digits(text)
        if not pins.valid(pin):
            self._step["at"] = self.clock()
            return _say(ASK_PIN)
        who = next((s for s in self.counter.staff() if pins.check_pin(pin, s["pin"])), None)
        if who is None:
            self._wrong += 1
            if self._wrong >= TRIES:
                self._wrong, self._locked_until, self._step = 0, self.clock() + LOCK_S, None
                return _say(PIN_LOCKED)
            self._step["at"] = self.clock()
            return _say(WRONG_PIN)
        self._wrong, self._staff = 0, (who, self.clock() + STAFF_S)
        self.log(f"[DESK] {who['name']} said their PIN")
        return self._confirm(want, who)

    # ---- read back, then written
    def _confirm(self, want: dict, who: dict):
        if want["kind"] not in (who.get("can") or []):
            self._step = None
            return _say(NOT_ALLOWED)
        if want["kind"] == "expense":
            said = [EXPENSE] + saynumber.words(want["amount"]) + [POUNDS, RECORD_IT]
        else:
            part = self.counter.part(want["part"])
            if part is None or not part.active:
                self._step = None
                return _say(WHICH_PART)
            if not part.retail_price or part.retail_price <= 0:
                self._step = None
                return _say(NO_PRICE_HERE)
            if want["qty"] > self.counter.left(part):
                self._step = None
                return _say(NOT_ENOUGH)
            total = Decimal(str(part.retail_price)) * want["qty"]
            if total != total.to_integral_value() or total > saynumber.MOST:
                self._step = None
                return _say(ENTRY_NEEDS_NET)         # piastres aren't said from clips: the page's
            said = ([SALE, pieces(want["qty"])] + part.label_segments + [TOTAL]
                    + saynumber.words(int(total)) + [POUNDS, RECORD_IT])
        self._step = {"step": "confirm", "want": want, "who": who, "at": self.clock(), "said": said}
        return Reply(" ".join(said), said, intent="command")

    def _on_confirm(self, said: str):
        step, words = self._step, set(said.split())
        if words & _NO:
            self._step = None
            return _say(CANCELLED)
        if not words & _YES:
            step["at"] = self.clock()
            return _say(YES_OR_NO)
        self._step = None
        want, sess = step["want"], {"who": step["who"], "cart": [], "last": None}
        if want["kind"] == "expense":
            error = self.counter.expense(sess, Decimal(want["amount"]), want["description"])
        else:
            error = (self.counter.add(sess, want["part"], want["qty"])
                     or self.counter.confirm(sess, "")[1])
        if error:
            self.log(f"[DESK] not recorded: {error}")
            return _say(NOT_ENOUGH if want["kind"] == "sale" else NOT_ALLOWED)
        return _say(DONE)

    def _open(self):
        if self._step is not None and self.clock() - self._step["at"] > STEP_S:
            self._step = None
        return self._step


def _say(sentence: str) -> Reply:
    return Reply(sentence, [sentence], intent="command")
