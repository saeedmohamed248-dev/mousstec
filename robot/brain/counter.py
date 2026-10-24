"""The counter page: «مخ موس» on the shop's Wi-Fi, for when the ERP can't be
reached (and works the same when it can).

Open http://<the mini PC's address>:8765 on a phone. An employee picks their
name and types the PIN a manager set on the robot's page; their role (sent
by the server with the catalog) says what they may do:

* 🧾 a cash sale — parts from the catalog on disk at their retail price, never
  more than what's left (the catalog's stock minus what was sold here since,
  ledger.py), one receipt;
* 💸 an expense — recorded at once, the manager reviews it later;
* 📦 a count — the counted quantity of a part, for a stock-take.

Each one goes to the outbox and reaches the server (/sync/push/) as soon as
the net is there — applied once, through the ERP's own pipeline
(robot/offline_ledger.py). Cash only: offline nobody can check a customer's
account. The rules are the owner's; see robot/README.md.

Standard library only (http.server): it has to work with the net down, so
nothing is loaded from the internet, not even a font.
"""

import html
import secrets
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import pins
from .knowledge import _ARABIC_DIGITS, part_words

SESSION_S = 12 * 3600                # a shift
TRIES, LOCK_S = 5, 300               # wrong PINs from one address, then a pause
MAX_AMOUNT = Decimal("1000000")
SHOWN = 20                           # search results
COOKIE = "mouss_counter"
ACTIONS = {"sale": "🧾 بيع كاش", "expense": "💸 مصروف", "stock_take": "📦 جرد"}


def money(value) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    text = f"{amount:,.2f}".replace(".00", "")
    return f"{text} ج"


def _decimal(text: str):
    try:
        return Decimal(str(text or "").translate(_ARABIC_DIGITS).replace(",", "").strip())
    except InvalidOperation:
        return None


def _int(text: str):
    text = str(text or "").translate(_ARABIC_DIGITS).strip()
    return int(text) if text.isdigit() else None


