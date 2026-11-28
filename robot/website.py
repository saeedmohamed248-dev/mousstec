"""robot/website.py — «📥 هات المعلومات من الموقع»: the shop's own website, read for what the robot answers.

The warranty, returns, delivery, paying, the hours, the address: on the shop's site already, word for
word what a customer is told online — and what the robot made up when nobody had written it (from the
shop's log: «قطع الاستيراد عليها ضمان شهر… تحب أحجزلك ولا شحن؟»). Here the site's pages are read
(its home page and the pages it links to about those), Gemini writes each topic found as the robot
says it — only what the pages say — and each becomes a «🗨️ الردود الجاهزة» reply, OFF: the owner
reads it, fixes it, turns it on. Then the robot says it, and «مخ موس» takes it with its catalog (said
with the net down too).

Prices and stock never come from the site: the system is where they're from, and newer.

Once a week (robot.tasks.refresh_website_replies) the site is read again: a topic whose words changed
on the site comes back as a draft to review; a reply the owner turned on is never touched.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("mouss_tec_core")

TIMEOUT_S = 10
MOST_PAGES = 8
PAGE_CHARS = 6000
ALL_CHARS = 30000
URL_KEY = "robot:website:url"
# The pages worth reading: their address or the link's words say what they're about.
_WORTH = re.compile(
    r"polic|shipping|deliver|return|refund|warrant|guarant|about|contact|faq|terms|payment|store-?info|"
    r"سياس|شحن|توصيل|استرجاع|استبدال|ضمان|من-?نحن|تواصل|اتصل|الأسئلة|الدفع|الشروط", re.I)
_SKIP = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|css|js)(\?|$)|/cart|/checkout|/account|/login", re.I)

PROMPT = """\
دي نصوص صفحات الموقع بتاع محل قطع غيار. طلّع منها معلومات المحل اللي الزبون بيسأل عنها، كل موضوع لوحده.
المواضيع (topic): hours المواعيد، address العنوان، phone التليفون، payment الدفع، warranty الضمان، returns الاسترجاع
والاستبدال، delivery التوصيل والشحن، cars العربيات اللي بيشتغلوا عليها، original أصلي ولا تقليد، install التركيب،
order طلب قطعة مش موجودة، wholesale الجملة والتجار، invoice الفاتورة، other حاجة تانية مهمة.
لكل موضوع لقيته مكتوب في النصوص: "answer" الرد زي ما روبوت المحل هيقوله بالعامية المصرية، جملة أو اتنين، من غير
إيموجي ولا قوايم، وفيه بس اللي مكتوب في الصفحات (أرقام ومدد ومصاريف بالظبط زي ما هي) — متخترعش ومتكملش من عندك؛
و"questions" من ٣ لـ ٦ أسئلة زي ما الزبون المصري بيسألها؛ و"source" عنوان الصفحة اللي لقيته فيها.
متطلّعش أسعار قطع ولا كميات. موضوع مش مكتوب في النصوص متكتبهوش.
رجّع JSON بس: {"replies": [{"topic": "...", "answer": "...", "questions": ["..."], "source": "..."}]}
"""


class _Text(HTMLParser):
    """A page's words and its links (no scripts, styles or menus' markup)."""

    def __init__(self):
        super().__init__()
        self.words, self.links, self._skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href:
                self.links.append(href)
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.words.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.words.append(data.strip())


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    return url if parsed.scheme in ("http", "https") and parsed.netloc else ""


def _get(url: str) -> Optional[_Text]:
    import requests

    try:
        resp = requests.get(url, timeout=TIMEOUT_S, headers={"User-Agent": "Mozilla/5.0 (shop robot)"})
    except requests.RequestException as exc:
        logger.warning("robot website: %s didn't answer: %s", url, exc)
        return None
    if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "html"):
        return None
    page = _Text()
    try:
        page.feed(resp.text)
    except Exception:
        return None
    return page


def pages(url: str) -> list:
    """[(address, text)] — the site's home page and the pages it links to about the shop's terms."""
    home = _clean_url(url)
    if not home:
        return []
    first = _get(home)
    if first is None:
        return []
    host = urlparse(home).netloc.lower().removeprefix("www.")
    wanted = []
    for href in first.links:
        link = urljoin(home, href).split("#")[0]
        if urlparse(link).netloc.lower().removeprefix("www.") != host or _SKIP.search(link):
            continue
        if _WORTH.search(link) and link not in wanted and link.rstrip("/") != home.rstrip("/"):
            wanted.append(link)
    out = [(home, _text(first))]
    for link in wanted[:MOST_PAGES - 1]:
        page = _get(link)
        if page is not None:
            out.append((link, _text(page)))
    return [(u, t) for u, t in out if t]


def _text(page: _Text) -> str:
    text = re.sub(r"[ \t]*\n[\s\n]*", "\n", " ".join(page.words))
    return re.sub(r"[ \t]+", " ", text).strip()[:PAGE_CHARS]


def extract(found: list) -> list:
    """[{"topic", "answer", "questions", "source"}] — what the pages say, as the robot says it."""
    from . import audio
    from .models import RobotReply
    from .replies import MOST_CHARS, MOST_QUESTIONS

    if not found:
        return []
    body, used = [], 0
    for url, text in found:
        piece = f"=== {url}\n{text}"
        if used + len(piece) > ALL_CHARS:
            piece = piece[:max(0, ALL_CHARS - used)]
        body.append(piece)
        used += len(piece)
    raw = audio.gemini_generate([{"text": PROMPT}, {"text": "\n\n".join(body)}], what="website",
                                timeout=40, budget=60, json_out=True)
    try:
        got = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip()))
    except ValueError:
        return []
    topics = dict(RobotReply.TOPIC)
    out, seen = [], set()
    for row in (got.get("replies") if isinstance(got, dict) else None) or []:
        if not isinstance(row, dict):
            continue
        topic = str(row.get("topic") or "other")
        topic = topic if topic in topics else "other"
        answer = " ".join(str(row.get("answer") or "").split())[:MOST_CHARS]
        questions = [" ".join(str(q).split()) for q in row.get("questions") or [] if str(q).strip()]
        if not answer or not questions or (topic != "other" and topic in seen):
            continue
        seen.add(topic)
        out.append({"topic": topic, "answer": answer, "questions": questions[:MOST_QUESTIONS],
                    "source": str(row.get("source") or "")[:200]})
    return out


def save(found: list) -> dict:
    """The site's replies as drafts (off): {"added", "changed", "kept"}. A topic the owner turned on
    («شغّال») is theirs: never touched. A draft from the site before is brought up to date."""
    from .models import RobotReply

    counts = {"added": 0, "changed": 0, "kept": 0}
    for row in found:
        mine = RobotReply.objects.filter(topic=row["topic"], branch__isnull=True)
        if row["topic"] != "other" and mine.filter(is_active=True).exists():
            counts["kept"] += 1
            continue
        draft = mine.filter(is_active=False, updated_by__isnull=True).first() if row["topic"] != "other" \
            else mine.filter(is_active=False, updated_by__isnull=True, answer=row["answer"]).first()
        if draft is not None and draft.answer == row["answer"]:
            continue
        reply = draft or RobotReply(topic=row["topic"])
        reply.questions = "\n".join(row["questions"])
        reply.answer = row["answer"]
        reply.is_active = False
        reply.updated_by = None                   # the site's, waiting for the owner
        reply.save()
        counts["changed" if draft is not None else "added"] += 1
    return counts


def remembered_url() -> str:
    import os

    from django.core.cache import cache

    try:
        return cache.get(URL_KEY) or os.getenv("ROBOT_WEBSITE_URL", "").strip()
    except Exception:
        return os.getenv("ROBOT_WEBSITE_URL", "").strip()


def read(url: str = "") -> dict:
    """The site read and its replies saved as drafts: {"pages", "added", "changed", "kept"}."""
    from django.core.cache import cache

    url = _clean_url(url) or _clean_url(remembered_url())
    if not url:
        return {"pages": 0, "added": 0, "changed": 0, "kept": 0, "error": "no_url"}
    try:
        cache.set(URL_KEY, url, None)             # the weekly reading reads it again
    except Exception:
        pass
    found = pages(url)
    if not found:
        return {"pages": 0, "added": 0, "changed": 0, "kept": 0, "error": "unreachable"}
    return {"pages": len(found), **save(extract(found))}
