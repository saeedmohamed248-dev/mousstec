"""Settings: brain.ini (see brain.ini.example), then MOUSS_* environment variables.

Looked for in: --config, $MOUSS_CONFIG, the current folder, next to this
package, ~/.mouss-brain/. brain.ini holds the device token: it's gitignored,
and never sent anywhere but the robot API.
"""

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HOME = Path(os.environ.get("MOUSS_HOME") or Path.home() / ".mouss-brain")
HERE = Path(__file__).resolve().parent


@dataclass
class Config:
    api_base: str = ""
    token: str = ""
    mic: str = ""                    # "" = the default; a number, or part of the name
    speaker: str = ""
    volume: float = 1.0
    model: str = "small"             # small · medium · large-v3-turbo (see `bench`)
    language: str = "ar"             # "auto" lets Whisper guess (short clips guess badly)
    device: str = "auto"             # cpu · cuda · auto
    compute_type: str = "int8"
    beam_size: int = 1               # 1 = fastest; 5 = a bit more accurate
    ears_use: str = "auto"           # auto · always · offline: when Whisper here hears (loop.server_hears_first)
    name: str = "موس"
    answer_here: bool = True         # stock and prices from this computer's catalog at once, the server after
    teach: bool = True               # the recording of a question goes to a teacher (robot/teachers.py)
    teach_all: bool = False          # every question, not only the ones it isn't sure of ([robot] teach = yes)
    min_start: int = 150             # listening starts above max(this, 3.5 × the room)
    max_start: int = 2500
    end_silence_ms: int = 600        # a pause this long ends a sentence
    media: dict = field(default_factory=dict)   # {"quran": folder-or-url, "calm": …}
    media_volume: float = 0.6
    counter_port: int = 8765         # the counter page on the shop's Wi-Fi (0 = off)
    counter_host: str = "0.0.0.0"
    home: Path = field(default_factory=lambda: HOME)
    source: Optional[Path] = None    # the brain.ini that was read
    ini: Optional[configparser.ConfigParser] = None   # for [day] / [routine …]

    def missing(self) -> list:
        return [k for k in ("api_base", "token") if not getattr(self, k)]


def _candidates(path):
    if path:
        return [Path(path)]
    env = os.environ.get("MOUSS_CONFIG")
    return ([Path(env)] if env else []) + [
        Path.cwd() / "brain.ini", HERE.parent / "brain.ini", HERE / "brain.ini", HOME / "brain.ini"]


def load(path=None) -> Config:
    cfg = Config()
    ini = configparser.ConfigParser(inline_comment_prefixes=(";", "#"), interpolation=None)
    for candidate in _candidates(path):
        if candidate.is_file():
            ini.read(candidate, encoding="utf-8")
            cfg.source = candidate
            break

    def get(section, key, default):
        return ini.get(section, key, fallback=str(default)).strip()

    cfg.api_base = get("server", "api_base", "")
    cfg.token = get("server", "token", "")
    cfg.mic = get("audio", "mic", "")
    cfg.speaker = get("audio", "speaker", "")
    cfg.volume = float(get("audio", "volume", cfg.volume) or 1)
    cfg.model = get("ears", "model", cfg.model) or cfg.model
    cfg.language = get("ears", "language", cfg.language)
    cfg.device = get("ears", "device", cfg.device) or "auto"
    cfg.compute_type = get("ears", "compute_type", cfg.compute_type) or "int8"
    cfg.beam_size = int(get("ears", "beam_size", cfg.beam_size) or 1)
    cfg.ears_use = (get("ears", "use", cfg.ears_use) or "auto").lower()
    cfg.name = get("robot", "name", cfg.name) or cfg.name
    cfg.answer_here = str(get("robot", "answer_here", "yes")).strip().lower() not in ("no", "off", "0", "false")
    # 🐛 [FIX]: في المحل Vosk كان «متأكد» وغلطان: «سلطة الزيت بوش» بدل «فلتر الزيت بوش» —
    # فالسؤال مكانش بيروح للمدرّس والرد كان غلط. دلوقتي yes = كل سؤال للروبوت بيسمعه Azure
    # طول ما النت شغال (في حدود دقايقه)، و unsure = بس اللي مش متأكد منه (زي الأول)، و no = أبداً.
    teach = str(get("robot", "teach", "yes")).strip().lower()
    cfg.teach = teach not in ("no", "off", "0", "false")
    cfg.teach_all = cfg.teach and teach not in ("unsure", "sometimes")
    cfg.min_start = int(get("listen", "min_start", cfg.min_start))
    cfg.max_start = int(get("listen", "max_start", cfg.max_start))
    cfg.end_silence_ms = int(get("listen", "end_silence_ms", cfg.end_silence_ms))
    for kind in ("quran", "calm"):
        # The folder first, then the stations: «quran = ~/Music/quran» and
        # «quran_url = https://a | https://b» end up as one ordered list.
        sources = [v for v in (get("media", kind, ""), get("media", f"{kind}_url", "")) if v]
        if sources:
            cfg.media[kind] = " | ".join(sources)
    cfg.media_volume = float(get("media", "volume", cfg.media_volume) or 0.6)
    cfg.counter_port = int(get("counter", "port", cfg.counter_port) or 0)
    cfg.counter_host = get("counter", "host", cfg.counter_host) or cfg.counter_host
    cfg.ini = ini

    for key, env in (("api_base", "MOUSS_API_BASE"), ("token", "MOUSS_TOKEN"),
                     ("mic", "MOUSS_MIC"), ("speaker", "MOUSS_SPEAKER"), ("model", "MOUSS_MODEL")):
        if os.environ.get(env):
            setattr(cfg, key, os.environ[env].strip())
    return cfg
