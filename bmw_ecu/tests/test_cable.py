"""CANable / D-CAN cable config + connectivity endpoint.

Config-building is pure Python (no hardware); the endpoint tests cover the
simulator demo, the bad-config 400, and the honest 'adapter did not open'
path (python-can / a real adapter is never present in CI, so a live probe
resolves to adapter_ok=False with a clear detail — never a crash).
"""
from __future__ import annotations

import os
import unittest

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from bmw_ecu.connection.base import TransportKind
from bmw_ecu.connection.cable import (
    CableConfigError,
    cable_config,
    cable_config_from_env,
    cable_config_from_request,
)


# ── Pure config assembly (no Django needed, but grouped for one runner) ──
class CableConfigTests(SimpleTestCase):
    def test_builds_kdcan_config_with_parsed_hex_ids(self) -> None:
        cfg = cable_config(serial_port="/dev/ttyACM0",
                           can_tx_id="0x6F1", can_rx_id="0x612")
        self.assertEqual(cfg.kind, TransportKind.KDCAN)
        self.assertEqual(cfg.serial_port, "/dev/ttyACM0")
        self.assertEqual(cfg.can_tx_id, 0x6F1)
        self.assertEqual(cfg.can_rx_id, 0x612)
        self.assertEqual(cfg.bitrate, 500_000)     # D-CAN default
        self.assertEqual(cfg.can_interface, "slcan")

    def test_missing_serial_port_raises(self) -> None:
        with self.assertRaises(CableConfigError):
            cable_config(serial_port="", can_tx_id="0x6F1", can_rx_id="0x612")

    def test_missing_can_id_raises(self) -> None:
        with self.assertRaises(CableConfigError):
            cable_config(serial_port="/dev/ttyACM0",
                         can_tx_id=None, can_rx_id="0x612")

    def test_garbage_can_id_raises(self) -> None:
        with self.assertRaises(CableConfigError):
            cable_config(serial_port="/dev/ttyACM0",
                         can_tx_id="ZZZ", can_rx_id="0x612")

    def test_from_env_returns_none_when_unset(self) -> None:
        for k in ("BMW_ECU_KDCAN_PORT", "BMW_ECU_CAN_TX_ID", "BMW_ECU_CAN_RX_ID"):
            os.environ.pop(k, None)
        self.assertIsNone(cable_config_from_env())

    def test_from_env_builds_when_set(self) -> None:
        prev = {k: os.environ.get(k) for k in
                ("BMW_ECU_KDCAN_PORT", "BMW_ECU_CAN_TX_ID", "BMW_ECU_CAN_RX_ID")}
        os.environ["BMW_ECU_KDCAN_PORT"] = "/dev/ttyACM1"
        os.environ["BMW_ECU_CAN_TX_ID"] = "0x6F1"
        os.environ["BMW_ECU_CAN_RX_ID"] = "0x612"
        try:
            cfg = cable_config_from_env()
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.serial_port, "/dev/ttyACM1")
            self.assertEqual(cfg.can_tx_id, 0x6F1)
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_from_request_overrides_env(self) -> None:
        cfg = cable_config_from_request({
            "serial_port": "/dev/cu.usbmodem1411",
            "can_tx_id": "0x6F1", "can_rx_id": 0x612,
        })
        self.assertEqual(cfg.serial_port, "/dev/cu.usbmodem1411")
        self.assertEqual(cfg.can_rx_id, 0x612)


class _User:
    is_authenticated = True
    is_active = True
    is_staff = False
    pk = 1
    id = 1


class CablePingEndpointTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = APIRequestFactory()

    def _post(self, payload, expect=200):
        from bmw_ecu.api.cable_views import cable_ping
        request = self.factory.post("/api/ecu/cable/ping", payload, format="json")
        force_authenticate(request, user=_User())
        response = cable_ping(request)
        self.assertEqual(response.status_code, expect, getattr(response, "data", None))
        return response.data

    def test_simulator_returns_labelled_demo(self) -> None:
        prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ["BMW_ECU_SIMULATOR"] = "1"
        try:
            out = self._post({})
            self.assertTrue(out["simulated"])
            self.assertTrue(out["adapter_ok"])
            self.assertTrue(out["ecu_answered"])
            self.assertIn("hint", out)
        finally:
            if prev is None:
                os.environ.pop("BMW_ECU_SIMULATOR", None)
            else:
                os.environ["BMW_ECU_SIMULATOR"] = prev

    def test_bad_config_400_when_no_env_no_body(self) -> None:
        prev = {k: os.environ.get(k) for k in
                ("BMW_ECU_SIMULATOR", "BMW_ECU_KDCAN_PORT",
                 "BMW_ECU_CAN_TX_ID", "BMW_ECU_CAN_RX_ID")}
        for k in prev:
            os.environ.pop(k, None)
        try:
            out = self._post({}, expect=400)
            self.assertEqual(out["error"], "bad_cable_config")
        finally:
            for k, v in prev.items():
                if v is not None:
                    os.environ[k] = v

    def test_live_probe_reports_adapter_not_opened_without_hardware(self) -> None:
        # No simulator, full config, but no real CANable/python-can present →
        # the probe must resolve to a clean adapter_ok=False, not a 500.
        prev = os.environ.get("BMW_ECU_SIMULATOR")
        os.environ.pop("BMW_ECU_SIMULATOR", None)
        try:
            out = self._post({"serial_port": "/dev/does-not-exist",
                              "can_tx_id": "0x6F1", "can_rx_id": "0x612"})
            self.assertFalse(out["simulated"])
            self.assertFalse(out["adapter_ok"])
            self.assertFalse(out["ecu_answered"])
        finally:
            if prev is not None:
                os.environ["BMW_ECU_SIMULATOR"] = prev


class CableUrlTests(SimpleTestCase):
    def test_route_resolves(self) -> None:
        from django.urls import reverse
        self.assertEqual(reverse("bmw_ecu:bmw_ecu_api:cable_ping"),
                         "/api/ecu/cable/ping")


if __name__ == "__main__":
    unittest.main()
