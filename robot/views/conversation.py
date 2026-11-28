"""💬 API الروبوت — فهم الكلام والرد عليه (_answer_voice) وكلام المحل والقطع."""

from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone

from .. import agent, assistant, entries, enrollment, memory, money, permissions, replies, restock, services, wakename, wizard, workshop
from ..brain import talk
from ..brain.knowledge import NOT_HERE, available
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch



# Said as the WHOLE utterance (e.g. «مش موجود»), never matched inside a
# sentence: "القطعة دي مش موجودة" near the robot must not skip the employee
# standing in front of the camera.
_ENROLL_SKIP_PHRASES = {"مش موجود", "مش موجوده", "هو مش موجود", "هي مش موجوده",
                        "مش هنا", "التالي", "اللي بعده", "اللي بعدو", "skip", "next"}
_ENROLL_CANCEL_PHRASES = {"وقف التسجيل", "الغي التسجيل", "stop enrollment"}


def _enrollment_control(text: str):
    """'skip' / 'cancel' when the utterance is exactly a control phrase."""
    t = wakename.normalize(text)
    if t in {wakename.normalize(p) for p in _ENROLL_SKIP_PHRASES}:
        return "skip"
    if t in {wakename.normalize(p) for p in _ENROLL_CANCEL_PHRASES}:
        return "cancel"
    return None


def _is_enrollment_control(transcript: str) -> bool:
    return _enrollment_control(transcript) is not None


def _handle_voice(transcript: str, device, employee=None, lang: str = "ar", caps=frozenset(), unsure=False,
                  via: str = "robot"):
    """Tiny bilingual intent router for the voice assistant. Replies are in
    Egyptian Arabic (`voice` translates them for an English speaker); only
    the free-form LLM answer is asked for in `lang` directly.

    «السلام عليكم، عندك طرمبة؟»: the greeting is answered first, and it's a
    new conversation (talk.opening) — the rest is answered as asked.
    `unsure`: the teacher wasn't sure it heard right — what nothing here
    makes sense of is asked again («مسمعتكش كويس»), not guessed at."""
    greeting, rest, _clips = talk.opening(transcript)
    if not greeting:
        return _answer_voice(transcript, device, employee, lang, caps, unsure, via=via)
    memory.forget(device)
    intent, reply, payload = _answer_voice(rest, device, employee, lang, caps, unsure, via=via)
    return intent, f"{greeting} {reply}", payload


def _understood(text: str, device) -> bool:
    """Whether the shop makes sense of a sentence — a part (or the few it could
    be), one of its replies, small talk, a command, the books, a follow-up on
    the part just named — for choosing among what a teacher may have heard
    (robot/teachers._likeliest). Reads only: nothing is answered or kept."""
    t = (text or "").strip()
    if not t:
        return False
    greeting, rest, _clips = talk.opening(t)
    if greeting:
        return True
    if (talk.small_talk(t) is not None or _TEACH_RE.match(t) or entries.is_answer(device, t)
            or assistant.media_command(t) is not None or assistant.staff_question(t) is not None
            or assistant.attendance_command(t) is not None
            or assistant.web_question(t) or talk.mobile_in(t) or re.search(r"\b[pbcu][0-9]{4}\b", t.lower())):
        return True
    if replies.answer(t, device.branch) is not None or memory.follow_up(t, device)[0] is not None:
        return True
    if entries.parse_expense(t) or entries.parse_purchase(t) or entries.parse_sale(t) or money.is_request(t):
        return True
    ans = services.inventory_answer(t, branch=device.branch)
    return bool(ans.get("found") or ans.get("choices"))


