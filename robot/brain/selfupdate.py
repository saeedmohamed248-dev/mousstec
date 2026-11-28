"""«مخ موس» updates itself: the newer brain the server ships comes down,
goes in over this folder, and the robot starts it when nobody's talking
(loop.Brain.updated) — and goes back to the one before if it won't run.

Before, a new brain waited for someone at the shop's computer: «⬇️ تحديث المخ»
on the dashboard, unzipped over the folder. Now:

* only where something starts the robot again (loop.watched(): run_windows.bat
  robotpc, run_mac.sh watch, MOUSS_WATCH=1). Run by hand, a brain written
  under a running one would be half old, half new: the log says so instead;
* never the launchers (.bat/.sh): cmd and bash read them while they run, and
  one changed under its own loop can jump anywhere. A new one is said in the
  log — the dashboard's zip once, by hand;
* __init__.py goes in last: its VERSION is what tells a new brain is on disk,
  so it changes only once every other file is there;
* the brain before is kept (home/brain-before). A new one that stops TRIES
  times before it ran SETTLE_S is put back, and that version isn't taken
  again (home/brain-refused.txt) — the next one the server ships is;
* [robot] auto_update = no in brain.ini: never; the dashboard's zip, as before.
"""

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

BACKUP = "brain-before"
TRIAL = "brain-trial.json"
REFUSED = "brain-refused.txt"
TRIES = 3                    # stops of a new brain before it ran SETTLE_S: it's put back
SETTLE_S = 120
EVERY_S = 2 * 60             # how often the server is asked for a newer brain
LAUNCHERS = (".bat", ".sh")
_VERSION = re.compile(r'^VERSION\s*=\s*"([^"]+)"', re.M)


def version_in(init_py: str) -> str:
    found = _VERSION.search(init_py or "")
    return found.group(1) if found else ""


