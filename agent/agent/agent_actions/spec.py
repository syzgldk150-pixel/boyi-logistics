"""One declaration shape for every action the model may choose (agent layer P1)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

READ_EFFECTS = frozenset({"read", "compute"})
# Every effect that is not a read counts as a write, whatever its spelling
# (Service V2 ``internal_write`` is projected as ``internal_projection_write``).
WRITE_EFFECTS = frozenset({"internal_write", "internal_projection_write", "external_write", "financial_write",
                           "destructive", "write"})


def is_write_effect(effect: str) -> bool:
    return str(effect or "") not in READ_EFFECTS


@dataclass(frozen=True)
class ActionSpec:
    """What the gateway needs to know about an action before it may run.

    ``capability`` is the identity agent capability (``business.query``,
    ``plugins.execute`` ...). ``input_schema`` is the closed object schema of
    the arguments the model may fill; ``None`` means the model fills nothing.
    ``preview`` actions only produce candidates on their first call (scan,
    self-pickup, split) and the person confirms the real run from that preview.
    """

    action_id: str
    title: str
    effect: str
    capability: str
    input_schema: Mapping[str, Any] | None = None
    preview: bool = False
    batch: bool = False
    irreversible: bool = False

    def __post_init__(self) -> None:
        if not self.effect or not self.action_id or not self.capability:
            raise ValueError("an action needs an id and a capability")

    @property
    def writes(self) -> bool:
        return is_write_effect(self.effect)


__all__ = ["ActionSpec", "READ_EFFECTS", "WRITE_EFFECTS", "is_write_effect"]