def _answer_voice(transcript: str, device, employee=None, lang: str = "ar", caps=frozenset(), unsure=False,
                  via: str = "robot"):
    """What's said, answered — by whoever's known to be there: the face the camera knows, or
    the one who said their PIN a moment ago (robot/wizard.py).

    🐛 [FIX]: من غير كاميرا، «سجل مصروف 200 بنزين» و«بعنا كام النهارده؟» و«شغّل قرآن» كانوا بيترفضوا
    («محتاج صلاحية أعلى» / «بص للكاميرا») حتى بعد ما الموظف قال رقمه السري لفاتورة قبلها بدقيقة —
    الرقم السري كان بيتسأل في الفواتير خطوة بخطوة بس. دلوقتي أي طلب للموظفين من حد مش معروف
    بيتسأل عن الرقم السري، وبعد ما يتقال الطلب نفسه بيتنفذ."""
    staff = employee or wizard.known_staff(device)
    if employee is not None:
        employee.robot_saw_face = True            # the camera knows them (attendance's face rule)
    intent, reply, payload = _answer_voice_as(transcript, device, staff, lang, caps, unsure, via)
    if payload.get("action") == "wizard_replay":
        # The PIN just said was right: what was asked before it, as that employee.
        staff = wizard.known_staff(device)
        intent, reply, payload = _answer_voice_as(payload["replay"], device, staff, lang, caps, False, via)
    if staff is not None and payload.get("action") in assistant.HEADS_UP_AFTER:
        try:                                      # once a day: what's running low, who owes for long
            more = assistant.heads_up(device, staff)
        except Exception:                         # a heads-up never costs them the answer itself
            import logging
            logging.getLogger("mouss_tec_core").exception("robot heads-up failed")
            more = ""
        if more:
            reply, payload = f"{reply} {more}", {**payload, "heads_up": True}
    if staff is None and payload.get("action") == "denied":
        held = wizard.hold(transcript, device, via=via)
        if held is not None:
            return ("command",) + held
    return intent, reply, payload


