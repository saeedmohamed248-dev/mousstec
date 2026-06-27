"""Live data streaming — PID catalog, rolling stats, anomaly flags.

Competitor tools (AutoX / Autel / Launch) sell "live data with graphing"
as a headline feature: stream sensor values in real time, draw them on a
moving chart, and flag anything outside its expected band. This module is
the engine for that, built the same hardware-free way as the rest of the
suite.

Pieces
------
  • Pid                — one streamable signal: code, bilingual name,
                         unit, expected band (for out-of-range flagging).
  • PID_CATALOG        — the common BMW/OBD signals a workshop watches.
  • LiveDataSample     — one timestamped frame: {pid_code: value}.
  • LiveDataSession    — a ring buffer of samples + per-PID rolling stats
                         (min / max / avg / latest) + out-of-range flags,
                         plus a graph-friendly to_dict() the UI charts.
  • AbstractLiveDataProvider / MockLiveDataProvider — the transport
                         (ReadDataByIdentifier polling in production; a
                         scripted/synthetic generator in tests).
  • LiveDataMonitor    — ties an entitlement gate to a session: check()
                         the 'live_data_stream' grant before the first
                         poll, consume() once when the session is stopped.

Everything is pure-Python and deterministic under test (the mock yields
scripted frames), so graphing logic + anomaly detection are unit-tested
without a car.
"""
from __future__ import annotations

import abc
import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..services.entitlement_guard import AbstractEntitlementGuard

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Pid:
    code: str
    name_ar: str
    name_en: str
    unit: str
    min_expected: float
    max_expected: float

    def in_range(self, value: float) -> bool:
        return self.min_expected <= value <= self.max_expected

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name_ar": self.name_ar,
            "name_en": self.name_en, "unit": self.unit,
            "min_expected": self.min_expected,
            "max_expected": self.max_expected,
        }


PID_CATALOG: dict[str, Pid] = {
    p.code: p for p in [
        Pid("rpm", "لفّات الموتور", "Engine RPM", "rpm", 600, 7000),
        Pid("coolant_temp", "حرارة المياه", "Coolant Temp", "°C", 70, 110),
        Pid("oil_temp", "حرارة الزيت", "Oil Temp", "°C", 80, 130),
        Pid("intake_temp", "حرارة سحب الهواء", "Intake Air Temp", "°C", -10, 60),
        Pid("maf", "تدفق الهواء (MAF)", "Mass Air Flow", "g/s", 2, 220),
        Pid("map", "ضغط المنيفولد (MAP)", "Manifold Pressure", "kPa", 20, 250),
        Pid("boost", "ضغط التيربو", "Boost Pressure", "bar", -0.2, 1.6),
        Pid("battery_voltage", "جهد البطارية", "Battery Voltage", "V", 11.8, 14.8),
        Pid("lambda_b1", "لامبدا بنك 1", "Lambda Bank 1", "λ", 0.8, 1.2),
        Pid("stft_b1", "تعديل الوقود اللحظي ب1", "Short Fuel Trim B1", "%", -10, 10),
        Pid("ltft_b1", "تعديل الوقود الدائم ب1", "Long Fuel Trim B1", "%", -15, 15),
        Pid("throttle", "فتحة البَوابة", "Throttle Position", "%", 0, 100),
        Pid("vehicle_speed", "سرعة السيارة", "Vehicle Speed", "km/h", 0, 300),
        Pid("rail_pressure", "ضغط ريل البنزين", "Fuel Rail Pressure", "bar", 30, 200),
        Pid("ignition_timing", "توقيت الإشعال", "Ignition Advance", "° BTDC", -10, 40),
    ]
}


def get_pid(code: str) -> Optional[Pid]:
    return PID_CATALOG.get(code)


