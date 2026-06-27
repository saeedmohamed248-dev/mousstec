"""Live data streaming — pure-Python, zero DB, zero hardware.

Asserts the rolling stats (min/max/avg/latest), out-of-range anomaly
flags, the ring-buffer cap, the graph series payload, and the
entitlement-gated monitor (check before first poll, consume once on stop).
"""
from __future__ import annotations

import asyncio
import unittest

from bmw_ecu.scan import (
    LiveDataMonitor,
    LiveDataSession,
    MockLiveDataProvider,
    PID_CATALOG,
    UnknownPid,
    get_pid,
)
from bmw_ecu.services.entitlement_guard import MockEntitlementGuard


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────
# PID catalog + session stats
# ─────────────────────────────────────────────────────────────────────
class PidCatalogTests(unittest.TestCase):
    def test_known_pids_present(self) -> None:
        for code in ("rpm", "coolant_temp", "battery_voltage", "lambda_b1"):
            self.assertIn(code, PID_CATALOG)
        self.assertEqual(get_pid("rpm").unit, "rpm")

    def test_in_range_band(self) -> None:
        pid = get_pid("coolant_temp")
        self.assertTrue(pid.in_range(90))
        self.assertFalse(pid.in_range(130))


class LiveSessionTests(unittest.TestCase):
    def test_rejects_empty_selection(self) -> None:
        with self.assertRaises(UnknownPid):
            LiveDataSession([])

    def test_rejects_unknown_pid(self) -> None:
        with self.assertRaises(UnknownPid):
            LiveDataSession(["rpm", "made_up"])

    def test_dedupes_pid_codes(self) -> None:
        s = LiveDataSession(["rpm", "rpm", "coolant_temp"])
        self.assertEqual(s.pid_codes, ["rpm", "coolant_temp"])

    def test_rolling_stats(self) -> None:
        s = LiveDataSession(["rpm"])
        for v in (800, 1200, 1000):
            s.record({"rpm": v})
        st = s.stats["rpm"]
        self.assertEqual(st.min_seen, 800)
        self.assertEqual(st.max_seen, 1200)
        self.assertEqual(st.latest, 1000)
        self.assertAlmostEqual(st.avg, 1000.0)
        self.assertEqual(s.sample_count, 3)

    def test_out_of_range_flag(self) -> None:
        s = LiveDataSession(["coolant_temp"])
        s.record({"coolant_temp": 90})    # ok
        s.record({"coolant_temp": 125})   # over 110 → anomaly
        self.assertEqual(s.stats["coolant_temp"].out_of_range_count, 1)
        self.assertIn("coolant_temp", s.anomalies())
        self.assertTrue(s.stats["coolant_temp"].is_currently_out_of_range)

    def test_clean_session_has_no_anomalies(self) -> None:
        s = LiveDataSession(["rpm", "coolant_temp"])
        s.record({"rpm": 800, "coolant_temp": 90})
        self.assertEqual(s.anomalies(), [])

    def test_ring_buffer_caps_samples_but_not_stats(self) -> None:
        s = LiveDataSession(["rpm"], max_samples=3)
        for v in (700, 800, 900, 1000, 1100):
            s.record({"rpm": v})
        self.assertEqual(len(s.samples), 3)         # buffer capped
        self.assertEqual(s.sample_count, 5)         # but counter is full
        self.assertEqual(s.stats["rpm"].min_seen, 700)   # stats keep history
        self.assertEqual(s.stats["rpm"].max_seen, 1100)

    def test_series_for_graph(self) -> None:
        s = LiveDataSession(["rpm", "coolant_temp"])
        s.record({"rpm": 800, "coolant_temp": 90})
        s.record({"rpm": 900, "coolant_temp": 92})
        self.assertEqual(s.series("rpm"), [800, 900])
        d = s.to_dict()
        self.assertEqual(d["series"]["coolant_temp"], [90, 92])
        self.assertEqual(d["stats"]["rpm"]["max"], 900)

    def test_missing_pid_in_frame_is_tolerated(self) -> None:
        s = LiveDataSession(["rpm", "coolant_temp"])
        s.record({"rpm": 800})   # no coolant this frame
        self.assertEqual(s.stats["coolant_temp"]._n, 0)
        self.assertEqual(s.series("coolant_temp"), [])


# ─────────────────────────────────────────────────────────────────────
# Monitor — provider + entitlement
# ─────────────────────────────────────────────────────────────────────
class LiveMonitorTests(unittest.TestCase):
    def test_replays_scripted_frames(self) -> None:
        prov = MockLiveDataProvider(frames=[
            {"rpm": 800, "coolant_temp": 88},
            {"rpm": 2500, "coolant_temp": 95},
            {"rpm": 9000, "coolant_temp": 130},   # both over band
        ])
        m = LiveDataMonitor(provider=prov, pid_codes=["rpm", "coolant_temp"])
        self.assertTrue(_run(m.start()))
        _run(m.run(frames=3))
        out = m.stop()
        self.assertEqual(out["sample_count"], 3)
        self.assertIn("rpm", out["anomalies"])
        self.assertIn("coolant_temp", out["anomalies"])
        self.assertEqual(out["stats"]["rpm"]["max"], 9000)
        # provider got the right PID set each poll
        self.assertEqual(len(prov.poll_calls), 3)
        self.assertEqual(prov.poll_calls[0], ["rpm", "coolant_temp"])

    def test_defaults_when_frames_exhausted(self) -> None:
        prov = MockLiveDataProvider(frames=[{"rpm": 800}],
                                    defaults={"rpm": 750})
        m = LiveDataMonitor(provider=prov, pid_codes=["rpm"])
        _run(m.start())
        _run(m.run(frames=3))
        self.assertEqual(m.session.series("rpm"), [800, 750, 750])

    def test_poll_before_start_raises(self) -> None:
        prov = MockLiveDataProvider(defaults={"rpm": 800})
        m = LiveDataMonitor(provider=prov, pid_codes=["rpm"])
        with self.assertRaises(RuntimeError):
            _run(m.poll_once())


class LiveMonitorEntitlementTests(unittest.TestCase):
    def test_unentitled_blocks_start(self) -> None:
        guard = MockEntitlementGuard(
            feature_code="live_data_stream", entitled_result=False,
            refusal_reason="no live-data grant")
        prov = MockLiveDataProvider(defaults={"rpm": 800})
        m = LiveDataMonitor(provider=prov, pid_codes=["rpm"], entitlement=guard)
        self.assertFalse(_run(m.start()))
        self.assertEqual(m.refusal_reason, "no live-data grant")
        self.assertEqual(guard.check_calls, 1)
        # never polled, never charged
        self.assertEqual(prov.poll_calls, [])
        self.assertEqual(guard.consume_calls, [])

    def test_entitled_consumes_once_on_stop(self) -> None:
        guard = MockEntitlementGuard(feature_code="live_data_stream")
        prov = MockLiveDataProvider(defaults={"rpm": 800})
        m = LiveDataMonitor(provider=prov, pid_codes=["rpm"],
                            vin="wba999", entitlement=guard)
        self.assertTrue(_run(m.start()))
        _run(m.run(frames=2))
        self.assertEqual(guard.consume_calls, [])   # not until stop
        m.stop()
        self.assertEqual(len(guard.consume_calls), 1)
        self.assertEqual(guard.consume_calls[0]["vin"], "WBA999")
        # idempotent stop — no double charge
        m.stop()
        self.assertEqual(len(guard.consume_calls), 1)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