def _answer_voice_as(transcript: str, device, employee, lang: str, caps, unsure, via: str):
    low = transcript.lower()

    # --- Answer to «أسجلها على أنهي فرع؟» --------------------------------
    pending = entries.pending_for(device)
    if pending is not None:
        handled = entries.answer(pending, transcript)
        if handled is not None:
            return ("command",) + handled
        # Talk about a branch that matched none of them: ask again. Anything
        # else is a new question and is answered normally.
        if "فرع" in transcript:
            return ("command", "مش فاهم أنهي فرع. " + entries.branch_question(device),
                    {"action": "entry_ask_branch_again", "entry_id": pending.pk})

    # --- A sale / purchase / expense asked for step by step (robot/wizard.py)
    stepped = wizard.answer(transcript, device, employee, via=via)
    if stepped is not None:
        return ("command",) + stepped

    # --- Teaching: "اتعلم الطرمبة يعني 11517586925" --------------------
    # Checked first: during a stock-take the trailing part number would
    # otherwise be read as a count.
    taught = _TEACH_RE.match(transcript.strip())
    if taught:
        alias, target = taught.group(1).strip(), taught.group(2).strip()
        if not permissions.employee_can(employee, "teach"):
            return ("command", permissions.denial_message("teach"), {"action": "denied"})
        product = services.find_product(target)
        if product is None:
            return ("command", f"مش لاقي «{target}» في الأصناف — قول رقم القطعة.",
                    {"action": "teach_unresolved"})
        services.learn_from_confirmation(
            product=product, label=alias, employee=employee,
            details={"source": "voice"},
        )
        return ("command", f"تمام، اتعلمت إن «{alias}» يعني {product.name}.",
                {"action": "learned", "alias": alias, "product_id": product.id})

    # --- Answering a page: "جاي" / "حاضر" from the employee who was called --
    if employee is not None and _PAGE_ACK_RE.search(low):
        from ..models import RobotPageCall
        page = (RobotPageCall.objects
                .filter(device=device, target_employee=employee, status="announced",
                        announced_at__gte=timezone.now() - timedelta(minutes=15))
                .order_by("-announced_at").first())
        if page is not None:
            page.status = "acknowledged"
            page.save(update_fields=["status"])
            # Let whoever paged see the answer in their alerts feed.
            from ..models import RobotAlert
            RobotAlert.objects.create(
                device=device, kind="other",
                message=f"📢 {employee.name} ردّ على النداء: جاي.",
            )
            return ("command", f"تمام يا {employee.name}، هبلّغهم إنك جاي.",
                    {"action": "page_acknowledged", "page_id": page.id})

    # --- Staff face enrollment round -------------------------------------
    round_ = enrollment.active_session(device)
    if round_ is not None:
        control = _enrollment_control(transcript)
        if control == "skip":
            skipped = enrollment.skip_current(round_, note="اتقال مش موجود")
            return ("command", f"ماشي، هنتخطى {skipped['name'] if skipped else 'الموظف ده'}.",
                    {"action": "enroll_skip"})
        if control == "cancel":
            enrollment.cancel(round_)
            return ("command", "تمام، وقفت تسجيل البصمات.", {"action": "enroll_cancel"})
    if "بصمات" in low and any(w in low for w in ("سجل", "سجّل", "تسجيل")):
        if not permissions.employee_can(employee, "enroll_staff"):
            return ("command", permissions.denial_message("enroll_staff") +
                    " أو ابدأه من لوحة التحكم.", {"action": "denied"})
        try:
            enrollment.start(device, user=getattr(employee, "user", None))
        except enrollment.EnrollmentUnavailable as exc:
            return ("command", str(exc), {"action": "enroll_unavailable"})
        return ("command", "تمام، هبدأ أنادي الموظفين واحد واحد.", {"action": "enroll_start"})

    # --- «قولي رقم موبايلك»: the number for a part that's out ------------
    # Before anything that reads digits (a count, a part number).
    heard = restock.hear(transcript, device)
    if heard is not None:
        return ("command",) + heard

    # --- Sound in the shop: «شغّل قرآن», «وقّف الصوت» (role-gated) ---------
    media = assistant.media_command(transcript)
    if media is not None:
        return ("command",) + assistant.media_reply(media, employee, caps)

    # --- «سجل حضوري» / «سجل انصرافي»: one's own, by HR's service ----------------
    clock = assistant.attendance_command(transcript)
    if clock is not None:
        return ("command",) + assistant.attendance_reply(clock, employee)

    # --- The staff's questions: «بعنا كام النهارده؟», «إيه اللي قرّب يخلص؟» --
    question = assistant.staff_question(transcript)
    if question is not None:
        return ("command",) + assistant.staff_answer(question, device, employee, transcript)

    # --- «دوّر على النت على …»: what the ERP doesn't know (role-gated) ----
    if assistant.web_question(transcript):
        return ("command",) + assistant.web_reply(transcript, employee, device, lang)

    # --- "امسح القطعة دي": ask the camera to look -------------------------
    if any(w in low for w in ("امسح القطعة", "شوف القطعة", "اعرف القطعة", "scan this")):
        views.RobotCommand.objects.create(device=device, kind="scan",
                                    payload={"purpose": "lookup", "lang": lang})
        return ("command", "ثانية واحدة، قرّب القطعة من الكاميرا.", {"action": "scan_requested"})

    # --- Expense / purchase: held until a branch is named ---------------
    # Before the stock-take block: «مصروف بنزين 200» would otherwise be read
    # as a count line during an open count.
    started = (wizard.start(transcript, device, employee, via=via)
               or entries.voice_request(transcript, device, employee))
    if started is not None:
        return ("command",) + started

    # --- Conversational stock-take (جرد) ---------------------------------
    open_session = services.get_open_stock_take(device)

    # Finish an in-progress count.
    if open_session and (low.startswith("خلص") or low.startswith("انهاء") or
                         low.startswith("إنهاء") or re.search(r"\b(finish|done)\b", low)):
        report = services.complete_stock_take(open_session)
        n, v = report["counted_items"], len(report["variances"])
        return ("command",
                f"خلّصت الجرد: عدّينا {n} صنف، فيه {v} فرق. تقدر تعتمد التسويات من اللوحة.",
                {"action": "stock_take_done", "report": report})

    # Start a new count — only a stock/manager role may (RBAC on voice too).
    if (low.startswith("اجرد") or low.startswith("جرد") or
            "stock take" in low or low.startswith("count")):
        if not permissions.employee_can(employee, "stock_take"):
            return ("command", permissions.denial_message("stock_take"),
                    {"action": "denied"})
        services.start_stock_take(device, device.branch,
                                  instruction=transcript, employee=employee)
        return ("command",
                "تمام، ابدأ عدّ القطع. قول اسم كل قطعة والعدد، ولما تخلص قول: خلص الجرد.",
                {"action": "start_stock_take"})

    # While counting, each turn like "كنترول ٣" adds a line.
    if open_session:
        query, qty = services.parse_count_utterance(transcript)
        if query is not None and qty is not None:
            line, product = services.add_stock_take_count(
                open_session, query=query, counted_qty=qty)
            if product is None:
                return ("command",
                        f"مش لاقي «{query}» في المخزون — قول الاسم أو الرقم تاني.",
                        {"action": "count_unresolved"})
            return ("command",
                    f"سجّلت {product.name}: {qty}. القطعة اللي بعدها؟",
                    {"action": "count_added",
                     "expected": line.expected_qty, "counted": line.counted_qty})

    # --- Fault code question, e.g. "P0301" or "كود p0420" ----------------
    m = re.search(r"\b([pbcu][0-9]{4})\b", low)
    if m:
        # The fault explained by the repair coach, with what the diagnostics catalog and bmw_ecu know
        # of it (robot/workshop.fault_facts) — the bare definition only when the AI can't.
        coached = _talk_shop(transcript, device, lang) if not unsure else ""
        if coached:
            return "diagnostic", coached, {"ai": True, "workshop": True, "fault": m.group(1).upper()}
        res = services.lookup_fault_code(m.group(1).upper())
        if res.get("found"):
            reply = f"{res['code']}: {res['description']}."
            if res.get("likely_causes"):
                reply += " الأسباب المحتملة: " + "، ".join(res["likely_causes"][:3]) + "."
            return "diagnostic", reply, {"fault": res}
        return "diagnostic", f"ملقتش تعريف للكود {m.group(1).upper()}.", {}

    # --- The customer's car: «عندكم قطع بي ام؟»، «عربيتي F30» ------------------
    # Kept for what's asked next (robot/memory.py): «عندك طرمبة مية؟» after it
    # is the pump for that car.
    car = memory.hear_car(device, transcript)

    # --- A follow-up on what was just said --------------------------------
    # «التانية» / «E90» answering «تقصد أنهي؟», «وبكام؟», «عليه ضمان؟»,
    # «مكانها فين؟», «فيه أرخص؟» — about the part the conversation is on
    # (robot/memory.py). A sentence that names another part is a new question.
    product, kind = memory.follow_up(transcript, device)
    if product is not None:
        if kind in ("pick", "price"):
            ans = views.safe_product_payload(product, branch=device.branch)
            ans["found"] = True
            return _part_reply(ans, device, product)
        reply, payload = memory.answer(kind, transcript, product, device)
        return "inventory_query", reply, payload

    # --- «الردود الجاهزة»: the hours, where the shop is, how to pay … ------
    # Written by the owner (robot/replies.py); before the parts, which would
    # look for a part called «مواعيد», and the AI, which would make one up.
    said = replies.answer(transcript, device.branch)
    if said is not None:
        return "shop_reply", said["answer"], {"reply_topic": said["topic"]}

    # --- «السلام عليكم»، «شكراً»، «إنت مين؟»، «قول تاني» (talk.small_talk) ------
    # After the shop's own replies (the owner may have taught «ازيك» its
    # answer), before the parts, which found nothing in «شكراً» and said so,
    # and the AI; the same words with the net down. A greeting or a goodbye
    # starts over.
    chat = talk.small_talk(transcript)
    if chat is not None:
        reply, _clips, over = talk.small_talk_reply(chat, (device.wake_name or "موس").strip(),
                                                    memory.last_reply(device), now=timezone.localtime())
        if over:
            memory.forget(device)
        elif chat["kinds"] & {"no", "thanks"}:
            memory.remember(device, restock_for=None)     # «لا شكراً» to «أبلغك أول ما ييجي؟»
        return "chat", reply, {"chat": sorted(chat["kinds"] | ({"greet"} if chat["greet"] else set()))}

    # --- Otherwise: inventory/stock question -----------------------------
    if not transcript.strip():
        return "unknown", "مسمعتكش كويس، ممكن تعيد؟", {}
    # Talking shop — how a thing is done, wiring, upkeep: the repair coach, before the stock
    # (robot/workshop.py). «ازاي أغير فلتر الزيت؟» isn't «أيوه، فلتر زيت موجود».
    no_part = not services.part_keys(transcript) and not car
    if not unsure and (workshop.asks_how(transcript) or (no_part and workshop.is_technical(transcript))):
        coached = _talk_shop(transcript, device, lang)
        if coached:
            return "unknown", coached, {"ai": True, "workshop": True}
        # 🐛 [FIX]: from the shop's log — Gemini busy, «العربية بتسخن، مشكلة إيه؟» got «مفهمتش قصدك».
        return "unknown", workshop.without_ai(transcript, car), {"workshop": True, "unresolved": True}
    # 🐛 [FIX]: «عندك» لوحدها (Vosk ضيّع باقي الجملة) راحت للذكاء الاصطناعي اللي عرّف نفسه وسأل
    # عن الصيانة. مفيش كلمة فيها اسم قطعة: الروبوت يسأل هو — إلا لو اتقال كلام كامل: دردشة عامة
    # («إيه أحسن زيت للصيف؟»، «الجو حر النهارده») ليها رد مفيد، مش «محتاج قطعة إيه؟».
    if no_part:
        if len(transcript.split()) >= 3 and not unsure:
            general = _discuss(transcript, device, lang)
            if general:
                return "unknown", general, {"ai": True, "discussion": True}
        return "chat", talk.ASK_PART, {"chat": ["ask_part"]}
    # «عربية إي نينتي» بعد «اسم العربية إيه؟»: العربية هي الجواب — للسؤال اللي قبلها لو كان عن
    # قطعة («عندك فلتر زيت؟»)، وإلا الروبوت يسأل محتاج إيه ليها.
    # «موتور إن توينتي» (only_car: the engine alone is the car's too) — with or without a car kept.
    if talk.only_car(transcript):
        turns = memory.recall(device).get("turns") or []
        before = str(turns[-1][0]) if turns else ""
        if before and services.part_keys(before):
            transcript = before
        else:
            said = (" ".join(car.get("models") or []) or car.get("make", "")
                    or " ".join(w.upper() for w in talk.part_words(transcript) if talk._ENGINE.match(w)))
            return "chat", f"تمام، {said}. محتاج قطعة إيه ليها؟", {"car": car}
    ans = services.inventory_answer(transcript, branch=device.branch, car=car)
    choice_parts = ans.pop("choice_parts", None) or []
    if not ans.get("found") and ans.get("choices"):
        # Several parts fit (or one, weakly): ask rather than say a price for
        # the wrong one. The answer needs no name (the 20 s follow-up window),
        # and «التانية» or «E90» is enough of one (robot/memory.py).
        memory.remember_choices(device, choice_parts)
        names = ans["choices"]
        if len(names) == 1:
            reply = f"تقصد {names[0]}؟ قول اسمها كامل أو رقمها."
        else:
            reply = "عندي أكتر من صنف: " + "، ".join(names) + ". تقصد أنهي؟"
        return "inventory_query", reply, {"choices": names}
    if not ans.get("found") and unsure:
        # 🐛 [FIX]: the teacher wasn't sure, and nothing here made sense of what it heard: the AI
        # answered a sentence nobody said. Asked again — sooner, too (no AI waited on).
        return "unknown", talk.HEARD_BADLY, {"unresolved": True, "unsure": True}
    if not ans.get("found"):
        # Flagged so the dashboard can list what the robot couldn't answer and
        # staff can teach it ("اتعلم … يعني …") — its gaps become lessons.
        # A general question still gets a helpful spoken answer from the LLM,
        # which sees what was just said and the part it was about.
        # A manager's question the ERP can't answer is looked up on the
        # internet (said so, with the source); anyone else gets the LLM.
        if assistant.may_look_up(employee, transcript):
            found, payload = assistant.web_reply(transcript, employee, device, lang)
            if payload.get("web"):
                return "unknown", found, payload
        facts, turns = memory.thinking_context(device)
        robot_name = (device.wake_name or "موس").strip()
        if workshop.is_technical(transcript):
            coached = _talk_shop(transcript, device, lang)
            if coached:
                return "unknown", coached, {"unresolved": True, "ai": True, "workshop": True}
            return "unknown", workshop.without_ai(transcript, car), {"unresolved": True, "workshop": True}
        # «🧠 المخ بأدوات» (robot/agent.py): Gemini reads the system itself — the parts, the stock in
        # each branch, the shop's replies, the staff's numbers for those who may; the general AI after.
        general = (agent.answer(device, employee, transcript, lang, history=turns, facts=facts)
                   or services.ai_reply(transcript, lang, facts=facts, history=turns,
                                        branch=getattr(device.branch, "name", "") or "",
                                        robot=robot_name))
        if general:
            return "unknown", general, {"unresolved": True, "ai": True}
        # 🐛 [FIX]: الذكاء الاصطناعي لما مردّش، كل حاجة كانت بتترد «ملقتش القطعة دي في المخزون» —
        # حتى «شكرا ليك يا موسى» و«الله ينور عليك». ده بس لو الكلام كان عن قطعة.
        if services.names_a_part(transcript):
            return ("inventory_query", "ملقتش القطعة دي في المخزون. ممكن تقولي رقمها؟",
                    {"unresolved": True})
        return "unknown", talk.NOT_UNDERSTOOD, {"unresolved": True}
    from inventory.models import Product
    return _part_reply(ans, device, Product.objects.filter(pk=ans.get("id")).first())


