"""🎙️ API الروبوت — الصوت: استقبال الكلام، التوقيت، الرد المسموع، الأوفلاين، أوامر تسجيل الوشوش."""

from __future__ import annotations

import time
from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from .. import audio as audio_svc
from .. import assistant, entries, enrollment, language, memory, services, teachers, understand, wakename, wizard
from ..brain import talk
from robot import views  # 🔌 seams: بتتنادى عن طريق الحزمة عشان الاختبارات تقدر تعمل لها patch
from ._common import _robot_endpoint
from .conversation import _answer_meant, _is_enrollment_control, _understood


SAID_AGO_MAX_MS = 60_000


def _said_at(request):
    """When the sentence was said. 🐛 [FIX]: «مخ موس» on a slow computer hears
    a sentence 15 s after it was said: «يا موس» → «أيوه، تحت أمرك» →
    «عندك فلتر زيت؟» got here after the 20 s follow-up window had closed,
    and the question went unanswered. The robot says how long ago it was
    said (`said_ago_ms`, a minute at most), and the window is checked then —
    and before the server's own speech-to-text time, too."""
    try:
        ago = int(request.data.get("said_ago_ms") or 0)
    except (TypeError, ValueError):
        ago = 0
    return timezone.now() - timedelta(milliseconds=min(max(ago, 0), SAID_AGO_MAX_MS))


