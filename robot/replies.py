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

# What the page suggests: the questions every shop gets, as they're asked in
# Egypt. The answer is the owner's to write — a suggestion is never said. A
# reply the owner wrote also answers its topic's questions here (for_branch),
# so «بتقفلوا امتى؟» finds the hours however they were written.
SUGGESTED = [
    ("hours", ["مواعيدكم", "مواعيد الشغل", "مواعيد العمل", "بتفتحوا امتى", "بتفتحوا الساعه كام",
               "بتقفلوا امتى", "بتقفلوا الساعه كام", "فاتحين لحد امتى", "فاتحين لحد الساعه كام",
               "فاتحين النهارده", "فاتحين دلوقتي", "لسه فاتحين", "فاتحين بكره", "الجمعه فاتحين",
               "بتشتغلوا يوم الجمعه", "اجازتكم يوم ايه", "بتقفلوا يوم ايه", "فاتحين في العيد",
               "مواعيد رمضان", "فاتحين في رمضان", "بتفتحوا الصبح امتى", "اخر ميعاد كام",
               "شغالين للساعه كام"]),
    ("address", ["عنوانكم", "العنوان فين", "المحل فين", "انتو فين", "الفرع فين", "اجيلكم ازاي",
                 "اوصلكم ازاي", "مكانكم فين", "انتو فين بالظبط", "العنوان بالتفصيل", "ابعتلي اللوكيشن",
                 "اللوكيشن", "قريبين من ايه", "فيه ركنه", "اركن فين", "عندكم فروع", "فروعكم فين",
                 "اقرب فرع ليا"]),
    ("phone", ["رقم تليفونكم", "رقم التليفون", "نمره التليفون", "رقم الواتساب", "تليفونكم",
               "اكلمكم ازاي", "رقمكم", "رقم المحل", "الرقم كام", "عندكم واتساب", "رقم الواتس",
               "ابعتلكم على الواتس", "صفحتكم على الفيسبوك", "عندكم صفحه"]),
    ("payment", ["ادفع ازاي", "الدفع ازاي", "بتقبلوا فيزا", "ادفع بالفيزا", "فودافون كاش", "انستاباي",
                 "فيه تقسيط", "بتاخدوا فيزا", "فيه دفع بالكارت", "ينفع ادفع فيزا", "ينفع ادفع فودافون كاش",
                 "بتقبلوا انستاباي", "بتقسطوا", "ينفع اقسط", "كاش بس", "فاليو", "فيه فاليو", "سمبل",
                 "ادفع كام مقدم", "ينفع ادفع تحويل", "ادفع فين", "الكاشير فين"]),
    ("warranty", ["فيه ضمان", "الضمان كام", "الضمان قد ايه", "الضمان بتاعكم", "الضمان كام شهر",
                  "الحاجات عليها ضمان", "لو القطعه باظت", "لو باظت ارجعها", "الضمان بيغطي ايه",
                  "الضمان بالفاتوره", "بتضمنوا القطع"]),
    ("returns", ["ينفع ارجع", "ينفع استبدل", "الاسترجاع", "الاستبدال", "ارجع القطعه",
                 "لو القطعه مركبتش", "ينفع ارجعها", "لو طلعت غلط", "الاستبدال خلال كام يوم",
                 "ينفع ابدل", "ترجعولي فلوسي", "لو مش مناسبه", "لو مركبتش ارجعها"]),
    ("delivery", ["فيه توصيل", "عندكم توصيل", "بتوصلوا", "الشحن", "بتشحنوا للمحافظات",
                  "بتبعتوا للبيت", "بتوصلوا لحد البيت", "التوصيل بكام", "فيه شحن", "بتشحنوا",
                  "التوصيل بياخد قد ايه", "توصيل مجاني", "بتبعتوا مع مندوب", "ابعتلي مندوب",
                  "بتشحنوا لاسكندريه"]),
    ("cars", ["بتشتغلوا على عربيات ايه", "عندكم قطع ايه", "عندكم قطع لانهي عربيات", "عندكم قطع بي ام",
              "عندكم قطع بي ام دبليو", "عندكم قطع ميني", "عندكم قطع مرسيدس", "بتبيعوا قطع ايه",
              "متخصصين في ايه", "عندكم قطع لعربيتي", "بتبيعوا قطع غيار ايه", "شغالين على انهي ماركات",
              "عندكم كل الموديلات"]),
    ("original", ["عندكم اصلي", "الحاجات اصلي", "القطع اصلي ولا تقليد", "عندكم وكاله", "بتبيعوا تقليد",
                  "عندكم تجاري", "عندكم استيراد", "عندكم مستعمل", "القطع جديده ولا مستعمله",
                  "بتجيبوا القطع منين", "عندكم صيني", "الماركات اللي عندكم"]),
    ("install", ["بتركبوا", "عندكم تركيب", "فيه تركيب", "ممكن تركبهالي", "عندكم ورشه", "عندكم فني",
                 "التركيب بكام", "التركيب ببلاش", "التركيب عليكم", "تركبوها", "فيه ميكانيكي",
                 "التركيب بياخد قد ايه", "ممكن اركبها عندكم"]),
    ("discount", ["فيه خصم", "اخر كلام", "اخر سعر", "السعر نهائي", "ممكن تنزل", "نزل شويه",
                  "هتعملي خصم", "ينفع تخصملي", "مفيش خصم", "فيه عروض", "عندكم عروض", "فيه تخفيضات",
                  "ده غالي", "غاليه اوي", "السعر غالي", "ارخص من كده"]),
    ("order", ["ممكن تطلبهالي", "ممكن تجيبهالي", "تقدر تجيبها", "تيجي امتى", "هتيجي امتى",
               "بتطلبوا قطع", "بتجيبوا من بره", "ممكن احجزها", "احجز قطعه", "فيه عربون",
               "اطلبها ازاي", "بتستوردوا", "القطعه مش موجوده اعمل ايه", "ممكن توفرهالي",
               "توصل امتى"]),
    ("wholesale", ["بتبيعوا جمله", "سعر الجمله", "انا تاجر", "عندكم جمله", "اسعار التجار",
                   "بتتعاملوا مع تجار", "خصم تجار", "بتبيعوا للورش", "اسعار الورش"]),
    ("invoice", ["فيه فاتوره", "هاخد فاتوره", "عايز فاتوره", "فاتوره ضريبيه", "بتعملوا فاتوره",
                 "ممكن ايصال", "الفاتوره باسم الشركه", "السعر شامل الضريبه", "فيه ضريبه"]),
    ("checkup", ["بتعملوا فحص", "فحص كمبيوتر", "عندكم كمبيوتر للعربيه", "بتشوفوا العطل",
                 "بتغيروا زيت", "عندكم صيانه", "بتصلحوا عربيات", "تشخيص اعطال", "بتقروا الاعطال",
                 "عندكم سكانر", "الفحص بكام", "ممكن تشوفلي العربيه"]),
]
_BUILT_IN = dict(SUGGESTED)
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
    return [{"id": r.pk, "topic": r.topic,
             "questions": list(dict.fromkeys(matcher.questions(r.questions) + _BUILT_IN.get(r.topic, []))),
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