def unpack(data: bytes, version: str) -> dict:
    """{path inside brain/: bytes} of the server's zip — checked: it's whole,
    nothing in it goes outside the brain folder, and it's the `version` the
    server said. ValueError otherwise."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a zip: {exc}") from exc
    with zf:
        broken = zf.testzip()
        if broken:
            raise ValueError(f"damaged on the way: {broken}")
        files = {}
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or not name.startswith("brain/"):
                continue                 # the launchers and README beside the folder: not ours
            rel = name[len("brain/"):]
            parts = rel.split("/")
            if not rel or "\\" in rel or ":" in rel or any(p in ("", ".", "..") for p in parts):
                raise ValueError(f"a file outside the brain folder: {name}")
            files[rel] = zf.read(info)
    init = files.get("__init__.py", b"").decode("utf-8", "replace")
    if version_in(init) != version:
        raise ValueError(f"the zip is {version_in(init) or 'no brain'}, not {version}")
    return files


def _write(folder: Path, files: dict) -> list:
    """The changed files written over `folder`, __init__.py last; the
    launchers that changed are left as they are and named."""
    left = []
    order = sorted(files, key=lambda rel: rel == "__init__.py")
    for rel in order:
        dest = folder / rel
        body = files[rel]
        try:
            same = dest.read_bytes() == body
        except OSError:
            same = False
        if same:
            continue
        if dest.suffix in LAUNCHERS:
            left.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        new = dest.with_name(dest.name + ".new")
        new.write_bytes(body)
        os.replace(new, dest)            # a reader sees the old file or the new one, never half
    return left


def _read_trial(home: Path) -> dict:
    try:
        trial = json.loads((Path(home) / TRIAL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return trial if isinstance(trial, dict) else {}


def refused(home) -> set:
    try:
        return set((Path(home) / REFUSED).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def install(data: bytes, version: str, folder: Path, home: Path, now: str, log=print) -> bool:
    """The server's brain `version` (its zip, `data`) in over `folder` — the
    one running (`now`) kept to go back to. False when it didn't go in (the
    zip wasn't right, or a file couldn't be written: the brain before is put
    back then, whole)."""
    folder, home = Path(folder), Path(home)
    try:
        files = unpack(data, version)
    except ValueError as exc:
        log(f"[UPDATE] {version} not taken: {exc}")
        return False
    backup = home / BACKUP
    home.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(folder, backup, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.new", "brain.ini"))
    try:
        left = _write(folder, files)
    except OSError as exc:
        log(f"[UPDATE] {version} couldn't be written here ({exc}): {now} stays")
        restore(folder, home)
        return False
    (home / TRIAL).write_text(json.dumps({"version": version, "before": now, "starts": 0}), encoding="utf-8")
    log(f"[UPDATE] {version} came down from the server and is in this folder (this one is {now})")
    if left:
        log(f"[UPDATE] a new {' · '.join(left)} came with it — not written while it runs: "
            "«⬇️ تحديث المخ» on the dashboard, unzipped by hand, once")
    return True


def restore(folder: Path, home: Path) -> bool:
    """The brain before put back over `folder` (__init__.py last)."""
    backup = Path(home) / BACKUP
    if not (backup / "__init__.py").exists():
        return False
    files = {p.relative_to(backup).as_posix(): p.read_bytes() for p in backup.rglob("*") if p.is_file()}
    _write(Path(folder), files)
    return True


def started(folder: Path, home: Path, version: str, log=print) -> bool:
    """Called as the robot starts: a new brain on trial counts its starts —
    one that stopped TRIES times without settling is put back, and False
    says to stop now (the watcher starts the one before)."""
    trial = _read_trial(home)
    if not trial:
        return True
    path = Path(home) / TRIAL
    if trial.get("version") != version:      # put back, or replaced by hand: no trial any more
        path.unlink(missing_ok=True)
        return True
    starts = int(trial.get("starts") or 0) + 1
    if starts > TRIES:
        before = str(trial.get("before") or "the brain before")
        if restore(folder, home):
            with (Path(home) / REFUSED).open("a", encoding="utf-8") as f:
                f.write(version + "\n")
            path.unlink(missing_ok=True)
            log(f"[UPDATE] {version} stopped {TRIES} times without running: {before} is back, "
                f"and {version} won't be taken again")
            return False
        path.unlink(missing_ok=True)         # nothing to go back to: it keeps trying, as by hand
        return True
    path.write_text(json.dumps({**trial, "starts": starts}), encoding="utf-8")
    return True


def settled(home: Path, log=print) -> None:
    """A new brain ran SETTLE_S: it stays."""
    trial = _read_trial(home)
    if trial:
        (Path(home) / TRIAL).unlink(missing_ok=True)
        log(f"[UPDATE] {trial.get('version')} is running fine: it stays")


def takes(brain) -> bool:
    from .loop import watched

    return bool(getattr(brain.cfg, "auto_update", True)) and watched()


def newer(brain, shipped: str) -> None:
    """The server said it ships `shipped`, newer than this brain (loop.check_services)."""
    from . import VERSION
    from .loop import watched

    if takes(brain):
        check(brain, shipped)
        return
    brain.log(f"[UPDATE] a newer brain is on the server ({shipped}; this one is {VERSION}): "
              "«⬇️ تحديث المخ» on the robot's dashboard, unzipped over this folder"
              + (" — it then starts by itself" if watched() else
                 " — or start the robot with «watch» and it comes down by itself"))


def tick(brain) -> None:
    """Each round of the robot's background loop: a new brain that ran SETTLE_S
    stays, and the server is asked for a newer one every EVERY_S."""
    import time

    now = time.monotonic()
    since = brain.__dict__.setdefault("_running_since", now)
    if not brain.__dict__.get("_settled") and now - since > SETTLE_S:
        brain._settled = True
        settled(brain.cfg.home, log=brain.log)
    if brain.online and now - brain.__dict__.get("_update_asked", since) > EVERY_S:
        check(brain)


def check(brain, shipped: str = "") -> bool:
    """The server ships a newer brain (at start, then every EVERY_S): it comes
    down and goes in over this folder (Brain.updated then starts it when
    nobody's talking). Only where something starts the robot again, and
    never a brain put back before."""
    from . import VERSION
    from .loop import _version
    from .server import ServerDown, Unauthorized

    import time

    brain._update_asked = time.monotonic()
    if not (takes(brain) and brain.online):
        return False
    if not shipped:
        try:
            shipped = str((brain.monitor.status() or {}).get("brain") or "")
        except (ServerDown, Unauthorized):
            return False
    if not shipped or _version(shipped) <= _version(VERSION) or brain._on_disk()[0] == shipped:
        return False                             # nothing newer, or it's in already (updated() takes it)
    if shipped in refused(brain.cfg.home):
        if brain.__dict__.get("_refused_said") != shipped:
            brain._refused_said = shipped
            brain.log(f"[UPDATE] {shipped} is on the server, but it didn't run here before: "
                      f"staying on {VERSION} until a newer one comes")
        return False
    try:
        data = brain.monitor.brain_update()
    except (ServerDown, Unauthorized) as exc:
        brain.log(f"[UPDATE] {shipped} couldn't come down: {exc}")
        return False
    if not data:
        return False                             # a server that doesn't hand it out yet
    return install(data, shipped, Path(__file__).resolve().parent, Path(brain.cfg.home), VERSION, log=brain.log)
