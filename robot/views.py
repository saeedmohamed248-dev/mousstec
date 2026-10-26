"""
robot/views.py — REST API the ESP32 firmware calls.

Auth model: the ESP32 authenticates as a *device* via `X-Robot-Token`
(`robot.security.authenticate_device`), not as a Django user — the robot stands
on the shop floor, not behind a login. Privileged actions (creating a sale,
dispensing a part) additionally require a face-authorized employee in the same
request, enforced here. The device may send the face embedding itself, or echo
back an `employee_id` from a face match this same device was granted in the
last few minutes — an id on its own is never accepted, or the device token
alone would be enough to sell as anybody.

Every product/price response is built by `robot.pricing.safe_product_payload`,
so wholesale/cost can never reach the device.

Endpoints (prefix /api/robot/v1/ — see urls.py):
  POST heartbeat/            device liveness + firmware version
  POST scan/                 ESP32-CAM part image → identify → stock/price (or scrap price)
  POST voice/                transcript → intent → spoken reply (workshop assistant + POS)
  POST face/                 face embedding → attendance clock-in/out + authorize session
  POST sale/                 create a retail sale from a scan (requires face auth)
  POST motor/                queue a physical-articulation command (requires face auth)
  GET  motor/pending/        commands the ESP32 should execute + ack
  GET  procurement-signals/  open low-stock signals for the Procurement Agent
  POST teach/                staff correct a scan / teach an alias (learning loop)
  POST enroll/capture/       camera capture for the staff face-enrollment round
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes, throttle_classes,
)
from rest_framework.response import Response

from . import audio as audio_svc
from . import customers as customers_svc
from . import assistant, entries, enrollment, faces, headfollow, language, memory, permissions, replies, restock, services, security, teachers, vision, wakename
from .brain import talk
from .brain.knowledge import NOT_HERE, available, pieces
from .models import (
    MotorCommandLog, ProcurementSignal, RobotAccessLog, RobotCommand,
    RobotDevice, RobotScanEvent, RobotSnapshot, RobotVoiceInteraction,
)
from .pricing import safe_product_payload

# Confidence below which vision must defer to the printed barcode.
_MIN_VISION_CONFIDENCE = 0.75

# How long a successful face authorization stays valid for follow-up privileged
# calls from the same device. The firmware scans a face once and then echoes the
# employee id back on the sale/motor calls of that visit; beyond this window it
# has to show the face again.
_FACE_SESSION_MINUTES = 10


def _device_or_401(request):
    """Resolve the device from its token, or return a 401 Response."""
    device = security.authenticate_device(request)
    if device is None:
        return None, Response(
            {"detail": "invalid or missing X-Robot-Token"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return device, None


def _authorized_employee(request, device):
    """Return the face-authorized employee for this request, or None.

    The device sends the current face embedding (or an employee id it already
    resolved this session) with privileged calls. Security gate for sales /
    dispensing per the requirements.
    """
    embedding = request.data.get("face_embedding")
    if embedding:
        emp, score = security.identify_employee(embedding, branch=device.branch)
        return emp

    # An employee id echoed back by the firmware is NOT proof of anything on its
    # own — anyone holding the device token could post any id. Honour it only
    # when THIS device actually granted that employee a face match in the last
    # few minutes, which `face/` recorded in RobotAccessLog.
    emp_id = request.data.get("employee_id")
    if emp_id:
        since = timezone.now() - timedelta(minutes=_FACE_SESSION_MINUTES)
        granted = (
            RobotAccessLog.objects
            .filter(
                device=device,
                employee_id=emp_id,
                result="granted",
                created_at__gte=since,
            )
            .select_related("employee")
            .order_by("-created_at")
            .first()
        )
        if granted and granted.employee and _employee_is_active(granted.employee):
            return granted.employee
    return None


# The mic is on the bridge board and the camera on the other one, so a spoken
# command carries no face. We use whoever is in front of the camera RIGHT NOW:
# the LATEST face check on this device (the cam re-checks every few seconds
# while someone's there), and only if it's recent and a granted match. A
# manager who merely walked past earlier doesn't count — the moment a
# customer (or nobody) is in frame, the latest check is "unknown" and voice
# gets no staff identity. Sales/motion still need their own face match.
_VOICE_FACE_SECONDS = 15


def _voice_employee(request, device):
    """Employee for a voice turn: explicit face auth, else the face the
    camera matched in its latest check (≤ `_VOICE_FACE_SECONDS` old)."""
    employee = _authorized_employee(request, device)
    if employee is not None:
        return employee
    since = timezone.now() - timedelta(seconds=_VOICE_FACE_SECONDS)
    latest = (RobotAccessLog.objects
              .filter(device=device, created_at__gte=since)
              .select_related("employee").order_by("-created_at").first())
    if (latest and latest.result == "granted" and latest.employee
            and _employee_is_active(latest.employee)):
        return latest.employee
    return None


def _employee_is_active(employee) -> bool:
    """True unless the employee's linked user account has been deactivated."""
    user = getattr(employee, "user", None)
    return True if user is None else bool(user.is_active)