class Counter:
    """The page's state and rules — the HTTP handler below only reads forms
    and writes HTML around these."""

    def __init__(self, knowledge, outbox, ledger, *, online=lambda: None, log=print, clock=time.time):
        self.k, self.outbox, self.ledger = knowledge, outbox, ledger
        self.online, self.log, self.clock = online, log, clock
        self.sessions: dict = {}
        self.failures: dict = {}
        self._lock = threading.Lock()

    # ---- who
    def staff(self) -> list:
        return [s for s in (self.k.catalog.get("staff") or [])
                if isinstance(s, dict) and s.get("id") and s.get("pin") and s.get("name")]

    def login(self, address: str, employee_id, pin: str):
        """(session token, '') — or (None, why not)."""
        now = self.clock()
        with self._lock:
            tries, until = self.failures.get(address, (0, 0))
            if until > now:
                return None, f"محاولات غلط كتير. استنى {int(until - now) // 60 + 1} دقيقة."
        who = next((s for s in self.staff() if str(s["id"]) == str(employee_id)), None)
        if who is None or not pins.check_pin(str(pin or "").translate(_ARABIC_DIGITS), who["pin"]):
            with self._lock:
                tries += 1
                self.failures[address] = (0, now + LOCK_S) if tries >= TRIES else (tries, 0)
            return None, "الاسم أو الرقم السري غلط."
        token = secrets.token_urlsafe(24)
        with self._lock:
            self.failures.pop(address, None)
            self.sessions[token] = {"who": who, "cart": [], "csrf": secrets.token_urlsafe(16),
                                    "until": now + SESSION_S, "last": None}
        self.log(f"[COUNTER] {who['name']} logged in from {address}")
        return token, ""

    def session(self, token):
        with self._lock:
            sess = self.sessions.get(token or "")
            if sess and sess["until"] < self.clock():
                self.sessions.pop(token, None)
                return None
            if sess:                          # still allowed? (a newer catalog may have changed it)
                fresh = next((s for s in self.staff() if s["id"] == sess["who"]["id"]), None)
                if fresh is None:
                    self.sessions.pop(token, None)
                    return None
                sess["who"] = fresh
            return sess

    def logout(self, token) -> None:
        with self._lock:
            self.sessions.pop(token or "", None)

    @staticmethod
    def may(sess, action: str) -> bool:
        return action in (sess["who"].get("can") or [])

    # ---- what's there
    def part(self, part_number: str):
        return next((p for p in self.k.parts if p.part_number == part_number), None)

    def left(self, part, cart=()) -> int:
        """In stock here: the catalog's count minus what was sold here since
        (and what's already in this cart)."""
        in_cart = sum(line["qty"] for line in cart if line["part_number"] == part.part_number)
        return int(part.stock or 0) - self.ledger.sold().get(part.part_number, 0) - in_cart

    def search(self, query: str) -> list:
        words = [w for w in part_words(query) if w]
        if not words:
            return []
        found = []
        for part in self.k.parts:
            if not part.active:
                continue
            haystack = " ".join(part_words(" ".join([part.name, part.car_model, part.part_number,
                                                     part.barcode] + part.extra_codes)))
            if all(w in haystack for w in words):
                found.append(part)
                if len(found) >= SHOWN:
                    break
        return found

    # ---- doing
    def add(self, sess, part_number: str, qty) -> str:
        if not self.may(sess, "sale"):
            return "مش مسموحلك تبيع."
        part = self.part(part_number)
        if part is None or not part.active:
            return "الصنف ده مش موجود."
        if not part.retail_price or part.retail_price <= 0:
            return f"{part.name} سعره مش متسجل — بيعه من السيستم لما النت يرجع."
        if qty is None or qty < 1:
            return "الكمية لازم تكون 1 أو أكتر."
        left = self.left(part, sess["cart"])
        if qty > left:                        # the owner's rule: what's out isn't sold
            return f"المتاح من {part.name} {max(left, 0)} بس."
        for line in sess["cart"]:
            if line["part_number"] == part.part_number:
                line["qty"] += qty
                return ""
        sess["cart"].append({"part_number": part.part_number, "name": part.name, "qty": qty,
                             "unit_price": str(Decimal(str(part.retail_price)))})
        return ""

    def remove(self, sess, part_number: str) -> None:
        sess["cart"] = [line for line in sess["cart"] if line["part_number"] != part_number]

    def confirm(self, sess, customer_name: str = ""):
        """(receipt, '') — the sale is in the outbox; or (None, why not)."""
        if not self.may(sess, "sale"):
            return None, "مش مسموحلك تبيع."
        if not sess["cart"]:
            return None, "الفاتورة فاضية."
        for line in sess["cart"]:             # checked again: another phone may have sold it
            part = self.part(line["part_number"])
            others = [o for o in sess["cart"] if o is not line]
            if part is None or line["qty"] > self.left(part, others):
                return None, f"المتاح من {line['name']} مبقاش مكفي — راجع الفاتورة."
        now = self.clock()
        receipt = "OFF-" + datetime.fromtimestamp(now).strftime("%y%m%d-%H%M%S") + "-" + secrets.token_hex(2).upper()
        total = sum(Decimal(line["unit_price"]) * line["qty"] for line in sess["cart"])
        payload = {"employee_id": sess["who"]["id"], "items": [dict(line) for line in sess["cart"]],
                   "customer_name": str(customer_name or "").strip()[:80], "receipt": receipt, "at": now}
        self.outbox.add("sale", payload)
        sess["last"] = {"receipt": receipt, "items": payload["items"], "total": total, "at": now,
                        "customer_name": payload["customer_name"]}
        sess["cart"] = []
        self.log(f"[COUNTER] sale {receipt}: {money(total)} by {sess['who']['name']}")
        return sess["last"], ""

    def expense(self, sess, amount, description: str) -> str:
        if not self.may(sess, "expense"):
            return "مش مسموحلك تسجّل مصروف."
        description = str(description or "").strip()[:200]
        if amount is None or amount <= 0 or amount > MAX_AMOUNT:
            return "المبلغ مش مظبوط."
        if not description:
            return "اكتب المصروف ده لإيه."
        self.outbox.add("expense", {"employee_id": sess["who"]["id"], "amount": str(amount.quantize(Decimal("0.01"))),
                                    "description": description, "at": self.clock()})
        self.log(f"[COUNTER] expense {money(amount)} ({description}) by {sess['who']['name']}")
        return ""

    def count(self, sess, part_number: str, counted) -> str:
        if not self.may(sess, "stock_take"):
            return "مش مسموحلك تجرد."
        part = self.part(part_number)
        if part is None:
            return "الصنف ده مش موجود."
        if counted is None or counted < 0:
            return "العدد مش مظبوط."
        self.outbox.add("count", {"query": part.part_number, "counted_qty": counted,
                                  "employee_id": sess["who"]["id"], "at": self.clock()})
        return ""


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------

