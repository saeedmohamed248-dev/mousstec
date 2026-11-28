"""robot/agent.py — «🧠 المخ بأدوات»: Gemini answering from the system itself.

The rules answer what they know for sure (a part's price, an invoice's steps). What they don't —
«عندكم حاجة لعربيتي ومين أرخص؟», «إيه أكتر حاجة قرّبت تخلص؟», «الفانوس ده موجود في أنهي فرع
وضمانه كام؟», or any subject at all — went to an AI that knew nothing of the shop, and was told to
say «هسأل حد من الموظفين». Here Gemini has tools that read the system: the parts, their stock in
each branch, their brand and warranty and what they fit, the shop's own replies, and — for the staff
who may — the day's numbers and the accounts. It calls what it needs, and says what came back in
Egyptian Arabic.

* Read only. A sale, a purchase, stock: the robot's own steps do those, with their PIN
  (robot/wizard.py) — Gemini tells how to start one, it never does it.
* A price is only ever a tool's: an answer with a sum of money no tool gave is dropped (as
  services.ai_reply does), and wholesale/cost never reaches it (pricing.safe_product_payload).
* The staff's numbers keep their roles: a tool answers what assistant.staff_answer answers the
  person in front of the camera — a customer hears it's for the staff.
* ROBOT_AGENT=0 in .env turns it off: services.ai_reply, as before (off in the tests that don't ask for it).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("mouss_tec_core")

STEPS = 4                  # tool rounds before an answer
TIMEOUT_S = 6              # one model, one round
BUDGET_S = 9               # all of it: the customer is waiting
PARTS_SAID = 5
LONGEST = 450

PROMPT = """\
إنت «{robot}»، روبوت في محل{shop} لقطع غيار BMW و MINI في مصر. بتتكلم بالعامية المصرية الطبيعية
زي بياع شاطر وودود، وردك هيتقال بصوت: جملتين أو تلاتة قصيرين، من غير قوايم ولا علامات ولا إيموجي.
عندك أدوات بتقرا من سيستم المحل نفسه — استعملها قبل ما ترد على أي حاجة عن القطع أو المخزون أو
الفروع أو المحل أو الحسابات:
- أي سعر أو كمية أو فرع أو ضمان تقوله لازم يكون من نتيجة أداة بالظبط. لو الأداة مرجعتش حاجة قول
  كده بصراحة واطلب اسم القطعة أو رقمها أو عربيته. متخترعش أبداً.
- انت مش بتعمل عمليات (فاتورة، شرا، مخزون، تحصيل): لو حد عايز يعمل حاجة قوله يقول مثلاً «اعمل
  فاتورة مبيعات» أو «ضيف للمخزون» والروبوت هيمشي معاه خطوة خطوة.
