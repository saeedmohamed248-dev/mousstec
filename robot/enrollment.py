"""
robot/enrollment.py — the robot enrolls staff faces itself, one by one, by name.

Right after the robot is installed nobody has a face on file, so nobody can be
recognized yet. The owner starts an enrollment round from the dashboard; the
robot then:

  1. announces the first employee by name ("أحمد، اتفضل قف قدام الكاميرا…"),
  2. tells the camera (in its /camera/frame/ reply) to send captures,
  3. keeps only captures with exactly one face that agree with each other,
  4. after SAMPLES_NEEDED good samples stores the averaged embedding on
     `hr.Employee.face_encoding` and says "تمام يا أحمد، اتسجلت",
  5. calls the next name. Nobody showing up after a few calls is skipped.

Safety rails: it refuses to start without a real face model (the fallback can't
tell people apart), a sample that doesn't match the ones before it is dropped
(someone else stepped in), and a face that already belongs to another
employee is not enrolled under a second name.
"""

from __future__ import annotations

from django.utils import timezone

SAMPLES_NEEDED = 3
# Call the name again if nobody has been captured for this long…
REANNOUNCE_SECONDS = 30
# …and skip the employee after this many calls (≈2 minutes).
MAX_ANNOUNCEMENTS = 4


class EnrollmentUnavailable(Exception):
    """No real face model installed — enrolling would store useless vectors."""


def _say(device, text, user=None):
    from .models import RobotCommand
    RobotCommand.objects.create(device=device, kind="say", issued_by=user,
                                payload={"text": text})


def _active_employees():
    from hr.models import Employee
    qs = Employee.objects.all()
    try:
        Employee._meta.get_field("user")
        from django.db.models import Q
        qs = qs.filter(Q(user__isnull=True) | Q(user__is_active=True))
    except Exception:
        pass
    return qs.order_by("name")


def active_session(device):
    from .models import RobotFaceEnrollment
    return (RobotFaceEnrollment.objects
            .filter(device=device, status="active")
            .order_by("-created_at").first())


def start(device, *, employee_ids=None, only_missing=True, user=None):
    """Open a new round for `device` and call the first name.

    `employee_ids` limits it to those people (in that order); otherwise every
    active employee — only those with no face on file when `only_missing`.
    Any earlier active round on this device is cancelled.
    """
    from . import security
    from .models import RobotFaceEnrollment

    if not security.matching_available():
        raise EnrollmentUnavailable(
            "مفيش موديل وجه حقيقي متثبّت (face_recognition) — التسجيل مش هيفرّق بين الناس."
        )

    RobotFaceEnrollment.objects.filter(device=device, status="active").update(
        status="cancelled", finished_at=timezone.now(),
    )

    employees = list(_active_employees())
    if employee_ids:
        wanted = [int(i) for i in employee_ids]
        by_id = {e.pk: e for e in employees}
        employees = [by_id[i] for i in wanted if i in by_id]
    elif only_missing:
        employees = [e for e in employees if not e.face_encoding]

    session = RobotFaceEnrollment.objects.create(
        device=device, created_by=user,
        entries=[{"employee_id": e.pk, "name": e.name, "status": "pending",
                  "samples": [], "announce_count": 0} for e in employees],
    )
    if not employees:
        _finish(session, user=user)
        return session
    _say(device, f"أهلاً بيكم! هسجّل بصمات وش {len(employees)} موظف واحد واحد. "
                 "لما أنادي اسمك اتفضل قف قدام الكاميرا.", user)
    _advance(session, user=user)
    return session


def _announce(session, entry, user=None):
    entry["announce_count"] = int(entry.get("announce_count", 0)) + 1
    entry["announced_at"] = timezone.now().isoformat()
    again = "تاني " if entry["announce_count"] > 1 else ""
    _say(session.device,
         f"{entry['name']}، {again}اتفضل قف قدام الكاميرا وبص لها لحد ما أقولك خلاص.",
         user)


def _advance(session, user=None):
    """Make the next pending employee current (and call them), or finish."""
    for entry in session.entries:
        if entry.get("status") == "pending":
            entry["status"] = "current"
            _announce(session, entry, user)
            session.save(update_fields=["entries"])
            return entry
    _finish(session, user=user)
    return None


def _finish(session, user=None):
    done = [e for e in session.entries if e.get("status") == "done"]
    skipped = [e for e in session.entries if e.get("status") == "skipped"]
    session.status = "done"
    session.finished_at = timezone.now()
    session.save(update_fields=["entries", "status", "finished_at"])
    msg = f"خلصت تسجيل البصمات: {len(done)} اتسجلوا."
    if skipped:
        msg += " ماسجّلتش: " + "، ".join(e["name"] for e in skipped[:6]) + "."
    _say(session.device, msg, user)