def _talk_shop(transcript: str, device, lang: str) -> str:
    """Wiring, a part's place, BMW/MINI upkeep: the shop's repair coach (Repair Atlas's «أسطى
    Mouss Tec», robot/workshop.py) answers it, said aloud — its verified knowledge first. '' when
    the AI is down."""
    facts, turns = memory.thinking_context(device)
    robot_name = (device.wake_name or "موس").strip()
    coached = services.ai_reply(
        transcript, lang, facts=facts, history=turns, branch=getattr(device.branch, "name", "") or "",
        robot=robot_name, longest=workshop.LONGEST,
        system=workshop.system_prompt(transcript, memory.car(device), robot=robot_name, shop=services.shop_name()))
    return workshop.spoken(coached) if coached else ""


def _discuss(transcript: str, device, lang: str) -> str:
    """A general subject talked through (robot/workshop.discussion_prompt): the conversation so far
    in mind, a few useful sentences, a question back. '' when the AI is down."""
    facts, turns = memory.thinking_context(device)
    robot_name = (device.wake_name or "موس").strip()
    said = services.ai_reply(
        transcript, lang, facts=facts, history=turns, branch=getattr(device.branch, "name", "") or "",
        robot=robot_name, longest=workshop.DISCUSS_LONGEST,
        system=workshop.discussion_prompt(robot=robot_name, shop=services.shop_name()))
    return workshop.spoken(said, longest=workshop.DISCUSS_LONGEST) if said else ""


