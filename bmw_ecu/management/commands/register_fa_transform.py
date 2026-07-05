"""Register a VERIFIED FA engine transform into the persistent catalog.

Use this ONCE you have read the new engine's type code from a genuine car
(ISTA / E-Sys) — never a guessed value. It validates and persists to the
JSON catalog that `BmwEcuConfig.ready()` loads at startup.

Example (R56 N14 → N18, after reading a real N18 FA):

    python manage.py register_fa_transform \\
        --from N14 --to N18 --type-code 3R18 \\
        --from-type-code 3F30 \\
        --add S1CBA --remove S205A \\
        --note "verified from a genuine R56 N18 FA"
"""
from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bmw_ecu.coding.fa_catalog import default_catalog_path, save_engine_transform


class Command(BaseCommand):
    help = "Register a verified FA engine transform into the persistent catalog."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--from", dest="from_engine", required=True,
                            help="source engine, e.g. N14")
        parser.add_argument("--to", dest="to_engine", required=True,
                            help="target engine, e.g. N18")
        parser.add_argument("--type-code", dest="new_type_code", required=True,
                            help="VERIFIED new model/type code (from a real car)")
        parser.add_argument("--from-type-code", dest="from_type_code", default="",
                            help="the car's current type code → maps to --from "
                                 "so the source engine is derivable")
        parser.add_argument("--add", action="append", default=[],
                            help="option code to add (repeatable)")
        parser.add_argument("--remove", action="append", default=[],
                            help="option code to remove (repeatable)")
        parser.add_argument("--note", default="", help="free-text provenance note")
        parser.add_argument("--catalog", default="",
                            help="catalog path override (default: app data dir)")

    def handle(self, *args, **opts) -> None:
        path = Path(opts["catalog"]) if opts["catalog"] else default_catalog_path()
        try:
            save_engine_transform(
                path,
                from_engine=opts["from_engine"],
                to_engine=opts["to_engine"],
                new_type_code=opts["new_type_code"],
                from_type_code=opts["from_type_code"],
                add_options=opts["add"],
                remove_options=opts["remove"],
                note=opts["note"],
            )
        except ValueError as e:
            raise CommandError(str(e)) from e

        self.stdout.write(self.style.SUCCESS(
            f"Registered {opts['from_engine']}→{opts['to_engine']} "
            f"(type {opts['new_type_code']}) into {path}"))
