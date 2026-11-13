"""A number said the Egyptian way, from clips the voice bank keeps: «ألف وميتين وخمسين».

With the net down the robot has no voice but its clips — the fixed sentences, each part's
name and price. An amount it must read back before it's written («مصروف 250 جنيه، أسجله؟»)
is none of those, so it's joined from these: the ones, the tens, the hundreds, the
thousands, each also with «و» before it. `words(250)` → ["ميتين", "وخمسين"]; `clips()` is
every clip that can come up, for the bank to fetch while the net is up.
"""

ONES = ["", "واحد", "اتنين", "تلاتة", "أربعة", "خمسة", "ستة", "سبعة", "تمانية", "تسعة", "عشرة",
        "حداشر", "اتناشر", "تلاتاشر", "أربعتاشر", "خمستاشر", "ستاشر", "سبعتاشر", "تمنتاشر", "تسعتاشر"]
TENS = ["", "", "عشرين", "تلاتين", "أربعين", "خمسين", "ستين", "سبعين", "تمانين", "تسعين"]
HUNDREDS = ["", "مية", "ميتين", "تلتمية", "ربعمية", "خمسمية", "ستمية", "سبعمية", "تمنمية", "تسعمية"]
THOUSANDS = ["", "ألف", "ألفين", "تلات تلاف", "أربع تلاف", "خمس تلاف", "ست تلاف", "سبع تلاف",
             "تمن تلاف", "تسع تلاف", "عشر تلاف"]
THOUSAND = "ألف"                      # «حداشر ألف», «مية وخمسين ألف»
ZERO = "صفر"
MOST = 999_999


def _below_thousand(n: int) -> list:
    parts = []
    if n >= 100:
        parts.append(HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        if n % 10:
            parts.append(ONES[n % 10])           # «خمسة وعشرين»: the one first
        parts.append(TENS[n // 10])
    elif n:
        parts.append(ONES[n])
    return parts


def words(n: int) -> list:
    """`n` (0…MOST) as the clips that say it, each after the first with its «و»."""
    n = int(n)
    if not 0 <= n <= MOST:
        raise ValueError(f"{n}: only 0…{MOST} is said from clips")
    if n == 0:
        return [ZERO]
    parts, thousands, rest = [], n // 1000, n % 1000
    if 1 <= thousands <= 10:
        parts.append(THOUSANDS[thousands])
    elif thousands:
        parts += _below_thousand(thousands) + [THOUSAND]
    parts += _below_thousand(rest)
    return [parts[0]] + ["و" + p for p in parts[1:]]


def text(n: int) -> str:
    return " ".join(words(n))


def clips() -> list:
    """Every clip `words` can return."""
    plain = [w for w in ONES + TENS + HUNDREDS + THOUSANDS if w] + [THOUSAND]
    return list(dict.fromkeys([ZERO] + plain + ["و" + w for w in plain]))
