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
    name: str = "موس"
    min_start: int = 150             # listening starts above max(this, 3.5 × the room)
    max_start: int = 2500
    end_silence_ms: int = 600        # a pause this long ends a sentence
    home: Path = field(default_factory=lambda: HOME)
    source: Optional[Path] = None    # the brain.ini that was read

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
    cfg.name = get("robot", "name", cfg.name) or cfg.name
    cfg.min_start = int(get("listen", "min_start", cfg.min_start))
    cfg.max_start = int(get("listen", "max_start", cfg.max_start))
    cfg.end_silence_ms = int(get("listen", "end_silence_ms", cfg.end_silence_ms))

    for key, env in (("api_base", "MOUSS_API_BASE"), ("token", "MOUSS_TOKEN"),
                     ("mic", "MOUSS_MIC"), ("speaker", "MOUSS_SPEAKER"), ("model", "MOUSS_MODEL")):
        if os.environ.get(env):
            setattr(cfg, key, os.environ[env].strip())
    return cfg