@_robot_endpoint
def voice(request):
    """Hands-free assistant + POS voice answers.

    Body: `transcript` (STT text), optional `face_embedding`. Classifies intent,
    queries the ERP (stock/price retail-only, or a fault code), returns
    `reply_text` for the amp to speak.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    timing = {"t0": time.monotonic()}
    audio = request.FILES.get("audio")
    body, listen_after = _voice_turn(request, device, audio.read() if audio is not None else None, timing)
    response = _voice_reply(request, timing, body)
    if listen_after:
        # 🐛 [FIX]: الـ ٢٠ ثانية كانت بتبتدي من السؤال. رد طويل جه بعد ٧ ثواني واتقال في ٩،
        # فـ«الزيت» اللي اتقالت بعده لقت الشباك قفل والروبوت قال «مش ليا». دلوقتي من بعد ما يخلّص رده.
        wakename.keep_listening(device, speaking=body.get("reply") or "")
    return response


def _voice_turn(request, device, recording, timing: dict, streamed=None):
    """One sentence said to the robot, heard and answered: (the /voice/ body,
    whether the follow-up window runs on once the reply is said).

    `request` is anything with `.data` — the HTTP request, or a sentence that
    came over the live socket (robot/consumers.py); `recording` the WAV, when
    one was sent; `streamed`: Azure already hearing it as it was said
    (teachers.AzureStream), so its words are ready now.
    """
    said_at = _said_at(request)

    # Accept either a ready transcript OR raw audio (transcribed server-side).
    transcript = (request.data.get("transcript") or "").strip()
    if not transcript and recording is not None:
        try:
            # Azure first (the key the shop pays for, within its minutes), Gemini after —
            # with the names of what the branch sells, written as the shop writes them.
            transcript = (teachers.hear(device, recording, streamed=streamed)
                          or audio_svc.transcribe(recording, name=getattr(device, "wake_name", "") or "موس",
                                                  strict=True, hints=teachers.hints_for(device)) or "")
        except audio_svc.SttUnavailable as exc:
            # 🐛 [FIX]: speech-to-text being down (no key, Gemini's quota or an
            # outage) came back as «heard nothing», word for word what a cough
            # does: the shop saw a deaf robot and no reason. It says why now.
            timing["hear"] = time.monotonic()
            return {"intent": "ignored", "addressed": False, "reason": "stt_down",
                    "detail": str(exc)[:200], "reply": ""}, False
    timing["hear"] = time.monotonic()

    ptt = str(request.data.get("ptt", "")).lower() in ("1", "true", "yes")
    # Nothing understood: noise, a mumble, or speech-to-text is down. Said so
    # (`reason`), so the robot's log tells this apart from "not my name";
    # with the button held the person clearly spoke to it, so it asks again.
    if not transcript:
        return {"intent": "ignored", "addressed": ptt, "reason": "no_transcript",
                "reply": "معلش مسمعتكش كويس، قول تاني." if ptt else ""}, False

    # Only speech addressed to the robot by name gets an answer. People
    # talking to each other nearby are ignored — not answered, not stored.
    # Only the enrollment round's own control words ("مش موجود", "التالي")
    # skip the name — any other chatter while names are being called is still
    # ignored. A stock count needs no exception: every accepted count extends
    # the 20 s follow-up window, so a steady counter never repeats the name.
    # Same for a branch name answering «أسجلها على أنهي فرع؟» («المعادي»).
    # «مخ موس» says a conversation's on (`talking`), by its own clock: from when it was done
    # speaking. 🐛 [FIX]: an answer that came 40 s late left the server's window shut before the
    # robot had said it — «شكرا» right after was «مش ليا».
    talking = str(request.data.get("talking", "")).lower() in ("1", "true", "yes")
    # Said on «📱 كلّم موس» or to the robot in the shop — one robot, but an invoice begun on one
    # is answered on that one only (robot/wizard.py): a customer at the robot never answers it.
    via = "phone" if request.data.get("phone") else "robot"
    ongoing = ((_is_enrollment_control(transcript)
                and enrollment.active_session(device) is not None)
               or entries.is_answer(device, transcript) or talking or wizard.active(device, via))
    for_robot, text, name_only = wakename.gate(
        device, transcript, push_to_talk=ptt, ongoing_flow=ongoing, said_at=said_at)
    if not for_robot:
        # What was heard goes back to the robot's own log only (stored nowhere):
        # «يا موز» told apart from chatter when it «doesn't answer».
        return {"intent": "ignored", "reply": "", "addressed": False, "transcript": transcript[:160]}, False
    wakename.keep_listening(device)
    # «🎓 تعلّم السمع»: «مخ موس» wasn't sure it heard right (`check=1`, with
    # the recording) — a teacher hears it, and its words are the question.
    heard_by, unsure = "", False
    ears, teacher_said = text, ""                 # each hearing, for robot/understand.py
    both = None                                   # heard and understood in one Gemini call
    # Said with the robot's name, or only inside the follow-up window (robot/wakename.py)?
    named = wakename.strip_name(device, transcript)[0]
    # The robot just asked for a PIN (robot/wizard.py): what's said now is never kept.
    secret = wizard.wants_secret(device)
    if (recording is not None and not name_only and request.data.get("transcript")
            and str(request.data.get("check", "")).lower() in ("1", "true", "yes")):
        try:
            confidence = float(request.data.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        # What the robot's ears wrote, when the words sent are what a lesson made of them
        # (a learned sentence heard again): the lesson is checked against the ears, not itself.
        ears = str(request.data.get("heard") or "").strip()
        if ears:
            ears_named, rest = wakename.strip_name(device, ears)
            ears = rest if ears_named else ears
        ears = ears or text
        asked_at = time.monotonic()
        if not secret and teachers.configured()[:1] == ["gemini"]:
            both = understand.hear_and_mean(device, recording, ears, via=via)
        got = None if both is not None else teachers.check(
            device, recording, ears, confidence, understood=lambda said: _understood(said, device),
            streamed=streamed, keep=not secret)
        timing["teach_ms"] = int((time.monotonic() - asked_at) * 1000)
        if both is not None:
            text, heard_by, teacher_said = both[0], "gemini", both[0]
        if got is not None:
            text, heard_by, teacher_said = got.text, got.teacher, got.text
            # 🐛 [FIX]: not for a sentence the robot answered itself and is having heard again (its
            # answer stood: «مسمعتكش» after it was a «sorry» for nothing).
            unsure = (isinstance(got.confidence, (int, float)) and got.confidence < teachers.UNSURE_BELOW
                      and str(request.data.get("known") or "") not in ("learned", "trusted"))
    # Answer in the language they spoke: Egyptian Arabic, or English.
    lang = language.detect(text or transcript)
    # 🐛 [FIX]: an ar-EG teacher is always unsure of English: English questions were asked again.
    unsure = unsure and lang == "ar"
    if name_only:
        # «يا موس» on its own after a pause: likely someone new (robot/memory.py).
        memory.fresh_start(device)
        timing["think"] = time.monotonic()
        return {"intent": "wake", "addressed": True, "transcript": transcript, "lang": lang,
                "reply": "Yes? How can I help?" if lang == "en" else "أهلاً بحضرتك، أقدر أساعدك إزاي؟"}, False

    employee = views._voice_employee(request, device)
    # What the caller can do beyond speaking: «مخ موس» on a computer sends
    # `caps=media` (it has speakers for the Quran and music); the ESP32 doesn't.
    caps = frozenset(c.strip() for c in str(request.data.get("caps", "")).lower().split(",") if c.strip())
    # «🧠 مخ الفهم» (robot/understand.py): what was meant — from every hearing, what was just said
    # and the step waited on — as a sentence the rules read for sure. The rules still do it all.
    meant, said = None, text
    known = str(request.data.get("known") or "")
    if both is not None:
        meant = both[1]
    elif lang == "ar" and not secret and known not in ("learned", "trusted"):
        meant = understand.meaning(device, text, ears=ears if ears != text else "", teacher=teacher_said, via=via)
    if meant is not None:
        timing["und_ms"] = meant.ms
    unanswered = meant is not None and not meant.intent
    if unanswered:
        meant = None                              # Gemini didn't answer: the rules alone, said so below
    # 🐛 [FIX]: بعد «اسم العميل إيه؟» الروبوت بيسمع ٢٠ ثانية من غير اسمه، فكان بيلقط كلام الناس في المحل
    # («شلت أنطوان من الشغلانة…») ويرد «معلش مسمعتكش». كلام مش للروبوت — ومن غير اسمه — بيتساب في هدوء.
    if not named and not ptt and (meant is not None and meant.intent in ("chatter", "unclear")
                      or meant is None and lang == "en" and not ears.strip()):
        wizard.keep(device)                       # its question still waits
        return {"intent": "ignored", "reply": "", "addressed": False, "reason": "chatter",
                "transcript": transcript[:160]}, False
    if meant is not None and meant.intent == "chatter":
        meant.intent = "unclear"
    if meant is not None and meant.intent == "unclear":
        wizard.keep(device)                       # its question still waits
        intent, reply, payload = "unknown", talk.HEARD_BADLY, {"unresolved": True}
    else:
        answered = _answer_meant(meant, device, lang, via) if meant is not None else None
        if meant is not None:
            text, unsure = meant.command, False
        if answered is not None:
            intent, reply, payload = answered
        else:
            intent, reply, payload = views._handle_voice(text, device, employee, lang=lang, caps=caps, unsure=unsure,
                                                   via=via)
    if meant is not None:
        payload = {**payload, "understood": {"intent": meant.intent, "command": meant.command,
                                             "by": meant.source, "ms": meant.ms}}
        # The robot's log: «[MEANT] «ضيف منتج جديد للمخزون» (stock · gemini 640 ms)».
        payload["meant"] = (f"«{meant.command}» ({meant.intent} · "
                            + (f"{meant.model} {meant.ms} ms" if meant.source == "gemini" else "learned") + ")")
    elif unanswered:
        payload = {**payload, "meant": f"— no AI answered in {timing['und_ms']} ms: the rules alone"}
    # 🐛 [FIX]: «لحظة واحدة هسأل لك حد من زمايلي» — and nobody was asked. Now the staff are called,
    # aloud («يا موظف!» three times), and the dashboard's alerts say what the customer wanted.
    # (Not from a phone — robot/phone.py: the one asking is the staff, away from the shop.)
    if (lang == "ar" and intent != "command" and not request.data.get("phone")
            and services.hands_to_staff(reply) and services.call_staff(device, text)):
        reply = f"{reply} {services.STAFF_CALL}"
        payload = {**payload, "staff_called": True}
    reply = language.reply_in(lang, reply)
    timing["think"] = time.monotonic()
    # What was just said, for the next sentence (robot/memory.py).
    memory.note_turn(device, "" if secret else (meant.meant if meant is not None and meant.meant else said), reply)

    # «مخ موس» answered it without waiting for a teacher — a learned sentence, or one of the
    # words its ears write right (robot/teachers.py): counted on «🎓 تعلّم السمع».
    views.RobotVoiceInteraction.objects.create(
        device=device, transcript="•••• (الرقم السري)" if secret else transcript, intent=intent,
        reply_text=reply, employee=employee,
        payload={**payload, "heard_as": known} if known in ("learned", "trusted") else payload,
    )
    # 🐛 [FIX]: الـ payload كان بييجي بعد «reply» فبيكتب فوقه: الرد الجاهز رجّع {"reply": "hours"}
    # والروبوت قال «hours» و«address» بدل المواعيد والعنوان. دلوقتي الـ payload الأول، والرد آخر كلمة.
    return {**payload, "intent": intent, "reply": reply, "transcript": "••••" if secret else transcript,
            "addressed": True, "lang": lang,
            **({"heard_by": heard_by, "taught": teacher_said} if heard_by and not secret else {})}, True


def _voice_reply(request, timing: dict, body: dict):
    """The /voice/ answer: the JSON body — or, when the robot asked for it
    (`reply_audio=ulaw`), the spoken reply itself.

    Asking for the words and then fetching their voice from /speak/ was two
    trips over the shop's internet before the robot said anything. With
    `reply_audio` the WAV comes back in this same response; what the robot
    logs travels in headers (percent-encoded UTF-8), and `X-Robot-Timing`
    says how long the server took to hear, think and voice it, in ms — so
    the robot can tell its owner whether a slow answer was the server or the
    internet. JSON whenever there's nothing to say, or no voice right now
    (the robot then falls back to /speak/).
    """
    from urllib.parse import quote

    wav, voices = _voice_audio(request.data, timing, body)
    if not wav:
        return Response(body)
    from django.http import HttpResponse
    resp = HttpResponse(wav, content_type="audio/wav")
    resp["X-Robot-Addressed"] = "1" if body.get("addressed") else "0"
    resp["X-Robot-Intent"] = quote(str(body.get("intent", ""))[:40], safe="")
    resp["X-Robot-Transcript"] = quote(str(body.get("transcript", ""))[:160], safe="")
    resp["X-Robot-Reply"] = quote(body["reply"][:160], safe="")
    resp["X-Robot-Timing"] = _timing_line(timing, voices)
    if body.get("meant"):                          # «🧠»: what was understood
        resp["X-Robot-Meant"] = quote(str(body["meant"])[:200], safe="")
    if body.get("heard_by"):                       # «🎓»: who heard it, and what
        resp["X-Robot-Heard-By"] = body["heard_by"]
        resp["X-Robot-Taught"] = quote(str(body.get("taught", ""))[:160], safe="")
    # A media command («شغّل قرآن») travels beside the spoken «حاضر».
    media = assistant.media_header(body)
    if media:
        resp["X-Robot-Media"] = media
    return resp


def _voice_audio(data, timing: dict, body: dict):
    """(the reply spoken — a WAV in the codec the robot asked for with
    `reply_audio` — and the voices that said it), or (None, []) when there's
    nothing to say, nothing was asked for, or no voice right now."""
    codec = str(data.get("reply_audio", "")).lower()
    reply = body.get("reply") or ""
    if codec not in (audio_svc.ULAW, audio_svc.PCM) or not reply:
        return None, []
    voices = []
    wav = audio_svc.synthesize_wav(reply, codec=codec, providers=voices)
    timing["voice"] = time.monotonic()
    return wav or None, voices


def _timing_line(timing: dict, voices) -> str:
    """How long the server took to hear, think and voice it, in ms
    («hear=..;think=..;voice=..;tts=edge»)."""
    marks, last, spent = ("hear", "think", "voice"), timing["t0"], []
    for mark in marks:
        if mark in timing:
            spent.append(f"{mark}={int((timing[mark] - last) * 1000)}")
            last = timing[mark]
    # Which voice said it (edge = the Egyptian one; gtts = the stand-in when
    # Edge is down; cache = said before), so «مش مصري» can be told apart.
    if voices:
        spent.append("tts=" + "+".join(voices))
    # Inside «think»: the teacher hearing it again, «🧠 مخ الفهم» understanding it.
    spent += [f"{name}={timing[key]}" for name, key in (("teach", "teach_ms"), ("und", "und_ms")) if key in timing]
    return ";".join(spent)


def _recorded_at(value):
    """A device's Unix timestamp as an aware datetime, or None when it's
    missing or implausible (the robot had no clock yet: it sends 0)."""
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    from datetime import datetime, timezone as dt_timezone
    when = datetime.fromtimestamp(ts, tz=dt_timezone.utc) if ts > 1_600_000_000 else None
    if when is None or when > timezone.now() + timedelta(minutes=5):
        return None
    return when


@_robot_endpoint
def voice_offline(request):
    """A voice clip the robot recorded on its SD card while it had no internet,
    sent when the connection is back.

    Body (multipart): `audio` (WAV), `client_uid`, `recorded_at` (Unix seconds,
    0 when the robot had no clock), `ptt`. Idempotent by `client_uid`, so a
    clip resent after a lost reply is applied once. Speech for the robot (its
    name said, or the button held) is acted on where that still makes sense
    later: a count during a stock-take that was open is added. Anything else
    is listed for staff, at the time it was said, with the questions the
    robot couldn't answer. Chatter not addressed to it is dropped, like live.
    503 when speech-to-text is down: the robot keeps the clip and retries.
    """
    device, err = views._device_or_401(request)
    if err:
        return err
    from ..models import RobotSyncEvent

    uid = str(request.data.get("client_uid") or "").strip()[:64]
    clip = request.FILES.get("audio")
    if not uid or clip is None:
        return Response({"detail": "client_uid and audio are required"},
                        status=status.HTTP_400_BAD_REQUEST)
    if RobotSyncEvent.objects.filter(device=device, client_uid=uid, applied=True).exists():
        return Response({"result": "duplicate"})
    try:
        transcript = audio_svc.transcribe(
            clip.read(), name=getattr(device, "wake_name", "") or "موس", strict=True,
            hints=teachers.hints_for(device))
    except audio_svc.SttUnavailable:
        return Response({"detail": "speech-to-text unavailable, retry later"},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)

    said_at = _recorded_at(request.data.get("recorded_at"))
    ptt = str(request.data.get("ptt", "")).lower() in ("1", "true", "yes")
    result = _apply_offline_voice(device, transcript.strip(), ptt=ptt, said_at=said_at)
    RobotSyncEvent.objects.update_or_create(
        device=device, client_uid=uid,
        defaults={"kind": "voice", "applied": True,
                  "payload": {"transcript": transcript[:500], "result": result,
                              "recorded_at": said_at.isoformat() if said_at else None}},
    )
    return Response({"result": result, "transcript": transcript})


def _apply_offline_voice(device, transcript: str, *, ptt: bool, said_at) -> str:
    """Act on one utterance heard offline; returns what became of it."""
    if not transcript:
        return "silence"
    addressed, text = wakename.strip_name(device, transcript)
    if not (addressed or ptt):
        return "not_addressed"
    if not text:
        return "name_only"
    when = said_at or timezone.now()

    # A count said during a stock-take that was open at the time.
    from ..models import RobotStockTakeSession
    session = (RobotStockTakeSession.objects
               .filter(device=device, status="open",
                       created_at__lte=when, created_at__gte=when - timedelta(hours=2))
               .order_by("-created_at").first())
    if session is not None:
        query, qty = services.parse_count_utterance(text)
        if query and qty is not None:
            _, product = services.add_stock_take_count(session, query=query, counted_qty=qty)
            if product is not None:
                _log_offline_turn(device, transcript, said_at, intent="command",
                                  payload={"action": "count_added", "product_id": product.id})
                return "count"

    # Everything else waits for a person: listed with what it couldn't answer.
    _log_offline_turn(device, transcript, said_at, intent="unknown",
                      payload={"unresolved": True})
    return "logged"


def _log_offline_turn(device, transcript, said_at, *, intent, payload):
    turn = views.RobotVoiceInteraction.objects.create(
        device=device, transcript=transcript, intent=intent, reply_text="",
        payload={**payload, "offline": True},
    )
    if said_at is not None:                      # dated when it was said
        views.RobotVoiceInteraction.objects.filter(pk=turn.pk).update(created_at=said_at)