_STYLE = """
body{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px;}
a{color:#67e8f9} h1{font-size:1.4rem;margin:.2rem 0 1rem} .box{background:#1e293b;border-radius:12px;padding:12px;margin:10px 0}
input,select,button{font:inherit;font-size:1.05rem;padding:10px;border-radius:10px;border:1px solid #334155;
background:#0b1220;color:#e2e8f0;box-sizing:border-box} input,select{width:100%} button{background:#0891b2;border:0;color:#fff;
font-weight:700;cursor:pointer} .big{display:block;width:100%;margin:8px 0;padding:16px;font-size:1.2rem;text-align:center;
text-decoration:none;background:#0e7490;border-radius:12px;color:#fff} .err{background:#7f1d1d;padding:10px;border-radius:10px}
.ok{background:#14532d;padding:10px;border-radius:10px} table{width:100%;border-collapse:collapse} td,th{padding:6px;border-bottom:1px solid #334155;text-align:right}
.muted{color:#94a3b8;font-size:.9rem} .row{display:flex;gap:8px;align-items:center} .row>*{flex:1} .ltr{direction:ltr;unicode-bidi:embed}
.danger{background:#9f1239}
"""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _page(title: str, body: str, *, status_line: str = "") -> str:
    return (f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title>'
            f"<style>{_STYLE}</style></head><body>{status_line}{body}</body></html>")


def _hidden(sess) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(sess["csrf"])}">'