class UnknownPid(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────
@dataclass
class LiveDataSample:
    seq: int
    values: dict[str, float]


@dataclass
class PidStats:
    pid: Pid
    latest: float = 0.0
    min_seen: float = float("inf")
    max_seen: float = float("-inf")
    _sum: float = 0.0
    _n: int = 0
    out_of_range_count: int = 0

    def update(self, value: float) -> None:
        self.latest = value
        self.min_seen = min(self.min_seen, value)
        self.max_seen = max(self.max_seen, value)
        self._sum += value
        self._n += 1
        if not self.pid.in_range(value):
            self.out_of_range_count += 1

    @property
    def avg(self) -> float:
        return self._sum / self._n if self._n else 0.0

    @property
    def is_currently_out_of_range(self) -> bool:
        return self._n > 0 and not self.pid.in_range(self.latest)

    def to_dict(self) -> dict:
        return {
            "pid": self.pid.to_dict(),
            "latest": round(self.latest, 3),
            "min": round(self.min_seen, 3) if self._n else None,
            "max": round(self.max_seen, 3) if self._n else None,
            "avg": round(self.avg, 3),
            "samples": self._n,
            "out_of_range_count": self.out_of_range_count,
            "currently_out_of_range": self.is_currently_out_of_range,
        }


class LiveDataSession:
    """A selection of PIDs + a bounded history of samples + rolling
    stats. `record()` ingests one frame; `to_dict()` hands the UI a
    graph-ready payload (per-PID series + stats + anomaly flags)."""

    def __init__(self, pid_codes: list[str], *, max_samples: int = 600) -> None:
        unknown = [c for c in pid_codes if c not in PID_CATALOG]
        if unknown:
            raise UnknownPid(f"غير معروف: {', '.join(unknown)}")
        if not pid_codes:
            raise UnknownPid("لازم تختار PID واحد على الأقل.")
        self.pid_codes = list(dict.fromkeys(pid_codes))  # de-dupe, keep order
        self.max_samples = max_samples
        self.samples: list[LiveDataSample] = []
        self.stats: dict[str, PidStats] = {
            c: PidStats(pid=PID_CATALOG[c]) for c in self.pid_codes
        }
        self._seq = 0

    def record(self, values: dict[str, float]) -> LiveDataSample:
        # Keep only the PIDs this session subscribed to.
        frame = {c: float(values[c]) for c in self.pid_codes if c in values}
        sample = LiveDataSample(seq=self._seq, values=frame)
        self._seq += 1
        self.samples.append(sample)
        if len(self.samples) > self.max_samples:
            # Ring-buffer: drop oldest (stats keep their full history).
            self.samples.pop(0)
        for code, value in frame.items():
            self.stats[code].update(value)
        return sample

    @property
    def sample_count(self) -> int:
        return self._seq

    def anomalies(self) -> list[str]:
        """PIDs that have been out of range at least once."""
        return [c for c, s in self.stats.items() if s.out_of_range_count > 0]

    def series(self, pid_code: str) -> list[float]:
        """Buffered values for one PID — the y-axis of the moving chart."""
        if pid_code not in self.stats:
            raise UnknownPid(pid_code)
        return [s.values[pid_code] for s in self.samples if pid_code in s.values]

    def to_dict(self) -> dict:
        return {
            "pid_codes": list(self.pid_codes),
            "sample_count": self.sample_count,
            "buffered": len(self.samples),
            "anomalies": self.anomalies(),
            "stats": {c: s.to_dict() for c, s in self.stats.items()},
            "series": {c: self.series(c) for c in self.pid_codes},
        }


# ─────────────────────────────────────────────────────────────────────
# Transport
# ─────────────────────────────────────────────────────────────────────
class AbstractLiveDataProvider(abc.ABC):
    @abc.abstractmethod
    async def poll(self, pid_codes: list[str]) -> dict[str, float]:
        """Read one frame of values for the requested PIDs."""


@dataclass
class MockLiveDataProvider(AbstractLiveDataProvider):
    """Deterministic test double. Either replay a scripted list of frames
    (`frames`) one-per-poll, or fall back to a fixed `defaults` dict.
    `poll_calls` records every requested PID set for assertions."""
    frames: list[dict[str, float]] = field(default_factory=list)
    defaults: dict[str, float] = field(default_factory=dict)
    poll_calls: list[list[str]] = field(default_factory=list)
    _i: int = 0

    async def poll(self, pid_codes: list[str]) -> dict[str, float]:
        self.poll_calls.append(list(pid_codes))
        if self._i < len(self.frames):
            frame = self.frames[self._i]
            self._i += 1
            return dict(frame)
        return dict(self.defaults)


# ─────────────────────────────────────────────────────────────────────
# Monitor — entitlement-gated driver around a session.
# ─────────────────────────────────────────────────────────────────────
class LiveDataMonitor:
    """Wraps a LiveDataSession with an entitlement gate. start() checks
    the 'live_data_stream' grant before the first poll; stop() consumes
    one use. poll_once() ingests a single frame from the provider."""

    def __init__(self, *, provider: AbstractLiveDataProvider,
                 pid_codes: list[str],
                 vin: str = "",
                 max_samples: int = 600,
                 entitlement: Optional["AbstractEntitlementGuard"] = None,
                 ) -> None:
        self.provider = provider
        self.session = LiveDataSession(pid_codes, max_samples=max_samples)
        self.vin = (vin or "").strip().upper()
        self.entitlement = entitlement
        self.started = False
        self.stopped = False
        self.refusal_reason = ""

    async def start(self) -> bool:
        """Returns True if the stream may proceed. On a failed
        entitlement check returns False and stores `refusal_reason`."""
        if self.entitlement is not None:
            entitled, reason = self.entitlement.check()
            if not entitled:
                self.refusal_reason = reason
                return False
        self.started = True
        return True

    async def poll_once(self) -> LiveDataSample:
        if not self.started:
            raise RuntimeError("start() must succeed before polling.")
        if self.stopped:
            raise RuntimeError("monitor already stopped.")
        frame = await self.provider.poll(self.session.pid_codes)
        return self.session.record(frame)

    async def run(self, *, frames: int) -> LiveDataSession:
        """Convenience: poll `frames` times. Caller must have start()ed."""
        for _ in range(frames):
            await self.poll_once()
        return self.session

    def stop(self) -> dict:
        """End the stream, consume one grant use, return the final
        graph-ready payload."""
        if not self.started:
            raise RuntimeError("stop() before a successful start().")
        if not self.stopped:
            self.stopped = True
            if self.entitlement is not None:
                op_ref = f"livedata-{self.vin or 'no-vin'}"
                self.entitlement.consume(vin=self.vin, operation_ref=op_ref)
        return self.session.to_dict()
