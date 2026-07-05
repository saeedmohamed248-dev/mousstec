from django.apps import AppConfig


class BmwEcuConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bmw_ecu"
    verbose_name = "BMW/Mini ECU Subsystem"

    def ready(self) -> None:
        # Load the workshop's VERIFIED FA catalog (type→engine + engine
        # transforms) into the in-memory registries. No-op when the file is
        # absent; never raises so a bad/missing catalog can't block startup.
        try:
            from .coding.fa_catalog import load_fa_catalog_from_file
            load_fa_catalog_from_file()
        except Exception:  # pragma: no cover - startup must never crash here
            pass
