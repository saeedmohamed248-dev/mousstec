"""FA engine-change planner — catalog-gated, never guesses.

Changing the FA from one engine to another (e.g. N14 → N18 after a DME
swap) rewrites the FA's *model/type* identity plus, sometimes, a few option
codes. Those values are BMW-internal: a wrong type code mis-codes the whole
car. So this planner refuses to invent them. It applies a change ONLY when a
VERIFIED transform has been registered for that exact (from_engine,
to_engine) pair — otherwise it raises `UnverifiedFaTransform` and the caller
tells the tech to register the confirmed values first.

    register_fa_engine_transform("N14", "N18", new_type_code="....",
                                 add_options=(...), remove_options=(...))
    plan = plan_fa_engine_change(vo, to_engine="N18")   # → FaChangePlan
    plan.new_raw   # the rebuilt FA to write (after explicit confirm)

The planner produces the canonical ASCII FA for preview/confirm; it never
writes anything itself. Registration is expected from a data migration /
Django admin backed by the workshop's verified FA catalog.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .fa_engine import engine_from_vo
from .fa_vo import VehicleOrder, parse_fa


class UnverifiedFaTransform(ValueError):
    """No verified transform registered for the requested engine change."""


@dataclass(frozen=True)
class _Transform:
    new_type_code: str
    add_options: tuple[str, ...] = ()
    remove_options: tuple[str, ...] = ()
    note: str = ""


# (from_engine, to_engine) → verified transform. EMPTY by default — only
# grows with confirmed values; never seed from memory.
_ENGINE_TRANSFORMS: dict[tuple[str, str], _Transform] = {}


def register_fa_engine_transform(from_engine: str, to_engine: str, *,
                                 new_type_code: str,
                                 add_options: Iterable[str] = (),
                                 remove_options: Iterable[str] = (),
                                 note: str = "") -> None:
    """Register a VERIFIED FA engine transform (admin / migration)."""
    key = ((from_engine or "").strip().upper(),
           (to_engine or "").strip().upper())
    if not (key[0] and key[1]):
        raise ValueError("from_engine and to_engine are required")
    if not new_type_code.strip():
        raise ValueError("new_type_code is required (never guessed)")
    _ENGINE_TRANSFORMS[key] = _Transform(
        new_type_code=new_type_code.strip().upper(),
        add_options=tuple(o.strip().upper() for o in add_options if o.strip()),
        remove_options=tuple(o.strip().upper() for o in remove_options if o.strip()),
        note=note,
    )


def clear_fa_engine_transforms() -> None:
    """Test/admin helper — wipe the registered catalog."""
    _ENGINE_TRANSFORMS.clear()


@dataclass(frozen=True)
class FaChangePlan:
    from_engine: str
    to_engine: str
    old_type_code: str
    new_type_code: str
    old_raw: str
    new_raw: str
    added_options: tuple[str, ...]
    removed_options: tuple[str, ...]
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "from_engine": self.from_engine, "to_engine": self.to_engine,
            "old_type_code": self.old_type_code,
            "new_type_code": self.new_type_code,
            "old_raw": self.old_raw, "new_raw": self.new_raw,
            "added_options": list(self.added_options),
            "removed_options": list(self.removed_options),
            "note": self.note,
        }


def _rebuild_fa(type_code: str, options: Iterable[str], e_word: str) -> str:
    """Rebuild the canonical hyphen-separated ASCII FA for preview/write."""
    parts = [type_code] + sorted(o.upper() for o in options)
    if e_word:
        parts.append(e_word)
    return "-".join(p for p in parts if p)


def plan_fa_engine_change(vo: VehicleOrder, *, to_engine: str) -> FaChangePlan:
    """Plan the FA change to `to_engine`. Raises UnverifiedFaTransform unless
    a verified transform is registered for (detected_engine → to_engine)."""
    to_engine = (to_engine or "").strip().upper()
    from_engine = engine_from_vo(vo)
    if not from_engine:
        raise UnverifiedFaTransform(
            "source engine not derivable from the FA — register the type_code "
            "or provide the engine explicitly")
    if from_engine == to_engine:
        raise UnverifiedFaTransform(
            f"FA already reports {to_engine} — nothing to change")

    key = (from_engine, to_engine)
    tf = _ENGINE_TRANSFORMS.get(key)
    if tf is None:
        raise UnverifiedFaTransform(
            f"no verified FA transform registered for {from_engine}→{to_engine}"
            " — register the confirmed type code before writing")

    old_options = set(vo.options)
    new_options = (old_options | set(tf.add_options)) - set(tf.remove_options)
    new_raw = _rebuild_fa(tf.new_type_code, new_options, vo.e_word)

    return FaChangePlan(
        from_engine=from_engine, to_engine=to_engine,
        old_type_code=vo.type_code, new_type_code=tf.new_type_code,
        old_raw=vo.raw, new_raw=new_raw,
        added_options=tuple(sorted(new_options - old_options)),
        removed_options=tuple(sorted(old_options - new_options)),
        note=tf.note,
    )


def plan_from_raw(fa_raw: str, *, to_engine: str) -> FaChangePlan:
    """Convenience: parse a raw FA then plan the engine change."""
    return plan_fa_engine_change(parse_fa(fa_raw), to_engine=to_engine)
