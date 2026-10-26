"""robot/replies.py — «🗨️ الردود الجاهزة» on the server.

The owner writes them on the robot's page (robot/views_ui.replies): the ways
people ask, one per line, and what the robot says. Asked by voice, the reply
is said before the parts are looked in (robot/views._handle_voice) — and the
same replies go to «مخ موس» with its catalog, so it says them with the net
down too. The match is robot/brain/replies.py, the same on both.
"""

import re

from django.db.models import F, Q

from .brain import replies as matcher

# What the page suggests: the questions every shop gets, as they're asked.
# The answer is the owner's to write — a suggestion is never said.
SUGGESTED = [
    ("hours", ["مواعيدكم", "مواعيد الشغل", "بتفتحوا امتى", "بتقفلوا امتى", "بتقفلوا الساعه كام",
               "فاتحين لحد امتى", "فاتحين النهارده", "الجمعه فاتحين"]),
    ("address", ["عنوانكم", "العنوان فين", "المحل فين", "انتو فين", "الفرع فين", "اجيلكم ازاي",
                 "اوصلكم ازاي"]),
    ("phone", ["رقم تليفونكم", "رقم التليفون", "نمره التليفون", "رقم الواتساب", "تليفونكم",
               "اكلمكم ازاي"]),
    ("payment", ["ادفع ازاي", "الدفع ازاي", "بتقبلوا فيزا", "ادفع بالفيزا", "فودافون كاش", "انستاباي",
                 "فيه تقسيط"]),
    ("warranty", ["فيه ضمان", "الضمان كام", "الضمان قد ايه", "الضمان بتاعكم"]),
    ("returns", ["ينفع ارجع", "ينفع استبدل", "الاسترجاع", "الاستبدال", "ارجع القطعه",
                 "لو القطعه مركبتش"]),
    ("delivery", ["فيه توصيل", "عندكم توصيل", "بتوصلوا", "الشحن", "بتشحنوا للمحافظات",
                  "بتبعتوا للبيت"]),
]
# Said one by one with the branch named: its address, its phone.
PER_BRANCH = ("address", "phone")
MOST_QUESTIONS, MOST_CHARS = 30, 500

_PHONE = re.compile(r"(?<!\d)\d{8,}(?!\d)")


def as_said(answer: str) -> str:
    """The reply as the robot says it: a phone number digit by digit
    («0 1 0 …» → «صفر واحد صفر …»), never as one big number."""
    return _PHONE.sub(lambda m: " ".join(m.group()), " ".join((answer or "").split()))


def for_branch(branch) -> list:
    """The replies the robot at `branch` says — its branch's own first —
    as robot/brain/replies.match takes them (and the catalog carries them)."""
    from .models import RobotReply

    rows = RobotReply.objects.filter(is_active=True).exclude(answer="")
    rows = rows.filter(Q(branch=branch) | Q(branch__isnull=True)) if branch is not None \
        else rows.filter(branch__isnull=True)
    rows = sorted(rows, key=lambda r: (r.branch_id is None, r.topic, r.pk))
    return [{"id": r.pk, "topic": r.topic, "questions": matcher.questions(r.questions),
             "answer": as_said(r.answer)} for r in rows]


def answer(text: str, branch):
    """The reply asked for in `text` ({"id", "topic", "questions", "answer"}),
    or None. Not when the rest of what was said is a part: «فيه ضمان على
    الطرمبة؟» is about the pump."""
    from . import services
    from .models import RobotReply

    found = matcher.match(text, for_branch(branch))
    if found is None:
        return None
    reply, rest = found
    if rest:
        product, choices = services.match_part_in_sentence(rest)
        if product is not None or choices:
            return None
    RobotReply.objects.filter(pk=reply["id"]).update(hit_count=F("hit_count") + 1)
    return reply