def _answer_meant(meant, device, lang: str, via: str):
    """What «🧠 مخ الفهم» understood decides who answers, when it's talk rather than a task: a
    workshop question (wiring, a fault code, upkeep) goes to the repair coach with the system's
    diagnostics; a general subject is discussed. (intent, reply, payload), or None for the rules —
    a task, a part, the car, small talk, or a step that waits on its own answer."""
    if lang != "ar" or meant.intent not in ("workshop", "chat") or wizard.context(device, via):
        return None
    if entries.pending_for(device) is not None:
        return None
    said = meant.command or meant.meant
    if meant.intent == "workshop":
        coached = _talk_shop(said, device, lang)
        if coached:
            return "unknown", coached, {"ai": True, "workshop": True}
        return "unknown", workshop.without_ai(said, memory.car(device)), {"workshop": True, "unresolved": True}
    # «ازيك»، «شكرا»، the shop's hours: their own answers, at once — the rest is a discussion.
    if talk.small_talk(said) is not None or replies.answer(said, device.branch) is not None:
        return None
    ans = services.inventory_answer(said, branch=device.branch)
    if ans.get("found") or ans.get("choices"):
        return None                               # a part of the shop's named in it: the shop's answer
    discussed = _discuss(said, device, lang)
    return ("unknown", discussed, {"ai": True, "discussion": True}) if discussed else None