def skip_current(session, *, note="مش موجود", user=None):
    """Skip whoever is being called now and call the next one."""
    _i, entry = session.current()
    if entry is None:
        return None
    entry["status"] = "skipped"
    entry["note"] = note
    entry["samples"] = []
    _advance(session, user=user)
    return entry


def cancel(session, user=None):
    session.status = "cancelled"
    session.finished_at = timezone.now()
    session.save(update_fields=["status", "finished_at"])
    _say(session.device, "تمام، وقفت تسجيل البصمات.", user)


def camera_prompt(device):
    """What the camera should do about enrollment right now (or None).

    Called from /camera/frame/: re-calls the name when nobody has shown up for
    a while and skips them after MAX_ANNOUNCEMENTS calls.
    """
    session = active_session(device)
    if session is None:
        return None
    _i, entry = session.current()
    if entry is None:
        _advance(session)
        _i, entry = session.current()
        if entry is None:
            return None
    last = entry.get("announced_at")
    if last:
        from django.utils.dateparse import parse_datetime
        ts = parse_datetime(last)
        if ts and (timezone.now() - ts).total_seconds() > REANNOUNCE_SECONDS:
            if int(entry.get("announce_count", 0)) >= MAX_ANNOUNCEMENTS:
                skip_current(session, note="ماظهرش قدام الكاميرا")
                _i, entry = session.current()
                if entry is None:
                    return None
            else:
                _announce(session, entry)
                session.save(update_fields=["entries"])
    return {
        "active": True,
        "session_id": session.pk,
        "employee_id": entry["employee_id"],
        "name": entry["name"],
        "samples": len(entry.get("samples") or []),
        "needed": SAMPLES_NEEDED,
    }


def _average(vectors):
    n = len(vectors)
    return [sum(v[i] for v in vectors) / n for i in range(len(vectors[0]))]


def capture(device, image_bytes: bytes) -> dict:
    """Take one camera capture for the employee being enrolled now.

    Returns {"status": ..., "say"?: ...}. Statuses: idle, no_face,
    multiple_faces, inconsistent, sample_ok, duplicate, enrolled, unavailable.
    """
    from . import faces, security
    from hr.models import Employee

    session = active_session(device)
    if session is None:
        return {"status": "idle"}
    _i, entry = session.current()
    if entry is None:
        return {"status": "idle"}

    emb, reason = faces.extract_single_face(image_bytes)
    if emb is None:
        out = {"status": reason, "name": entry["name"]}
        if reason == "multiple_faces":
            out["say"] = f"فيه أكتر من حد قدام الكاميرا — {entry['name']} لوحده لو سمحت."
            _say(device, out["say"])
        return out

    threshold = security._threshold()
    samples = entry.setdefault("samples", [])
    if samples and security.compare_embeddings(emb, samples[0]) < threshold:
        # Not the same person as the first sample — someone else stepped in.
        return {"status": "inconsistent", "name": entry["name"]}
    samples.append(emb)

    if len(samples) < SAMPLES_NEEDED:
        session.save(update_fields=["entries"])
        return {"status": "sample_ok", "name": entry["name"],
                "samples": len(samples), "needed": SAMPLES_NEEDED}

    final = _average(samples)
    # The same face must not end up on two employees: a wrong name here would
    # let one person clock in and authorize sales as someone else.
    for other in Employee.objects.exclude(pk=entry["employee_id"]).exclude(
            face_encoding__isnull=True):
        if other.face_encoding and \
                security.compare_embeddings(final, other.face_encoding) >= threshold:
            entry["status"] = "skipped"
            entry["note"] = f"الوش شبه بصمة {other.name} المسجلة"
            entry["samples"] = []
            say = (f"الوش ده شبه بصمة {other.name} المسجلة قبل كده، "
                   f"فمش هسجّله باسم {entry['name']}. المدير يراجعها من اللوحة.")
            _say(device, say)
            _advance(session)
            return {"status": "duplicate", "name": entry["name"], "say": say}

    employee = Employee.objects.filter(pk=entry["employee_id"]).first()
    if employee is None:
        return {"status": "idle"}
    employee.face_encoding = final
    employee.save(update_fields=["face_encoding"])
    entry["status"] = "done"
    entry["samples"] = []  # the average is stored on the employee; drop raw samples
    entry["enrolled_at"] = timezone.now().isoformat()
    # Keep the photo it was enrolled from, so the owner can check on the
    # dashboard that the face under each name is really that person.
    try:
        from django.core.files.base import ContentFile
        from .models import RobotSnapshot
        snap = RobotSnapshot(device=device, reason="enroll")
        snap.image.save(f"enroll_{employee.pk}.jpg", ContentFile(image_bytes), save=True)
        entry["snapshot_id"] = snap.pk
    except Exception:
        pass
    say = f"تمام يا {entry['name']}، بصمتك اتسجلت. شكراً!"
    _say(device, say)
    _advance(session)
    return {"status": "enrolled", "name": entry["name"], "employee_id": employee.pk,
            "say": say}