class _Handler(BaseHTTPRequestHandler):
    counter: Counter = None               # set per server

    def log_message(self, fmt, *args):    # the brain logs what matters itself
        pass

    # ---- plumbing
    def _token(self) -> str:
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        return jar[COOKIE].value if COOKIE in jar else ""

    def _form(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        data = parse_qs(self.rfile.read(length).decode("utf-8", "replace")) if length else {}
        return {k: v[0] for k, v in data.items()}

    def _send(self, body: str, *, status=200, cookie=None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, where: str, *, cookie=None) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _status(self) -> str:
        c = self.counter
        online = c.online()
        net = "🟢 النت شغال" if online else ("🔴 النت فاصل" if online is False else "⚪ لسه بيشوف النت")
        waiting = len(c.outbox.pending())
        tail = f" · مستني يتبعت: {waiting}" if waiting else ""
        return f'<p class="muted">{net}{tail}</p>'

    # ---- routes
    def do_GET(self):
        url = urlparse(self.path)
        sess = self.counter.session(self._token())
        if sess is None:
            return self._send(self._login_page())
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        pages = {"/": self._home, "/sale": self._sale, "/expense": self._expense, "/count": self._count,
                 "/receipt": self._receipt}
        page = pages.get(url.path)
        if page is None:
            return self._send(_page("مش موجودة", "<p>الصفحة دي مش موجودة.</p>"), status=404)
        return self._send(page(sess, query))

    def do_POST(self):
        url = urlparse(self.path)
        form = self._form()
        c = self.counter
        if url.path == "/login":
            token, error = c.login(self.client_address[0], form.get("employee"), form.get("pin"))
            if token is None:
                return self._send(self._login_page(error), status=403)
            return self._redirect("/", cookie=f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_S}")
        token = self._token()
        sess = c.session(token)
        if sess is None:
            return self._redirect("/")
        if not secrets.compare_digest(form.get("csrf", ""), sess["csrf"]):
            return self._send(_page("ممنوع", "<p class='err'>الطلب ده مش من الصفحة.</p>"), status=403)
        if url.path == "/logout":
            c.logout(token)
            return self._redirect("/", cookie=f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        if url.path == "/sale/add":
            error = c.add(sess, form.get("part", ""), _int(form.get("qty", "1")))
            return self._send(self._sale(sess, {"q": form.get("q", "")}, error=error), status=400 if error else 200)
        if url.path == "/sale/remove":
            c.remove(sess, form.get("part", ""))
            return self._redirect("/sale")
        if url.path == "/sale/confirm":
            receipt, error = c.confirm(sess, form.get("customer_name", ""))
            if receipt is None:
                return self._send(self._sale(sess, {}, error=error), status=400)
            return self._redirect("/receipt")
        if url.path == "/expense":
            error = c.expense(sess, _decimal(form.get("amount")), form.get("description", ""))
            return self._send(self._expense(sess, {}, error=error, done=not error), status=400 if error else 200)
        if url.path == "/count":
            error = c.count(sess, form.get("part", ""), _int(form.get("counted", "")))
            return self._send(self._count(sess, {"q": form.get("q", "")}, error=error, done=not error),
                              status=400 if error else 200)
        return self._send(_page("مش موجودة", "<p>الصفحة دي مش موجودة.</p>"), status=404)

    # ---- pages
    def _login_page(self, error: str = "") -> str:
        staff = self.counter.staff()
        if not staff:
            body = ("<h1>🤖 كاشير موس</h1><div class='box'>مفيش موظفين ليهم رقم سري لسه. "
                    "المدير يحطه من صفحة الروبوت في السيستم («🔢 أرقام الكاشير»)، والروبوت بياخدها "
                    "مع الكتالوج وهو على النت.</div>")
            return _page("كاشير موس", body)
        options = "".join(f'<option value="{_e(s["id"])}">{_e(s["name"])}</option>' for s in staff)
        err = f"<p class='err'>{_e(error)}</p>" if error else ""
        body = (f"<h1>🤖 كاشير موس</h1>{err}<form method='post' action='/login' class='box'>"
                f"<label>الاسم<select name='employee'>{options}</select></label><br><br>"
                "<label>الرقم السري<input name='pin' type='password' inputmode='numeric' "
                "autocomplete='off' maxlength='6' required></label><br><br>"
                "<button class='big' type='submit'>دخول</button></form>")
        return _page("كاشير موس", body)

    def _home(self, sess, _q) -> str:
        links = {"sale": "/sale", "expense": "/expense", "stock_take": "/count"}
        buttons = "".join(f"<a class='big' href='{links[a]}'>{label}</a>" for a, label in ACTIONS.items()
                          if Counter.may(sess, a))
        body = (f"<h1>أهلاً {_e(sess['who']['name'])}</h1>{buttons or '<p>مفيش حاجة مسموحلك بيها هنا.</p>'}"
                f"<form method='post' action='/logout'>{_hidden(sess)}<button class='danger' type='submit'>خروج</button></form>")
        return _page("كاشير موس", body, status_line=self._status())

    def _results(self, sess, query: str, action: str) -> str:
        if not query:
            return ""
        rows = []
        for part in self.counter.search(query):
            left = self.counter.left(part, sess["cart"] if action == "/sale/add" else ())
            price = money(part.retail_price) if part.retail_price else "مش متسجل"
            field = ("<input name='qty' value='1' inputmode='numeric' style='width:5em'>" if action == "/sale/add"
                     else "<input name='counted' inputmode='numeric' placeholder='العدد' style='width:6em' required>")
            rows.append(f"<tr><td>{_e(part.label)}<div class='muted ltr'>{_e(part.part_number)}</div></td>"
                        f"<td>{_e(price)}</td><td>{left}</td><td><form method='post' action='{action}' class='row'>"
                        f"{_hidden(sess)}<input type='hidden' name='part' value='{_e(part.part_number)}'>"
                        f"<input type='hidden' name='q' value='{_e(query)}'>{field}<button type='submit'>+</button></form></td></tr>")
        if not rows:
            return "<p class='muted'>ملقتش حاجة بالاسم أو الرقم ده.</p>"
        return ("<table><tr><th>الصنف</th><th>السعر</th><th>المتاح</th><th></th></tr>" + "".join(rows) + "</table>")

    def _search_box(self, path: str, query: str) -> str:
        return (f"<form method='get' action='{path}' class='row box'><input name='q' value='{_e(query)}' "
                "placeholder='اسم القطعة أو رقمها' autofocus><button type='submit'>دوّر</button></form>")

    def _sale(self, sess, q, *, error: str = "") -> str:
        if not Counter.may(sess, "sale"):
            return _page("بيع", "<p class='err'>مش مسموحلك تبيع.</p><a href='/'>رجوع</a>")
        query = q.get("q", "")
        cart = sess["cart"]
        lines = "".join(
            f"<tr><td>{_e(line['name'])}</td><td>{line['qty']}</td><td>{_e(money(Decimal(line['unit_price']) * line['qty']))}</td>"
            f"<td><form method='post' action='/sale/remove'>{_hidden(sess)}<input type='hidden' name='part' "
            f"value='{_e(line['part_number'])}'><button class='danger' type='submit'>✕</button></form></td></tr>"
            for line in cart)
        total = sum(Decimal(line["unit_price"]) * line["qty"] for line in cart)
        bill = ("<div class='box'><h2>الفاتورة (كاش)</h2><table>" + lines + "</table>"
                f"<p><b>الإجمالي: {_e(money(total))}</b></p><form method='post' action='/sale/confirm'>{_hidden(sess)}"
                "<input name='customer_name' placeholder='اسم العميل (اختياري)' maxlength='80'><br><br>"
                "<button class='big' type='submit'>✅ اتدفع كاش — سجّل</button></form></div>") if cart else ""
        err = f"<p class='err'>{_e(error)}</p>" if error else ""
        body = (f"<a href='/'>رجوع</a><h1>🧾 بيع كاش</h1>{err}{bill}{self._search_box('/sale', query)}"
                f"{self._results(sess, query, '/sale/add')}")
        return _page("بيع كاش", body, status_line=self._status())

    def _receipt(self, sess, _q) -> str:
        last = sess.get("last")
        if not last:
            return _page("إيصال", "<p>مفيش إيصال.</p><a href='/sale'>بيع جديد</a>")
        rows = "".join(f"<tr><td>{_e(i['name'])}</td><td>{i['qty']}</td><td>{_e(money(Decimal(i['unit_price']) * i['qty']))}</td></tr>"
                       for i in last["items"])
        when = datetime.fromtimestamp(last["at"]).strftime("%Y-%m-%d %H:%M")
        customer = f"<p>العميل: {_e(last['customer_name'])}</p>" if last.get("customer_name") else ""
        body = (f"<div class='ok'>✅ اتسجّل — هيتبعت للسيستم أول ما النت يكون شغال.</div><div class='box'>"
                f"<h2>Mouss Tec — إيصال <span class='ltr'>{_e(last['receipt'])}</span></h2>"
                f"<p class='muted'><span dir='ltr'>{_e(when)}</span> · {_e(sess['who']['name'])}</p>{customer}"
                f"<table>{rows}</table><p><b>الإجمالي (كاش): {_e(money(last['total']))}</b></p></div>"
                "<a class='big' href='/sale'>بيع جديد</a><a href='/'>الرئيسية</a>")
        return _page("إيصال", body, status_line=self._status())

    def _expense(self, sess, _q, *, error: str = "", done: bool = False) -> str:
        if not Counter.may(sess, "expense"):
            return _page("مصروف", "<p class='err'>مش مسموحلك تسجّل مصروف.</p><a href='/'>رجوع</a>")
        note = (f"<p class='err'>{_e(error)}</p>" if error else
                ("<p class='ok'>✅ المصروف اتسجّل، والمدير هيراجعه.</p>" if done else ""))
        body = (f"<a href='/'>رجوع</a><h1>💸 مصروف</h1>{note}<form method='post' action='/expense' class='box'>"
                f"{_hidden(sess)}<label>المبلغ (جنيه)<input name='amount' inputmode='decimal' required></label><br><br>"
                "<label>لإيه<input name='description' maxlength='200' placeholder='بنزين، أكل، نقل…' required></label><br><br>"
                "<button class='big' type='submit'>سجّل المصروف</button></form>")
        return _page("مصروف", body, status_line=self._status())

    def _count(self, sess, q, *, error: str = "", done: bool = False) -> str:
        if not Counter.may(sess, "stock_take"):
            return _page("جرد", "<p class='err'>مش مسموحلك تجرد.</p><a href='/'>رجوع</a>")
        query = q.get("q", "")
        note = (f"<p class='err'>{_e(error)}</p>" if error else
                ("<p class='ok'>✅ العدد اتسجّل في الجرد، والمدير بيعتمد الفروق.</p>" if done else ""))
        body = (f"<a href='/'>رجوع</a><h1>📦 جرد</h1>{note}{self._search_box('/count', query)}"
                f"{self._results(sess, query, '/count')}")
        return _page("جرد", body, status_line=self._status())


def serve(counter: Counter, host: str = "0.0.0.0", port: int = 8765) -> ThreadingHTTPServer:
    """Start the page in a background thread; returns the server (.shutdown() to stop)."""
    handler = type("CounterHandler", (_Handler,), {"counter": counter})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