def _require_permission(request, device, action):
    """Resolve the face-authorized employee AND check their role may do `action`.

    Returns (employee, None) when allowed, or (None, Response) with 403 —
    unrecognized face → generic deny; recognized but wrong role → spoken reason.
    """
    employee = _authorized_employee(request, device)
    if not employee:
        return None, Response(
            {"authorized": False, "detail": "الوجه غير مصرّح."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if not permissions.employee_can(employee, action):
        return None, Response(
            {
                "authorized": True,
                "permitted": False,
                "role": permissions.employee_role(employee),
                "detail": permissions.denial_message(action),
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    return employee, None


# All robot endpoints authenticate by device token, not Django session/JWT,
# and are rate-limited per device (not by the global anonymous-per-IP limit).
def _robot_endpoint(view, throttles=None):
    return api_view(["GET", "POST"])(
        authentication_classes([])(permission_classes([])(
            throttle_classes(throttles or [security.RobotDeviceThrottle])(view)))
    )


def _kiosk_lookup_endpoint(view):
    return _robot_endpoint(
        view, throttles=[security.RobotDeviceThrottle, security.KioskLookupThrottle])


@_robot_endpoint
def heartbeat(request):
    device, err = _device_or_401(request)
    if err:
        return err
    fw = request.data.get("firmware_version")
    if fw:
        device.firmware_version = str(fw)[:20]
        device.save(update_fields=["firmware_version"])
    # A part customers were waiting for is back: tell the staff. Checked here
    # (every 30 s) so any way the stock rose — a purchase, a transfer, a count
    # — is seen. An extra: the heartbeat answers regardless.
    try:
        restock.sweep(device)
    except Exception:
        services.logger.warning("robot heartbeat: restock sweep failed", exc_info=True)
    return Response({
        "ok": True,
        "device": device.name,
        "branch": device.branch.name,
        "server_time": timezone.now().isoformat(),
        # The bridge turns on its fast voice only when the server can take it:
        # µ-law both ways, and /voice/ answering with the spoken reply itself.
        "voice": {"ulaw": True},
    })


@_robot_endpoint
def scan(request):
    """ESP32-CAM part capture → identify → stock+retail price (or scrap price).

    Body: image (file) or `code` (barcode text the cam decoded), `purpose`
    (pos|scrap|lookup). For confident matches returns a retail-only payload; for
    the scrap flow adds a condition-based suggested retail price. Raises a
    procurement signal if stock is critically low.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    purpose = request.data.get("purpose", "pos")
    code = (request.data.get("code") or "").strip()
    image = request.FILES.get("image")

    label, part_number, confidence = "", "", 0.0
    product = None

    # Read the image bytes once, up front, so both vision and the scrap step can
    # use them regardless of whether a barcode `code` was also supplied. Then
    # rewind the file so it saves in full to the ImageField below.
    image_bytes = None
    if image is not None:
        image_bytes = image.read()
        try:
            image.seek(0)
        except Exception:
            pass

    # Prefer a decoded barcode (exact); else run vision on the image. Either
    # way what staff taught the robot is consulted before the raw catalogue,
    # and a photo it has seen before is recognized by its look alone.
    recognized_by = ""
    if code:
        product = (services.resolve_from_knowledge(code=code)
                   or services.find_product(code))
        confidence = 1.0 if product else 0.0
        part_number = code
        recognized_by = "code" if product else ""
    elif image_bytes:
        label, part_number, confidence = vision.identify_part(image_bytes)
        if confidence >= _MIN_VISION_CONFIDENCE and part_number:
            product = (services.resolve_from_knowledge(code=part_number)
                       or services.find_product(part_number))
            recognized_by = "vision" if product else ""
        if product is None:
            product, distance = services.match_fingerprint(
                services.image_hash(image_bytes))
            if product is not None:
                # Similar-looking parts exist, so a look-alone match is shown
                # for a human to confirm, never taken as certain.
                recognized_by = "learned_look"
                confidence = round(1.0 - distance / 64.0, 3)

    event = RobotScanEvent.objects.create(
        device=device, purpose=purpose, image=image,
        recognized_label=label, recognized_part_number=part_number,
        confidence=confidence, product=product,
    )

    if not product:
        miss = {
            "found": False,
            "scan_id": event.id,
            "message": "مش متأكد من القطعة دي، ورّيني الباركود اللي عليها لو سمحت.",
        }
        _announce_scan(request, device, miss)
        return Response(miss)

    payload = safe_product_payload(
        product, branch=device.branch, include_scrap=(purpose == "scrap"),
    )
    payload["found"] = True
    payload["scan_id"] = event.id
    payload["recognized_by"] = recognized_by
    payload["needs_confirmation"] = recognized_by == "learned_look"

    # Scrap flow: assess wear and suggest a RETAIL price.
    if purpose == "scrap" and image_bytes:
        cond, notes = vision.assess_condition(image_bytes)
        try:
            calibration = services.used_price_calibration()
        except Exception:
            calibration = 1.0
        suggested = services.suggest_used_price(product, cond, calibration)
        notes = dict(notes or {})
        notes["raw_suggested_price"] = float(services.suggest_used_price(product, cond))
        notes["calibration"] = calibration
        event.condition_score = cond
        event.condition_notes = notes
        event.suggested_price = suggested
        event.save(update_fields=["condition_score", "condition_notes", "suggested_price"])
        payload["condition_score"] = cond
        payload["suggested_price"] = float(suggested)

    # Multi-agent: low stock → procurement signal (idempotent).
    signal = services.maybe_raise_procurement_signal(
        device=device, product=product, branch=device.branch,
    )
    payload["low_stock_signal"] = bool(signal)
    _announce_scan(request, device, payload)
    return Response(payload)


def _announce_scan(request, device, payload):
    """A scan someone asked for by voice gets its answer spoken by the robot.

    The camera board has no speaker, so when the scan answers a queued `scan`
    command we mark it done and queue a `say` for the bridge ESP32.
    """
    cmd_id = request.data.get("command_id")
    if not cmd_id:
        return
    cmd = RobotCommand.objects.filter(pk=cmd_id, device=device).first()
    if cmd is None:
        return
    RobotCommand.objects.filter(pk=cmd.pk).update(
        status="done", done_at=timezone.now(), result={"scan_id": payload.get("scan_id")},
    )
    if not payload.get("found"):
        text = "مش قادر أتعرف على القطعة دي — قرّب الباركود للكاميرا."
    else:
        if not payload.get("needs_confirmation"):
            # «عليه ضمان؟», «فيه أرخص؟» next are about the part just scanned.
            memory.remember_part(device, payload.get("id"))
        stock = payload.get("stock", 0)
        price = payload.get("suggested_price") or payload.get("retail_price") or 0
        text = f"دي {payload.get('name')}. "
        said = f" بسعر {services.spoken_price(price)}" if price > 0 else ""
        text += (f"عندي منها {pieces(stock)}{said}." if stock
                 else "مش موجودة هنا دلوقتي.")
        if payload.get("needs_confirmation"):
            text += " عرفتها من شكلها، أكّدها من فضلك."
    # Answered in the language the scan was asked for in.
    text = language.reply_in((cmd.payload or {}).get("lang", "ar"), text)
    RobotCommand.objects.create(device=device, kind="say", payload={"text": text})


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
    device, err = _device_or_401(request)
    if err:
        return err
    timing = {"t0": time.monotonic()}
    said_at = _said_at(request)

    # Accept either a ready transcript OR raw audio (transcribed server-side).
    transcript = (request.data.get("transcript") or "").strip()
    audio = request.FILES.get("audio")
    if not transcript and audio is not None:
        try:
            transcript = audio_svc.transcribe(
                audio.read(), name=getattr(device, "wake_name", "") or "موس", strict=True) or ""
        except audio_svc.SttUnavailable as exc:
            # 🐛 [FIX]: speech-to-text being down (no key, Gemini's quota or an
            # outage) came back as «heard nothing», word for word what a cough
            # does: the shop saw a deaf robot and no reason. It says why now.
            timing["hear"] = time.monotonic()
            return _voice_reply(request, timing, {
                "intent": "ignored", "addressed": False, "reason": "stt_down",
                "detail": str(exc)[:200], "reply": ""})
    timing["hear"] = time.monotonic()

    ptt = str(request.data.get("ptt", "")).lower() in ("1", "true", "yes")
    # Nothing understood: noise, a mumble, or speech-to-text is down. Said so
    # (`reason`), so the robot's log tells this apart from "not my name";
    # with the button held the person clearly spoke to it, so it asks again.
    if not transcript:
        return _voice_reply(request, timing, {
            "intent": "ignored", "addressed": ptt, "reason": "no_transcript",
            "reply": "معلش مسمعتكش كويس، قول تاني." if ptt else ""})

    # Only speech addressed to the robot by name gets an answer. People
    # talking to each other nearby are ignored — not answered, not stored.
    # Only the enrollment round's own control words ("مش موجود", "التالي")
    # skip the name — any other chatter while names are being called is still
    # ignored. A stock count needs no exception: every accepted count extends
    # the 20 s follow-up window, so a steady counter never repeats the name.
    # Same for a branch name answering «أسجلها على أنهي فرع؟» («المعادي»).
    ongoing = ((_is_enrollment_control(transcript)
                and enrollment.active_session(device) is not None)
               or entries.is_answer(device, transcript))
    for_robot, text, name_only = wakename.gate(
        device, transcript, push_to_talk=ptt, ongoing_flow=ongoing, said_at=said_at)
    if not for_robot:
        # What was heard goes back to the robot's own log only (stored nowhere):
        # «يا موز» told apart from chatter when it «doesn't answer».
        return Response({"intent": "ignored", "reply": "", "addressed": False,
                         "transcript": transcript[:160]})
    wakename.keep_listening(device)
    # «🎓 تعلّم السمع»: «مخ موس» wasn't sure it heard right (`check=1`, with
    # the recording) — a teacher hears it, and its words are the question.
    heard_by = ""
    if (audio is not None and not name_only and request.data.get("transcript")
            and str(request.data.get("check", "")).lower() in ("1", "true", "yes")):
        try:
            confidence = float(request.data.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        got = teachers.check(device, audio.read(), text, confidence)
        if got is not None:
            text, heard_by = got.text, got.teacher
    # Answer in the language they spoke: Egyptian Arabic, or English.
    lang = language.detect(text or transcript)
    if name_only:
        # «يا موس» on its own after a pause: likely someone new (robot/memory.py).
        memory.fresh_start(device)
        timing["think"] = time.monotonic()
        return _voice_reply(request, timing, {
            "intent": "wake", "addressed": True, "transcript": transcript, "lang": lang,
            "reply": "Yes? How can I help?" if lang == "en" else "أيوه، تحت أمرك."})

    employee = _voice_employee(request, device)
    # What the caller can do beyond speaking: «مخ موس» on a computer sends
    # `caps=media` (it has speakers for the Quran and music); the ESP32 doesn't.
    caps = frozenset(c.strip() for c in str(request.data.get("caps", "")).lower().split(",") if c.strip())
    intent, reply, payload = _handle_voice(text, device, employee, lang=lang, caps=caps)
    reply = language.reply_in(lang, reply)
    timing["think"] = time.monotonic()
    # What was just said, for the next sentence (robot/memory.py).
    memory.note_turn(device, text, reply)

    RobotVoiceInteraction.objects.create(
        device=device, transcript=transcript, intent=intent,
        reply_text=reply, employee=employee, payload=payload,
    )
    # 🐛 [FIX]: الـ payload كان بييجي بعد «reply» فبيكتب فوقه: الرد الجاهز رجّع {"reply": "hours"}
    # والروبوت قال «hours» و«address» بدل المواعيد والعنوان. دلوقتي الـ payload الأول، والرد آخر كلمة.
    response = _voice_reply(request, timing, {
        **payload, "intent": intent, "reply": reply, "transcript": transcript,
        "addressed": True, "lang": lang,
        **({"heard_by": heard_by, "taught": text} if heard_by else {})})
    # 🐛 [FIX]: الـ ٢٠ ثانية كانت بتبتدي من السؤال. رد طويل جه بعد ٧ ثواني واتقال في ٩،
    # فـ«الزيت» اللي اتقالت بعده لقت الشباك قفل والروبوت قال «مش ليا». دلوقتي من بعد ما يخلّص رده.
    wakename.keep_listening(device, speaking=reply)
    return response


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

    codec = str(request.data.get("reply_audio", "")).lower()
    reply = body.get("reply") or ""
    if codec not in (audio_svc.ULAW, audio_svc.PCM) or not reply:
        return Response(body)
    voices = []
    wav = audio_svc.synthesize_wav(reply, codec=codec, providers=voices)
    timing["voice"] = time.monotonic()
    if not wav:
        return Response(body)
    from django.http import HttpResponse
    resp = HttpResponse(wav, content_type="audio/wav")
    resp["X-Robot-Addressed"] = "1" if body.get("addressed") else "0"
    resp["X-Robot-Intent"] = quote(str(body.get("intent", ""))[:40], safe="")
    resp["X-Robot-Transcript"] = quote(str(body.get("transcript", ""))[:160], safe="")
    resp["X-Robot-Reply"] = quote(reply[:160], safe="")
    marks, last, spent = ("hear", "think", "voice"), timing["t0"], []
    for mark in marks:
        if mark in timing:
            spent.append(f"{mark}={int((timing[mark] - last) * 1000)}")
            last = timing[mark]
    # Which voice said it (edge = the Egyptian one; gtts = the stand-in when
    # Edge is down; cache = said before), so «مش مصري» can be told apart.
    if voices:
        spent.append("tts=" + "+".join(voices))
    resp["X-Robot-Timing"] = ";".join(spent)
    if body.get("heard_by"):                       # «🎓»: who heard it, and what
        resp["X-Robot-Heard-By"] = body["heard_by"]
        resp["X-Robot-Taught"] = quote(str(body.get("taught", ""))[:160], safe="")
    # A media command («شغّل قرآن») travels beside the spoken «حاضر».
    media = assistant.media_header(body)
    if media:
        resp["X-Robot-Media"] = media
    return resp


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
    device, err = _device_or_401(request)
    if err:
        return err
    from .models import RobotSyncEvent

    uid = str(request.data.get("client_uid") or "").strip()[:64]
    clip = request.FILES.get("audio")
    if not uid or clip is None:
        return Response({"detail": "client_uid and audio are required"},
                        status=status.HTTP_400_BAD_REQUEST)
    if RobotSyncEvent.objects.filter(device=device, client_uid=uid, applied=True).exists():
        return Response({"result": "duplicate"})
    try:
        transcript = audio_svc.transcribe(
            clip.read(), name=getattr(device, "wake_name", "") or "موس", strict=True)
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
    from .models import RobotStockTakeSession
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
    turn = RobotVoiceInteraction.objects.create(
        device=device, transcript=transcript, intent=intent, reply_text="",
        payload={**payload, "offline": True},
    )
    if said_at is not None:                      # dated when it was said
        RobotVoiceInteraction.objects.filter(pk=turn.pk).update(created_at=said_at)


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


def _handle_voice(transcript: str, device, employee=None, lang: str = "ar", caps=frozenset()):
    """Tiny bilingual intent router for the voice assistant. Replies are in
    Egyptian Arabic (`voice` translates them for an English speaker); only
    the free-form LLM answer is asked for in `lang` directly.

    «السلام عليكم، عندك طرمبة؟»: the greeting is answered first, and it's a
    new conversation (talk.opening) — the rest is answered as asked."""
    greeting, rest, _clips = talk.opening(transcript)
    if not greeting:
        return _answer_voice(transcript, device, employee, lang, caps)
    memory.forget(device)
    intent, reply, payload = _answer_voice(rest, device, employee, lang, caps)
    return intent, f"{greeting} {reply}", payload


def _answer_voice(transcript: str, device, employee=None, lang: str = "ar", caps=frozenset()):
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
        from .models import RobotPageCall
        page = (RobotPageCall.objects
                .filter(device=device, target_employee=employee, status="announced",
                        announced_at__gte=timezone.now() - timedelta(minutes=15))
                .order_by("-announced_at").first())
        if page is not None:
            page.status = "acknowledged"
            page.save(update_fields=["status"])
            # Let whoever paged see the answer in their alerts feed.
            from .models import RobotAlert
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

    # --- The staff's questions: «بعنا كام النهارده؟», «إيه اللي قرّب يخلص؟» --
    question = assistant.staff_question(transcript)
    if question is not None:
        return ("command",) + assistant.staff_answer(question, device, employee)

    # --- «دوّر على النت على …»: what the ERP doesn't know (role-gated) ----
    if assistant.web_question(transcript):
        return ("command",) + assistant.web_reply(transcript, employee, device, lang)

    # --- "امسح القطعة دي": ask the camera to look -------------------------
    if any(w in low for w in ("امسح القطعة", "شوف القطعة", "اعرف القطعة", "scan this")):
        RobotCommand.objects.create(device=device, kind="scan",
                                    payload={"purpose": "lookup", "lang": lang})
        return ("command", "ثانية واحدة، قرّب القطعة من الكاميرا.", {"action": "scan_requested"})

    # --- Expense / purchase: held until a branch is named ---------------
    # Before the stock-take block: «مصروف بنزين 200» would otherwise be read
    # as a count line during an open count.
    started = entries.voice_request(transcript, device, employee)
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
            ans = safe_product_payload(product, branch=device.branch)
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
        general = services.ai_reply(transcript, lang, facts=facts, history=turns,
                                    branch=getattr(device.branch, "name", "") or "")
        if general:
            return "unknown", general, {"unresolved": True, "ai": True}
        return ("inventory_query", "ملقتش القطعة دي في المخزون. ممكن تقولي رقمها؟",
                {"unresolved": True})
    from inventory.models import Product
    return _part_reply(ans, device, Product.objects.filter(pk=ans.get("id")).first())


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


def _attendance_greeting(employee, action: str) -> str:
    """What the robot says when a face clocks someone in or out ('' otherwise:
    a passive sighting while already clocked in stays silent)."""
    first = (getattr(employee, "name", "") or "").split()
    name = f" يا {first[0]}" if first else ""
    if action == "clock_in":
        return f"أهلاً{name}، سجّلت حضورك."
    if action == "clock_out":
        return f"مع السلامة{name}، سجّلت انصرافك."
    return ""


@_robot_endpoint
def face(request):
    """Facial recognition → attendance clock-in/out + session authorization.

    Body: `face_embedding` (list of floats) OR `image` (a JPEG the ESP32-CAM
    sends — the embedding is then extracted server-side). Logs the event, clocks
    the employee in/out via hr.AttendanceRecord, and returns whether the session
    is authorized to create sales.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    # Accept a ready embedding, or extract one from a posted image.
    embedding = request.data.get("face_embedding")
    if not embedding and request.FILES.get("image") is not None:
        img = request.FILES["image"]
        img_bytes = img.read()
        try:
            img.seek(0)
        except Exception:
            pass
        embedding = faces.extract_embedding(img_bytes)

    # Without a real face model nothing is authorized, and saying so beats
    # logging a silent "unknown" that looks like a badly-lit photo.
    if not security.matching_available():
        return Response(
            {
                "authorized": False,
                "result": "unavailable",
                "detail": (
                    "التعرّف على الوجه مش مفعّل: مفيش موديل وجه حقيقي مثبّت، "
                    "والبديل مايقدرش يفرّق بين الأشخاص."
                ),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # A frame with nobody's face in it (the camera checks on any motion) is
    # not an unknown person: nothing to log.
    if not embedding:
        return Response({"authorized": False, "result": "no_face"})

    employee, score = security.identify_employee(embedding, branch=device.branch)

    if not employee:
        RobotAccessLog.objects.create(
            device=device, result="unknown", action="scan",
            match_score=score, image=request.FILES.get("image"),
        )
        return Response({"authorized": False, "result": "unknown", "match_score": score})

    # "authorize" = seen while authorizing / on motion; "attendance" = a
    # deliberate check-in/out. Only the latter can end a shift.
    purpose = str(request.data.get("purpose") or "attendance").lower()
    if purpose not in ("attendance", "authorize"):
        purpose = "attendance"
    action, record = security.register_attendance(
        employee, match_score=score, purpose=purpose,
    )
    RobotAccessLog.objects.create(
        device=device, employee=employee, result="granted", action=action,
        match_score=score, image=request.FILES.get("image"),
    )
    # Say it out loud, so whoever walked in knows the robot logged them.
    greeting = _attendance_greeting(employee, action)
    if action == "clock_in":
        # The first face of the day: a manager hears yesterday and what's
        # waiting. An extra — never worth losing the clock-in over.
        try:
            briefing = assistant.morning_briefing(employee, device)
        except Exception:
            services.logger.warning("robot face: no morning briefing", exc_info=True)
            briefing = ""
        if briefing:
            greeting += " " + briefing
    if greeting:
        RobotCommand.objects.create(device=device, kind="say", payload={"text": greeting})
    return Response({
        "authorized": True,
        "employee_id": employee.id,
        "employee_name": employee.name,
        "action": action,
        "match_score": score,
    })


@_robot_endpoint
def customer_greet(request):
    """Greet a walk-in customer, recognizing them by face / name / phone / invoice.

    Body (any of): `image` (face), `name`, `phone`, `invoice_number`. On a match
    the robot remembers the visit (and enrolls the face for next time when an
    image is given) and returns a warm, personal greeting plus a private
    `staff_note` (e.g. outstanding balance) that is NOT part of the spoken text.

    No employee auth needed — this is customer-facing and never exposes pricing
    beyond the customer's own record.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    # Someone new in front of the robot: the last customer's part and car aren't theirs.
    memory.fresh_start(device)

    # Face embedding from a posted image, if any.
    embedding = request.data.get("face_embedding")
    if not embedding and request.FILES.get("image") is not None:
        img = request.FILES["image"]
        b = img.read()
        embedding = faces.extract_embedding(b)

    customer, method, score = customers_svc.recognize_customer(
        embedding=embedding,
        name=(request.data.get("name") or "").strip(),
        phone=(request.data.get("phone") or "").strip(),
        invoice_number=(request.data.get("invoice_number") or "").strip(),
    )

    if not customer:
        return Response({
            "recognized": False,
            "greeting": f"{services.welcome()}! أنا تحت أمرك — محتاج قطعة أو استفسار؟",
        })

    # Enrolling the customer's face writes biometric data, so it needs a staff
    # member present who is allowed to do it. Greeting and recognising an
    # already-enrolled customer stay open — this only gates the write.
    may_enroll = permissions.employee_can(
        _authorized_employee(request, device), "customer_enroll"
    )
    face = customers_svc.remember_visit(
        customer, embedding=embedding, may_enroll_face=may_enroll
    )
    info = customers_svc.customer_greeting(
        customer, method=method, visit_count=face.visit_count,
        notes=face.notes,
    )

    # Turn the head to face the customer, if the cam told us where they are.
    if request.data.get("face_offset") is not None:
        services.look_at(device, request.data.get("face_offset"))

    return Response({
        "recognized": True,
        "customer_id": customer.id,
        "customer_name": customer.name,
        "recognized_by": method,
        "match_score": score,
        "visit_count": face.visit_count,
        **info,
    })


@_robot_endpoint
def sale(request):
    """Create a retail sale from a scan. Requires a face-authorized employee.

    Body: `scan_id` OR `part_number`, `customer_name`, `customer_phone`,
    `payment` (cash|credit), `branch_id`, `face_embedding` (or `employee_id`),
    optional `quantity`, `unit_price` (retail only). Security gate per
    requirements. Without `branch_id` nothing is created: the 428 reply holds
    the question «أسجلها على أنهي فرع؟» and the branch list.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    employee, perr = _require_permission(request, device, "sale")
    if perr:
        return perr

    # Resolve product from scan or part number.
    product = None
    scan_id = request.data.get("scan_id")
    if scan_id:
        ev = RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
        product = ev.product if ev else None
    if product is None:
        product = services.find_product(request.data.get("part_number", ""))
    if product is None:
        return Response({"detail": "القطعة غير موجودة."}, status=status.HTTP_404_NOT_FOUND)

    # Resolve / create the customer by phone (phone is unique in CRM).
    from inventory.models import Customer
    phone = (request.data.get("customer_phone") or "").strip()
    name = (request.data.get("customer_name") or "عميل نقدي").strip()
    customer = None
    if phone:
        customer, _c = Customer.get_or_create_by_phone(phone, defaults={"name": name})
    else:
        customer, _c = Customer.get_or_create_by_phone(
            "0000000000", defaults={"name": "عميل نقدي"},
        )

    # Quantity and price come off the wire, so neither is trusted. A bad value
    # here is a wrong invoice, not a 500: reject it with a reason.
    try:
        quantity = int(request.data.get("quantity", 1) or 1)
    except (TypeError, ValueError):
        return Response(
            {"detail": "الكمية غير صالحة."}, status=status.HTTP_400_BAD_REQUEST
        )
    if quantity < 1:
        return Response(
            {"detail": "الكمية لازم تكون ١ أو أكتر."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Which branch is this invoice for? Never assumed: without `branch_id` the
    # reply is the question to ask and the branches to pick from (428), and
    # the device posts again with the answer.
    branch_id = request.data.get("branch_id")
    if branch_id in (None, ""):
        return Response(entries.branch_choices(device),
                        status=status.HTTP_428_PRECONDITION_REQUIRED)
    branch = entries.branch_by_id(branch_id)
    if branch is None:
        return Response({"detail": "الفرع غير موجود."}, status=status.HTTP_400_BAD_REQUEST)

    # A cash sale needs somewhere to put the cash. Without a cash treasury the
    # invoice would post as if unpaid while the customer has paid.
    payment = request.data.get("payment", "cash")
    if services.is_cash_payment(payment) and entries.cash_treasury(branch) is None:
        return Response(
            {"detail": f"فرع {branch.name} مالوش خزنة كاش — ضيف خزنة كاش للفرع "
                       "أو سجّل البيع آجل."},
            status=status.HTTP_409_CONFLICT,
        )
    # Same rule as the POS: credit with no phone lands the debt on the shared
    # walk-in customer, and nobody knows who owes it.
    if not phone and not services.is_cash_payment(payment):
        return Response(
            {"detail": "البيع الآجل محتاج رقم موبايل العميل — من غيره المديونية "
                       "بتتسجّل على «عميل نقدي» ومحدش يعرف مين عليه الفلوس."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Don't sell what the branch doesn't have. Staff can still take an order
    # for a part on its way by saying so explicitly (`allow_backorder`).
    backorder = str(request.data.get("allow_backorder", "")).lower() in ("1", "true", "yes")
    on_hand = services.branch_stock(product, branch)
    if quantity > on_hand and not backorder:
        return Response(
            {"detail": f"المتاح في فرع {branch.name} {on_hand} بس من {product.name}.",
             "on_hand": on_hand},
            status=status.HTTP_409_CONFLICT,
        )

    unit_price = request.data.get("unit_price")
    if unit_price in (None, ""):
        unit_price = None  # services.create_robot_sale falls back to retail
    else:
        try:
            unit_price = Decimal(str(unit_price))
        except (InvalidOperation, TypeError, ValueError):
            return Response(
                {"detail": "السعر غير صالح."}, status=status.HTTP_400_BAD_REQUEST
            )
        if unit_price <= 0:
            return Response(
                {"detail": "السعر لازم يكون أكبر من صفر."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Price floor: a device-supplied price may not go below the part's scrap
        # price (the retail-side floor for a used part). This closes the "sell at
        # 1 EGP" hole the PR flagged, while still allowing a used-part discount
        # down to scrap. Parts with no scrap price set impose no floor here —
        # that stays a per-shop business decision, not a silent hardcode.
        floor = Decimal(str(getattr(product, "scrap_price", 0) or 0))
        if floor > 0 and unit_price < floor:
            return Response(
                {"detail": f"السعر لا يقل عن سعر الخردة ({floor:.0f} ج.م)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    from django.core.exceptions import ValidationError as _DjangoValidationError
    is_backorder = quantity > on_hand
    try:
        invoice = services.create_robot_sale(
            product=product, branch=branch, customer=customer,
            employee=employee, quantity=quantity, unit_price=unit_price,
            payment=payment, post=not is_backorder,
        )
    except _DjangoValidationError as exc:   # e.g. stock moved between check and post
        return Response({"detail": "؛ ".join(exc.messages)}, status=status.HTTP_409_CONFLICT)

    # A name given without a phone used to be dropped: the sale showed as
    # «عميل نقدي» in the invoices list. Keep it on the invoice, like the POS.
    typed_name = (request.data.get("customer_name") or "").strip()
    if not phone and typed_name and typed_name != "عميل نقدي":
        from inventory.models import SaleInvoice
        invoice.set_walk_in_name(typed_name)
        SaleInvoice.objects.filter(pk=invoice.pk).update(notes=invoice.notes)
    entries.log_sale(device=device, branch=branch, invoice=invoice, employee=employee,
                     summary=(f"أوردر {quantity} {product.name} (مش متوفر)" if is_backorder
                              else f"بيع {quantity} {product.name}"))

    # Link the scan to the invoice for the audit trail, and LEARN from the
    # confirmed match (scanned code/label → this product) so recognition of the
    # same part gets faster and more confident next time.
    if scan_id:
        ev = RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
        if ev:
            ev.sale_invoice = invoice
            ev.created_by = employee
            ev.save(update_fields=["sale_invoice", "created_by"])
            services.learn_from_confirmation(
                product=product,
                code=ev.recognized_part_number or "",
                label=ev.recognized_label or "",
                fingerprint_hash=services.scan_fingerprint(ev),
                employee=employee,
            )

    # Remember what this customer buys (car model / category) for next visit.
    try:
        customers_svc.learn_from_purchase(customer, product, quantity=quantity)
    except Exception:
        pass  # a memory hiccup must never undo a posted sale

    # Low-stock check after the sale deducted stock.
    services.maybe_raise_procurement_signal(
        device=device, product=product, branch=branch,
    )

    return Response({
        "ok": True,
        "invoice_id": invoice.id,
        "branch": branch.name,
        "total_amount": float(invoice.total_amount),
        "status": invoice.status,
        "backorder": is_backorder,
        "authorized_by": employee.name,
    }, status=status.HTTP_201_CREATED)


@_robot_endpoint
def motor(request):
    """Queue a physical-articulation command (head/arms/tracks).

    Requires a face-authorized employee (safety: no arm lift by anonymous
    request). Body: `actuator`, `direction`, `duration_ms`, face auth.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "motor")
    if perr:
        return perr
    try:
        actuator, direction, duration_ms = services.validate_motor_command(
            request.data.get("actuator"), request.data.get("direction"),
            request.data.get("duration_ms"),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    cmd = MotorCommandLog.objects.create(
        device=device, actuator=actuator, direction=direction,
        duration_ms=duration_ms, issued_by=getattr(employee, "user", None),
    )
    return Response({"ok": True, "command_id": cmd.id, "frame": cmd.serial_frame()},
                    status=status.HTTP_201_CREATED)


@_robot_endpoint
def motor_pending(request):
    """ESP32 polls for unacknowledged motor commands, then acks them.

    GET returns pending frames; the ESP32 forwards each to the Mega over serial
    and the same call marks them acknowledged (fire-and-forget model).
    """
    device, err = _device_or_401(request)
    if err:
        return err
    # 🐛 [FIX]: الأوامر مكانش ليها صلاحية. لو البريدج مش بيسحب (HAS_MOTORS 0،
    # أو النت قاطع) أوامر «لف الراس ناحية اللي بيتكلم» والداشبورد بتتراكم، وأول
    # ما يرجع يسحب كان بينفّذ القديم كله ورا بعض — راس ودراع بيتحركوا من غير ما
    # حد طالب دلوقتي. القديم ميتبعتش ويفضل acknowledged=False (متنفّذش فعلاً).
    fresh_from = timezone.now() - services.MOTOR_COMMAND_TTL
    pending = list(device.motor_commands
                   .filter(acknowledged=False, created_at__gte=fresh_from)
                   .order_by("created_at")[:20])
    frames = [{"command_id": c.id, "frame": c.serial_frame()} for c in pending]
    MotorCommandLog.objects.filter(id__in=[c.id for c in pending]).update(
        acknowledged=True, acknowledged_at=timezone.now(),
    )
    return Response({"commands": frames})


@_robot_endpoint
def intake(request):
    """Goods intake: photograph a part, register it, add stock.

    "صوّر الكنترول ده واعمله خلفية بيضا وسجّل البارت نمبر وقوله اسمه، ودوّر عليه
    في المخزون — لو موجود زوّد الكمية، لو مش موجود ضيف بند جديد بصورته وسعره."

    Requires a face-authorized employee (it writes stock + prices). Body:
    `image` (file, optional), `name`, `part_number` (optional — read from photo),
    `retail_price`, `quantity`, `car_model`, `part_category`, face auth.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "intake")
    if perr:
        return perr

    image = request.FILES.get("image")
    image_bytes = image.read() if image else None

    # Intake writes stock and prices, so bad numbers are a 400, not a 500 —
    # and a negative quantity would silently *remove* stock.
    try:
        quantity = int(request.data.get("quantity", 1) or 1)
    except (TypeError, ValueError):
        return Response({"detail": "الكمية غير صالحة."}, status=status.HTTP_400_BAD_REQUEST)
    if quantity < 1:
        return Response({"detail": "الكمية لازم تكون ١ أو أكتر."},
                        status=status.HTTP_400_BAD_REQUEST)
    retail_price = request.data.get("retail_price")
    if retail_price not in (None, ""):
        try:
            retail_price = Decimal(str(retail_price))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "السعر غير صالح."}, status=status.HTTP_400_BAD_REQUEST)
        if retail_price < 0:
            return Response({"detail": "السعر لازم يكون موجب."},
                            status=status.HTTP_400_BAD_REQUEST)

    result = services.intake_part(
        device=device, branch=device.branch, image_bytes=image_bytes,
        name=(request.data.get("name") or "").strip(),
        part_number=(request.data.get("part_number") or "").strip(),
        retail_price=retail_price if retail_price not in (None, "") else None,
        quantity=quantity,
        car_model=(request.data.get("car_model") or "").strip(),
        part_category=(request.data.get("part_category") or "").strip(),
        employee=employee,
    )
    return Response(result, status=status.HTTP_201_CREATED)


@_robot_endpoint
def stock_take(request):
    """Physical inventory count (جرد). Requires a face-authorized employee.

    Body: `counts` = [{"query": "<code/name>", "counted_qty": N}, ...],
    optional `instruction`. Returns a reconciled report; variances are approved
    from the dashboard (no silent stock change).
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "stock_take")
    if perr:
        return perr
    result = services.run_stock_take(
        device=device, branch=device.branch,
        counts=request.data.get("counts") or [],
        instruction=(request.data.get("instruction") or "").strip(),
        employee=employee,
    )
    return Response(result, status=status.HTTP_201_CREATED)


@_robot_endpoint
def stock_take_apply(request):
    """Apply a stock-take's variances to inventory. Requires face auth.

    Body: `session_id`. Sets each variance line's branch on-hand to the counted
    quantity (recording adjustment movements). Idempotent.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "stock_take_apply")
    if perr:
        return perr
    from .models import RobotStockTakeSession
    session = RobotStockTakeSession.objects.filter(
        pk=request.data.get("session_id"), device=device,
    ).first()
    if session is None:
        return Response({"detail": "جلسة الجرد غير موجودة."},
                        status=status.HTTP_404_NOT_FOUND)
    result = services.apply_stock_take(session, employee=employee)
    return Response(result)


@_robot_endpoint
def speak(request):
    """Text-to-speech: return synthesized audio bytes for the amp to play.

    Body: `text`. Returns audio/mpeg when a TTS provider is available, else 204
    so the firmware can fall back to on-device synthesis.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    text = (request.data.get("text") or "").strip()[:600]
    from django.http import HttpResponse
    # `format=wav` → 16 kHz mono PCM the ESP32 streams straight to the amp;
    # with `codec=ulaw`, µ-law: half the bytes over a slow line.
    if str(request.data.get("format", "")).lower() == "wav":
        codec = audio_svc.ULAW if str(request.data.get("codec", "")).lower() == audio_svc.ULAW \
            else audio_svc.PCM
        wav = audio_svc.synthesize_wav(text, codec=codec) if text else None
        if not wav:
            return Response(status=status.HTTP_204_NO_CONTENT)
        return HttpResponse(wav, content_type="audio/wav")
    audio_bytes = audio_svc.synthesize(text) if text else None
    if not audio_bytes:
        return Response(status=status.HTTP_204_NO_CONTENT)
    return HttpResponse(audio_bytes, content_type="audio/mpeg")


@_robot_endpoint
def camera_frame(request):
    """The ESP32-CAM pushes its latest live frame here (camera runs 24/7).

    Body: `image` (JPEG), optional `motion` (1 when the frame differs / PIR
    fired). Stores the frame for the dashboard live view; if motion is flagged
    AND it's after-hours, raises an alert with a saved snapshot.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    # «الراس يلف مع اللي بيتكلم»: while someone talks with the robot, the face
    # in this frame turns the head toward them (robot/headfollow.py). An extra:
    # it never costs the upload.
    following = headfollow.following(device)
    if image is not None and following:
        try:
            headfollow.follow(device, image.read())
        except Exception:
            services.logger.warning("robot camera: head follow failed", exc_info=True)
        finally:
            image.seek(0)
    if image is not None and device.camera_always_on:
        # The live view needs the LATEST frame, not every frame ever pushed.
        # Django writes a new file on each save and never removes the old one,
        # so with a camera running 24/7 this directory would grow until the
        # disk filled. Drop the previous frame as we replace it; deliberate
        # captures live in RobotSnapshot and are untouched.
        previous = device.last_frame
        device.last_frame = image
        device.last_frame_at = timezone.now()
        device.save(update_fields=["last_frame", "last_frame_at"])
        if previous:
            try:
                previous.delete(save=False)
            except Exception:
                pass  # a missing/locked old frame must never fail the upload

    motion = str(request.data.get("motion", "")).lower() in ("1", "true", "yes")
    alerted = False
    if motion:
        device.last_motion_at = timezone.now()
        device.save(update_fields=["last_motion_at"])
        if services.is_after_hours(device):
            snap = None
            if image is not None:
                try:
                    image.seek(0)
                except Exception:
                    pass
                snap = RobotSnapshot.objects.create(
                    device=device, image=image, reason="after_hours",
                )
            services.raise_after_hours_alert(device, snapshot=snap)
            alerted = True
    # Tell the camera how fast to push: fast while a supervisor is watching
    # (stream_until in the future), slow when idle — smooth video on demand
    # without hammering the network 24/7.
    # Camera-side work rides on this reply (the cam doesn't poll anything
    # else): pending snapshot/scan commands, and — during a staff enrollment
    # round — who to capture now.
    cam_cmds = services.take_camera_commands(device)
    try:
        enroll = enrollment.camera_prompt(device)
    except Exception:
        enroll = None
    push_ms = device.desired_push_interval_ms()
    if following:                         # a face seen twice in under a second
        push_ms = min(push_ms, headfollow.PUSH_MS)
    return Response({
        "ok": True, "motion": motion, "after_hours_alert": alerted,
        "push_interval_ms": 700 if enroll else push_ms,
        "commands": cam_cmds,
        "enroll": enroll,
        # For an outage: when the shop is closed the camera keeps a photo
        # every 2 s of motion, open hours one every 30 s.
        "guard": services.guard_window(device),
    })


@_robot_endpoint
def telemetry(request):
    """Device reports its health (battery/temp/disk/wifi) for the live view.

    Body: any of `battery_percent`, `cpu_temp`, `free_disk_mb`, `wifi_rssi`.
    Raises a low-battery alert (deduped) when the battery drops below 15%.
    """
    device, err = _device_or_401(request)
    if err:
        return err

    def _num(key, cast):
        v = request.data.get(key)
        if v in (None, ""):
            return None
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None

    # The body carries *any of* these, so only write what was actually sent —
    # a device reporting just its battery must not blank out the temperature,
    # disk and signal readings the dashboard is showing.
    changed = []
    for key, cast in (("battery_percent", int), ("cpu_temp", float),
                      ("free_disk_mb", int), ("wifi_rssi", int)):
        if key not in request.data:
            continue
        setattr(device, key, _num(key, cast))
        changed.append(key)
    device.telemetry_at = timezone.now()
    device.save(update_fields=changed + ["telemetry_at"])

    if device.battery_percent is not None and device.battery_percent < 15:
        services.raise_low_battery_alert(device, device.battery_percent)

    # Per-motor current (amps) from the Mega's current sensors → predictive
    # maintenance: stall and wear alerts before a motor dies.
    currents = request.data.get("motor_current")
    faults = []
    if isinstance(currents, dict) and currents:
        faults = services.record_motor_currents(device, currents)
    return Response({"ok": True, "motor_alerts": len(faults)})


@_robot_endpoint
def snapshot_upload(request):
    """Device uploads a still (usually answering a `snapshot` command)."""
    device, err = _device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is None:
        return Response({"detail": "no image"}, status=status.HTTP_400_BAD_REQUEST)
    reason = str(request.data.get("reason") or "manual")
    if reason not in dict(RobotSnapshot.REASON):
        reason = "manual"
    # Photos the camera kept on its SD card while offline come with the time
    # they were taken; motion in the guard window then is still an intrusion.
    captured = _recorded_at(request.data.get("captured_at"))
    after_hours = (captured is not None and reason == "motion"
                   and services.is_after_hours(device, timezone.localtime(captured)))
    if after_hours:
        reason = "after_hours"
    snap = RobotSnapshot.objects.create(device=device, image=image, reason=reason)
    if captured is not None:
        RobotSnapshot.objects.filter(pk=snap.pk).update(created_at=captured)
    if after_hours:
        services.raise_after_hours_alert(
            device, snapshot=snap,
            message=(f"🚨 حركة بعد غلق المحل الساعة {timezone.localtime(captured):%H:%M} "
                     "والنت كان فاصل: الكاميرا صوّرتها على الكارت وبعتتها لما النت رجع."))
    # Ack the originating command if one was referenced.
    cmd_id = request.data.get("command_id")
    if cmd_id:
        RobotCommand.objects.filter(pk=cmd_id, device=device).update(
            status="done", done_at=timezone.now(), result={"snapshot_id": snap.id},
        )
    return Response({"ok": True, "snapshot_id": snap.id}, status=status.HTTP_201_CREATED)


@_robot_endpoint
def commands_pending(request):
    """Device polls queued dashboard/owner commands, which are marked sent."""
    device, err = _device_or_401(request)
    if err:
        return err
    # Snapshot/scan belong to the camera board (delivered via /camera/frame/);
    # handing them to the bridge would ack them without a photo ever taken.
    pending = list(device.commands.filter(status="pending")
                   .exclude(kind__in=RobotCommand.CAMERA_KINDS)
                   .order_by("created_at")[:20])
    out = [{"command_id": c.id, "kind": c.kind, "payload": c.payload} for c in pending]
    RobotCommand.objects.filter(id__in=[c.id for c in pending]).update(
        status="sent", sent_at=timezone.now(),
    )
    return Response({"commands": out})


@_robot_endpoint
def commands_ack(request):
    """Device confirms a command finished (with optional result)."""
    device, err = _device_or_401(request)
    if err:
        return err
    cmd = RobotCommand.objects.filter(
        pk=request.data.get("command_id"), device=device,
    ).first()
    if cmd is None:
        return Response({"detail": "unknown command"}, status=status.HTTP_404_NOT_FOUND)
    ok = str(request.data.get("ok", "1")).lower() in ("1", "true", "yes")
    cmd.status = "done" if ok else "failed"
    cmd.done_at = timezone.now()
    cmd.result = request.data.get("result", {}) or {}
    cmd.save(update_fields=["status", "done_at", "result"])
    # A 'page' command that completed marks its RobotPageCall announced.
    if cmd.kind == "page" and ok:
        from .models import RobotPageCall
        page_id = (cmd.payload or {}).get("page_id")
        if page_id:
            RobotPageCall.objects.filter(pk=page_id, device=device).update(
                status="announced", announced_at=timezone.now(),
            )
    return Response({"ok": True})


@_robot_endpoint
def look(request):
    """Face the speaker: turn the head toward a horizontal `offset` ∈ [-1,1].

    The ESP32-CAM computes the offset from the detected face's position in frame
    and calls this; the head pans to center them. No auth beyond the device
    token — turning to look at someone is harmless.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    cmd = services.look_at(device, request.data.get("offset", 0),
                           issued_by=None)
    return Response({"ok": True, "turned": bool(cmd),
                     "frame": cmd.serial_frame() if cmd else None})


@_robot_endpoint
def sync_pull(request):
    """Download a RETAIL-ONLY offline catalog so the robot works with no net."""
    device, err = _device_or_401(request)
    if err:
        return err
    catalog = services.offline_catalog(device.branch)
    # The name it answers to, so «يا موس» is recognized with the net down too.
    catalog["wake"] = {"name": getattr(device, "wake_name", "") or "موس",
                       "aliases": list(getattr(device, "wake_aliases", None) or [])}
    # 🐛 [FIX]: الروبوت بيسأل كل دقيقة ([robot] answer_here): نفس الكتالوج كان بينزل كله
    #    على خط المحل كل مرة. دلوقتي لو مفيش جديد بيرجع 304 فاضي.
    tag = services.catalog_tag(catalog)
    # Caddy's compression can send the tag back as W/"…" or "…-gzip": the digest is what counts.
    if tag.strip('"') in request.headers.get("If-None-Match", ""):
        return Response(status=status.HTTP_304_NOT_MODIFIED, headers={"ETag": tag})
    return Response(catalog, headers={"ETag": tag})


@_robot_endpoint
def sync_push(request):
    """Replay queued offline events when the net returns (idempotent)."""
    device, err = _device_or_401(request)
    if err:
        return err
    result = services.apply_offline_events(device, request.data.get("events") or [])
    return Response(result)


@_robot_endpoint
def enroll_capture(request):
    """ESP32-CAM capture during a staff face-enrollment round.

    No face auth: at first install nobody can be recognized yet. It only acts
    while an owner-started round is active on THIS device, and only for the
    employee whose name the robot just called.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    image = request.FILES.get("image")
    if image is None:
        return Response({"detail": "no image"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(enrollment.capture(device, image.read()))


@_robot_endpoint
def teach(request):
    """Teach the robot — the human half of its learning loop. Requires face auth.

    Body (one of):
      * `scan_id` + `part_number`: "this scan was really THAT part" — weakens
        the wrong guess, learns the right code/label/look, fixes the scan.
      * `alias` + `part_number`: "when someone says <alias>, they mean THAT
        part" — shop slang the voice assistant then understands in sentences.
    """
    device, err = _device_or_401(request)
    if err:
        return err
    employee, perr = _require_permission(request, device, "teach")
    if perr:
        return perr

    product = services.find_product((request.data.get("part_number") or "").strip())
    if product is None:
        return Response({"detail": "القطعة غير موجودة."}, status=status.HTTP_404_NOT_FOUND)

    scan_id = request.data.get("scan_id")
    if scan_id:
        ev = RobotScanEvent.objects.filter(pk=scan_id, device=device).first()
        if ev is None:
            return Response({"detail": "المسح غير موجود."}, status=status.HTTP_404_NOT_FOUND)
        result = services.teach_scan_correction(ev, product=product, employee=employee)
        result["reply"] = f"تمام، اتعلمت إن القطعة دي {product.name}."
        return Response(result)

    alias = (request.data.get("alias") or "").strip()
    if len(alias) < 3:
        return Response({"detail": "ابعت scan_id أو alias (٣ حروف على الأقل)."},
                        status=status.HTTP_400_BAD_REQUEST)
    services.learn_from_confirmation(
        product=product, label=alias, employee=employee, details={"source": "teach"},
    )
    return Response({
        "ok": True, "alias": alias, "product_id": product.id, "name": product.name,
        "reply": f"تمام، اتعلمت إن «{alias}» يعني {product.name}.",
    })


@_robot_endpoint
def kiosk_part(request):
    """Kiosk: stock + RETAIL price + shelf for a part (`q`)."""
    device, err = _device_or_401(request)
    if err:
        return err
    from . import kiosk
    q = request.query_params.get("q") or request.data.get("q") or ""
    return Response(kiosk.part_info(q, device.branch))


@_kiosk_lookup_endpoint
def kiosk_customer(request):
    """Kiosk: first name + loyalty for a phone (no balance, no history)."""
    device, err = _device_or_401(request)
    if err:
        return err
    from . import kiosk
    phone = request.query_params.get("phone") or request.data.get("phone") or ""
    return Response(kiosk.customer_brief(phone))


@_kiosk_lookup_endpoint
def kiosk_return_check(request):
    """Kiosk: return/warranty eligibility by `invoice_number` or `phone`."""
    device, err = _device_or_401(request)
    if err:
        return err
    from . import kiosk
    get = lambda k: request.query_params.get(k) or request.data.get(k) or ""  # noqa: E731
    return Response(kiosk.return_check(get("invoice_number"), get("phone")))


@_robot_endpoint
def procurement_signals(request):
    """Open low-stock signals for the Procurement Agent to build the manifest."""
    device, err = _device_or_401(request)
    if err:
        return err
    signals = ProcurementSignal.objects.filter(
        branch=device.branch, status="open",
    ).select_related("product")[:100]
    return Response({
        "signals": [
            {
                "id": s.id,
                "product": s.product.name,
                "part_number": s.product.part_number,
                "on_hand": s.quantity_on_hand,
                "suggested_qty": s.suggested_reorder_qty,
                "manifest": s.manifest_payload,
            }
            for s in signals
        ]
    })
