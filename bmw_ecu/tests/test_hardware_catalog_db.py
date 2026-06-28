"""DB-backed Hardware Catalog (Phase: dynamic catalog → admin-editable).

Proves a workshop can register a confirmed board revision in the DB and it
wins over the bundled seed, while an empty/missing DB silently falls back to
the in-memory catalog. bmw_ecu is a TENANT_APP, so the EcuHardwareProfile
table lives in a tenant schema — these run against a provisioned tenant.
"""
from __future__ import annotations

from bmw_ecu.autodetect import (
    get_hardware_profile,
    get_hardware_profile_db_first,
)
from bmw_ecu.models import EcuHardwareProfile
from bmw_ecu.tests.base import (
    BmwEcuTenantTestCase,
    setup_module_tenant,
    teardown_module_tenant,
)


def setUpModule() -> None:
    setup_module_tenant()


def tearDownModule() -> None:
    teardown_module_tenant()


class HardwareCatalogDbTests(BmwEcuTenantTestCase):
    def test_bundled_catalog_ships_empty(self) -> None:
        # SAFETY: the bundled catalog must contain NO guessed pins. The old
        # placeholder seed (8606229 / 8623136) was removed, so the pure
        # in-memory lookup resolves nothing — only the DB can supply pins.
        self.assertIsNone(get_hardware_profile("8606229"))
        self.assertIsNone(get_hardware_profile("8623136"))

    def test_db_row_is_the_only_source_of_pins(self) -> None:
        # With the seed gone, a confirmed DB row is the SOLE way a pinout
        # becomes available — exactly the admin-driven path we enforce.
        EcuHardwareProfile.objects.create(
            hardware_id="8606229",
            ecu_name="MEVD17.2.9",
            board_revision="Rev B (confirmed on bench)",
            family="MEVD17",
            protocol="BootMode",
            power_pin=87, ground_pin=88, boot_pin=26, k_line_pin=63,
            pcb_image_url="/static/bmw_ecu/hw/confirmed_pcb.jpg",
            boot_image_url="/static/bmw_ecu/hw/confirmed_boot.jpg",
            callouts=[{"pin": 26, "label": "BOOT", "color": "yellow"}],
            physical_steps_ar=["خطوة مؤكَّدة"],
            physical_steps_en=["Confirmed step"],
            verified=True,
        )
        prof = get_hardware_profile_db_first("8606229")
        self.assertIsNotNone(prof)
        self.assertEqual(prof.pinout.boot_pin, 26)          # DB value
        self.assertTrue(prof.verified)
        self.assertEqual(prof.board_revision, "Rev B (confirmed on bench)")
        # Pure (non-DB) lookup still resolves nothing — no bundled pins.
        self.assertIsNone(get_hardware_profile("8606229"))

    def test_no_db_row_resolves_to_none(self) -> None:
        # No DB row and no bundled seed → honest None (never a guessed pin).
        self.assertIsNone(get_hardware_profile_db_first("8623136"))

    def test_db_adds_brand_new_hardware_id(self) -> None:
        # A board the bundled seed has never heard of becomes resolvable
        # purely by adding a DB row — no code change.
        self.assertIsNone(get_hardware_profile("7700001"))
        EcuHardwareProfile.objects.create(
            hardware_id="7700001",
            ecu_name="N55 DME",
            board_revision="Rev A",
            family="MEVD17",
            protocol="BootMode",
            power_pin=10, ground_pin=11, boot_pin=12,
        )
        prof = get_hardware_profile_db_first("7700001")
        self.assertIsNotNone(prof)
        self.assertEqual(prof.ecu_name, "N55 DME")
        self.assertEqual(prof.pinout.boot_pin, 12)

    def test_unknown_everywhere_returns_none(self) -> None:
        self.assertIsNone(get_hardware_profile_db_first("0000000"))
