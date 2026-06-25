"""Cloud recorder — bridges the in-memory ECU session to Django models.

Use as a sink: every safety/uds/flashing module calls
`recorder.record_event(...)` and the recorder handles the DB write
asynchronously so the wire-level code never blocks on Postgres.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from asgiref.sync import sync_to_async

from ..logging_setup import get_logger
from ..safety.backup import EcuBackup

log = get_logger(__name__)


@dataclass
class EventRecord:
    kind: str
    ecu_name: str = ""
    success: bool = True
    backup_sha256: str = ""
    payload_summary: dict | None = None
    error_code: str = ""
    error_message: str = ""


class CloudRecorder:
    def __init__(self, *, vin: str, chassis: str = "", technician: str = "",
                 transport_kind: str = "") -> None:
        self.vin = vin
        self.chassis = chassis
        self.technician = technician
        self.transport_kind = transport_kind
        self._session_id: Optional[int] = None
        self._queue: asyncio.Queue[EventRecord] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "CloudRecorder":
        from ..models import EcuSession

        @sync_to_async
        def _open() -> int:
            return EcuSession.objects.create(
                vin=self.vin, chassis=self.chassis,
                technician=self.technician, transport_kind=self.transport_kind,
            ).pk

        self._session_id = await _open()
        self._worker = asyncio.create_task(self._drain(), name="bmw_ecu_recorder")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._queue.put(EventRecord(kind="__close__"))
        if self._worker is not None:
            await self._worker
        from django.utils import timezone
        from ..models import EcuSession

        @sync_to_async
        def _close() -> None:
            EcuSession.objects.filter(pk=self._session_id).update(ended_at=timezone.now())

        await _close()

    async def record_event(self, ev: EventRecord) -> None:
        await self._queue.put(ev)

    async def register_backup(self, backup: EcuBackup, path: str) -> None:
        from ..models import EcuBackupRef

        @sync_to_async
        def _save() -> None:
            EcuBackupRef.objects.update_or_create(
                sha256=backup.sha256,
                defaults=dict(
                    vin=backup.vin, ecu_name=backup.ecu_name,
                    memory_region=backup.memory_region, size=len(backup.data),
                    path=path, captured_at=backup.captured_at,
                ),
            )

        await _save()

    async def _drain(self) -> None:
        from ..models import EcuStateChange

        @sync_to_async
        def _write(ev: EventRecord) -> None:
            EcuStateChange.objects.create(
                session_id=self._session_id,
                kind=ev.kind, ecu_name=ev.ecu_name, success=ev.success,
                backup_sha256=ev.backup_sha256,
                payload_summary=ev.payload_summary or {},
                error_code=ev.error_code, error_message=ev.error_message,
            )

        while True:
            ev = await self._queue.get()
            if ev.kind == "__close__":
                return
            try:
                await _write(ev)
            except Exception as e:
                log.warning("Cloud recorder write failed", extra={"err": str(e)})