- أرقام المحل (المبيعات، الخزنة، الحسابات) للموظفين بس: الأداة بتقولك لو الشخص مسموحله ولا لأ.
- أي موضوع عام (عربيات، صيانة، كورة، أي حاجة) اتكلم فيه عادي بلطف ومعلومة مفيدة.
{now}{branch}
"""


def enabled() -> bool:
    from django.conf import settings

    from .audio import gemini_key

    return bool(getattr(settings, "ROBOT_AGENT", True)) and bool(gemini_key())


# ---------------------------------------------------------------------------
# The tools: what Gemini may read
# ---------------------------------------------------------------------------

_REPORTS = ["sales_today", "sales_yesterday", "sales_month", "spent_today", "low_stock", "treasury",
            "day_summary", "present", "absent"]

TOOLS = [{"functionDeclarations": [
    {"name": "find_parts",
     "description": "دوّر على قطع غيار في المخزون بالاسم أو رقم القطعة أو العربية. بيرجّع لحد ٥ قطع بسعر البيع "
                    "والكمية في الفرع ده والفروع التانية.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "اسم القطعة أو رقمها، مع العربية لو اتقالت (مثلاً: فلتر زيت F30)"}},
         "required": ["query"]}},
    {"name": "part_details",
     "description": "تفاصيل قطعة واحدة برقمها: الماركة، الحالة، الضمان، بتركب على إيه، مكانها في الرف، والبدايل "
                    "الموجودة.",
     "parameters": {"type": "object", "properties": {
         "part_number": {"type": "string", "description": "رقم القطعة زي ما رجع من find_parts"}},
         "required": ["part_number"]}},
    {"name": "shop_numbers",
     "description": "أرقام المحل للموظفين: مبيعات النهارده/امبارح/الشهر، مصاريف النهارده، النواقص، الخزنة، "
                    "ملخص اليوم، مين حاضر ومين غايب. بيرجّع الرد أو إن الشخص مش مسموحله.",
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": _REPORTS}}, "required": ["kind"]}},
    {"name": "account_balance",
     "description": "حساب عميل آجل أو مورد بالاسم (للموظفين).",
     "parameters": {"type": "object", "properties": {
         "who": {"type": "string", "enum": ["customer", "vendor"]},
         "name": {"type": "string"}}, "required": ["who", "name"]}},
    {"name": "shop_info",
     "description": "معلومات المحل اللي صاحبه كتبها: المواعيد، العنوان، التليفون، الدفع، الضمان، التوصيل، "
                    "المرتجعات، وأسامي الفروع.",
     "parameters": {"type": "object", "properties": {}}},
]}]


def _product(part_number: str):
    from inventory.models import Product

    code = (part_number or "").strip()
    return Product.objects.filter(part_number__iexact=code, is_active=True).first() if code else None


def _part_row(product, branch) -> dict:
    from . import assistant, services
    from .pricing import safe_product_payload

    data = safe_product_payload(product, branch=branch)
    return {"name": data["name"], "part_number": data["part_number"],
            "price": services.spoken_price(data.get("retail_price") or 0) if data.get("retail_price") else "مش متسجل",
            "car": (getattr(product, "car_model", "") or "").strip(), "stock_here": data.get("stock", 0),
            "other_branches": [{"branch": name, "stock": qty} for name, qty in assistant.branch_counts(product, branch)]}


def _find_parts(device, query: str) -> dict:
    from . import memory, services

    query = (query or "").strip()
    if not query:
        return {"parts": []}
    product = services.find_product(query)
    choices = []
    if product is None:
        product, choices = services.match_part_in_sentence(query, car=memory.car(device))
    found = ([product] if product is not None else []) + [c for c in choices if c != product]
    return {"parts": [_part_row(p, device.branch) for p in found[:PARTS_SAID]]}


def _part_details(device, part_number: str) -> dict:
    from . import assistant, services
    from .brain import talk

    product = _product(part_number)
    if product is None:
        return {"found": False}
    row = _part_row(product, device.branch)
    months = int(getattr(product, "warranty_months", 0) or 0)
    row.update({
        "brand": (getattr(product, "brand", "") or "").strip() or "مش متسجلة",
        "condition": talk.CONDITIONS.get(getattr(product, "condition", "") or "", "مش متسجلة"),
        "warranty": talk.months(months) if months > 0 else "مش متسجل",
        "fits": talk.models_of(getattr(product, "chassis_compatibility", None), getattr(product, "car_model", ""))[:8],
        "shelf": services.shelf_location(product, device.branch) or "مش متسجل",
        "alternatives_here": [{"name": a.name, "part_number": a.part_number,
                               "price": services.spoken_price(a.retail_price or 0)}
                              for a in assistant.alternatives_here(product, device.branch, limit=3)],
    })
    return row


def _shop_numbers(device, employee, kind: str) -> dict:
    from . import assistant

    if kind not in _REPORTS:
        return {"error": "kind"}
    reply, payload = assistant.staff_answer(kind, device, employee)
    return {"answer": reply, "allowed": payload.get("action") != "denied"}


def _account_balance(device, employee, who: str, name: str) -> dict:
    from . import assistant

    kind = "vendor_balance" if who == "vendor" else "customer_balance"
    said = f"حساب {'المورد' if who == 'vendor' else 'العميل'} {(name or '').strip()}"
    reply, payload = assistant.staff_answer(kind, device, employee, said)
    return {"answer": reply, "allowed": payload.get("action") != "denied"}


def _shop_info(device) -> dict:
    from inventory.models import Branch

    from . import replies

    return {"replies": [{"topic": r["topic"], "answer": r["answer"]} for r in replies.for_branch(device.branch)],
            "branches": list(Branch.objects.values_list("name", flat=True)[:20])}


def call(name: str, args: dict, device, employee) -> dict:
    """One tool, as Gemini asked for it — its result, or what went wrong (never an exception)."""
    try:
        if name == "find_parts":
            return _find_parts(device, str(args.get("query") or ""))
        if name == "part_details":
            return _part_details(device, str(args.get("part_number") or ""))
        if name == "shop_numbers":
            return _shop_numbers(device, employee, str(args.get("kind") or ""))
        if name == "account_balance":
            return _account_balance(device, employee, str(args.get("who") or ""), str(args.get("name") or ""))
        if name == "shop_info":
            return _shop_info(device)
    except Exception:                       # a tool that broke says so; the answer goes on
        logger.warning("robot agent: tool %s failed", name, exc_info=True)
        return {"error": "failed"}
    return {"error": "unknown tool"}


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

def _ask(contents: list, system: str, timeout: float) -> Optional[dict]:
    """Gemini's next turn (its content: text, or the tools it calls), or None."""
    import requests

    from . import audio

    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "tools": TOOLS,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    for model in audio._models_in_order()[:2]:
        try:
            resp = requests.post(audio._GEMINI_URL.format(model=model), params={"key": audio.gemini_key()},
                                 json=body, timeout=timeout)
        except requests.RequestException as exc:
            logger.warning("robot agent: %s didn't answer: %s", model, exc)
            continue
        if resp.status_code == 400 and "thinking" in resp.text.lower():
            body["generationConfig"].pop("thinkingConfig", None)
            resp = requests.post(audio._GEMINI_URL.format(model=model), params={"key": audio.gemini_key()},
                                 json=body, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("robot agent: %s answered %s: %s", model, resp.status_code, resp.text[:200])
            continue
        try:
            return resp.json()["candidates"][0]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    return None


def answer(device, employee, said: str, lang: str = "ar", *, history=(), facts: str = "") -> str:
    """What the robot says to `said`, from the system's own data — '' when Gemini can't (the
    caller falls back to services.ai_reply)."""
    from . import language, services
    from .pricing import redact
    from .services.ai import _AI_ENGLISH, _shop_and_time, invented_price

    said = (said or "").strip()
    if not said or not enabled():
        return ""
    filled = _shop_and_time()
    branch = getattr(device.branch, "name", "") or ""
    system = PROMPT.format(robot=(getattr(device, "wake_name", "") or "موس").strip(), shop=filled["shop"],
                           now=filled["now"].strip(), branch=f" وانت في فرع {branch}." if branch else "")
    if facts:
        system += "\nالقطعة اللي الزبون بيتكلم عنها دلوقتي:\n" + facts
    if lang == "en":
        system += _AI_ENGLISH
    contents = []
    for before, replied in list(history or [])[-4:]:
        contents += [{"role": "user", "parts": [{"text": str(before)[:200]}]},
                     {"role": "model", "parts": [{"text": str(replied)[:300]}]}]
    contents.append({"role": "user", "parts": [{"text": said[:500]}]})
    seen = [said, facts]                    # what a price said may come from
    started = time.monotonic()
    reply = ""
    for _step in range(STEPS):
        left = BUDGET_S - (time.monotonic() - started)
        if left < 1:
            break
        content = _ask(contents, system, min(TIMEOUT_S, left))
        if not content:
            break
        parts = content.get("parts") or []
        calls = [p["functionCall"] for p in parts if isinstance(p, dict) and p.get("functionCall")]
        if not calls:
            reply = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict) and not p.get("thought"))
            break
        contents.append({"role": "model", "parts": parts})
        results = []
        for fc in calls:
            got = call(str(fc.get("name") or ""), fc.get("args") or {}, device, employee)
            seen.append(json.dumps(got, ensure_ascii=False))
            answered = {"name": fc.get("name"), "response": got}
            if fc.get("id"):                # Gemini 3 pairs each answer with its call
                answered["id"] = fc["id"]
            results.append({"functionResponse": answered})
        contents.append({"role": "user", "parts": results})
    reply = " ".join(reply.replace("*", " ").split())
    if not reply:
        return ""
    if lang != "en" and language.detect(reply) == "en":
        return ""
    reply = redact(reply[:LONGEST])
    if invented_price(reply, "\n".join(seen)):
        logger.warning("robot agent: dropped an answer with a price no tool gave")
        return ""
    services.logger.info("robot agent: answered in %.1f s", time.monotonic() - started)
    return reply
