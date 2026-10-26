"""python -m brain doctor — «🩺 فحص المخ»: everything the robot needs, checked
in order, each with what to do when it isn't right.

What shops met before, found in a minute instead of a day: brain.ini unzipped
to C:\\ instead of C:\\mouss-brain, «model = small» (Whisper) on an old
computer, the key rotated on the dashboard, Gemini out of quota, a brain older
than the server's. Nothing is changed; it only looks (and asks the server).
"""

from pathlib import Path

from . import VERSION
from .config import HERE, HOME
from .server import ServerDown, Unauthorized


def check(cfg, server=None, *, log=print) -> int:
    """Look at everything; the number of problems found (0: all good)."""
    found = []

    def good(line):
        log(f"[DOCTOR] ✔ {line}")

    def bad(line, fix):
        found.append(line)
        log(f"[DOCTOR] ✘ {line}\n[DOCTOR]   → {fix}")

    def tip(line):
        log(f"[DOCTOR] • {line}")

    log(f"[DOCTOR] Mouss Tec {VERSION}")
    folder = HERE.parent                         # where the brain folder is (C:\mouss-brain)
    if cfg.source is None:
        bad("no brain.ini found",
            f"download the brain WITH its key from the robot's page on the dashboard and unzip it into {folder}")
    else:
        good(f"brain.ini: {cfg.source}")
        if Path(cfg.source).resolve().parent not in (folder.resolve(), HERE.resolve(), Path(HOME).resolve()):
            tip(f"brain.ini isn't next to the brain folder ({folder}): an update unzipped there won't see it")
    if Path(folder.anchor) == folder:
        bad(f"the brain is at the top of the drive ({HERE})",
            "put it in its own folder: make C:\\mouss-brain and unzip the brain (and brain.ini) in it")
    missing = cfg.missing()
    if missing:
        bad(f"brain.ini has no {', '.join(missing)} under [server]",
            "download the brain WITH its key from the robot's page («⬇️ تحديث المخ» after «تغيير المفتاح»)")
        return len(found)

    if server is None:
        from .server import Server
        server = Server(cfg.api_base, cfg.token)
    try:
        code = server.heartbeat(VERSION)
    except Unauthorized:
        bad("the server refused this robot's key (401)",
            "the key was changed, or api_base is another shop's: download the brain again with its key")
        return len(found)
    except ServerDown as exc:
        bad(f"no answer from the server ({_why(exc)})", f"check the internet, and api_base = {cfg.api_base}")
        code = 0
    if code == 200:
        good(f"the server answers, and knows this robot ({cfg.api_base})")
        _services(server, good, bad, tip)
    elif code:
        bad(f"the server answered {code}", f"check api_base = {cfg.api_base}")

    _ears(cfg, good, tip)
    _offline(cfg, good, tip)
    if cfg.counter_port:
        good(f"the counter page: http://<this computer's address>:{cfg.counter_port}")
    log(f"[DOCTOR] {'all good' if not found else f'{len(found)} to fix'}")
    return len(found)


def _why(exc) -> str:
    """A network error in a few words, not urllib3's paragraph."""
    text = str(exc)
    for needle, said in (("refused", "connection refused"), ("timed out", "timed out"),
                         ("getaddrinfo", "the address isn't found"), ("not known", "the address isn't found"),
                         ("certificate", "a certificate problem — is the computer's date right?")):
        if needle in text.lower():
            return said
    return text[:120]


def _services(server, good, bad, tip):
    from .loop import _service_said, _version

    try:
        status = server.status()
    except (ServerDown, Unauthorized):
        return
    if not isinstance(status, dict) or not status:
        tip("the server doesn't say how its services are (not updated yet)")
        return
    for s in status.get("services") or []:
        if not isinstance(s, dict):
            continue
        if s.get("ok") is False:
            bad(f"server: {_service_said(s)}", s.get("remedy") or "see «صحة الخدمات» on the robot's dashboard")
        elif s.get("ready"):
            good(f"server: {_service_said(s)}")
        else:
            tip(f"server: {_service_said(s)} (not set up there)")
    shipped = str(status.get("brain") or "")
    if shipped and _version(shipped) > _version(VERSION):
        bad(f"this brain is {VERSION}, the server has {shipped}",
            "«⬇️ تحديث المخ» on the robot's dashboard, unzipped over this folder (brain.ini stays)")


def _ears(cfg, good, tip):
    from .vosk_ears import MODELS, is_vosk

    model = (cfg.model or "").strip()
    if is_vosk(model):
        name = MODELS.get(model.lower(), model)
        if (Path(cfg.home) / "models" / name).is_dir():
            good(f"ears: Vosk ({name}), on this computer")
        else:
            tip(f"ears: Vosk — its model ({name}) downloads on the first start, a few minutes")
    elif model.lower() in ("off", "none", "0", "no", ""):
        tip("ears: off — the server hears everything (it needs the internet for every word)")
    else:
        tip(f"ears: Whisper «{model}» — on an old computer that's 10 s and more a sentence: "
            "[ears] model = vosk in brain.ini hears in about a second")
    if cfg.teach:
        good("teachers: " + ("every question goes to a teacher while the net is up ([robot] teach = yes)"
                             if cfg.teach_all else "the questions it isn't sure of go to a teacher"))


def _offline(cfg, good, tip):
    from .knowledge import Knowledge
    from .voicebank import VoiceBank

    knowledge = Knowledge.load(Path(cfg.home) / "catalog.json")
    if not knowledge.parts:
        tip("offline: no catalog on this computer yet — it comes from the server on the first start")
        return
    good(f"offline: {len(knowledge.parts)} parts known (catalog of {knowledge.generated_at or 'unknown date'})")
    lessons, sentences = len(knowledge.heard_fixes), len(knowledge.heard_sentences)
    if lessons or sentences:
        good(f"offline: learned from the teachers — {lessons} word(s), {sentences} whole sentence(s)")
    have, total = VoiceBank(Path(cfg.home) / "voice" / "clips").coverage(knowledge.clip_texts())
    if total and have < total:
        tip(f"offline voice: {have} of {total} clips — the rest come from the server while the robot runs")
    elif total:
        good(f"offline voice: all {total} clips")