def _part_reply(ans: dict, device, product):
    """«أيوه، X موجود، عندي منه 3 حتت، بسعر …» — or not here, and what instead.
    `ans`: the part's retail-only payload. The conversation is now on it."""
    if product is not None:
        memory.remember_part(device, product.pk)
        memory.took_part(device, product)          # «والفلتر بتاعها؟»: for the same car
    price = ans.get("retail_price") or 0
    stock = ans.get("stock", 0)
    # 🐛 [FIX]: «بسعر {price:.0f}» كان بيقول 451 لسعر 450.50، و«بسعر 0 جنيه» لصنف
    # سعره مش متسجل، والصنف الخلصان مكانش بيتقال سعره خالص.
    said_price = (f"بسعر {services.spoken_price(price)}" if price > 0
                  else "بس سعره مش متسجل، اسأل حد من الموظفين")
    if stock > 0:
        reply = f"أيوه، {ans['name']} {available(stock, upto=None)}، {said_price}."
        # Say where it is, and turn the head toward that shelf when mapped.
        shelf = services.shelf_location(product, device.branch) if product else ""
        if shelf:
            reply += f" موجود في الرف {shelf}."
            ans["shelf_location"] = shelf
            services.point_to_shelf(device, shelf)
    else:
        reply = f"{ans['name']} {NOT_HERE}"
        reply += (f"، وسعره {services.spoken_price(price)}." if price > 0 else ".")
        # Someone asked for it and we have none: that is demand, so let the
        # Procurement Agent know (idempotent per part/branch).
        if product is not None:
            # Not the end of the sale: the same part under another number on
            # the shelf here, or the same part in another branch. An extra —
            # never worth losing the answer itself over.
            try:
                tail, extra = assistant.out_of_stock_tail(product, device.branch)
            except Exception:
                services.logger.warning("robot voice: no alternative for %s", getattr(product, "pk", "?"),
                                        exc_info=True)
                tail, extra = "", {}
            reply += tail
            ans.update(extra)
            if not tail:
                # Nothing to offer instead: the sale can still wait for it.
                reply += " " + restock.offer(device, product)
                ans["restock_offer"] = True
            services.maybe_raise_procurement_signal(
                device=device, product=product, branch=device.branch,
            )
    return "inventory_query", reply, {"product": ans}


# A paged employee answering the robot's call.
_PAGE_ACK_RE = re.compile(r"(^|\s)(جاي|جايلك|جايين|حاضر|coming|on my way)(\s|$)")

# "اتعلم <كلمة> يعني <رقم/اسم القطعة>" / "learn <word> means <part>".
_TEACH_RE = re.compile(
    r"^(?:اتعلم|اتعلّم|تعلم|learn)\s+(.+?)\s+(?:يعني|=|means|is)\s+(.+)$",
    re.IGNORECASE,
)
