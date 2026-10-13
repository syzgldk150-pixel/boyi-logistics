"""Which part of a project schedule is plugin generation material.

The schedule kind and its on/off switch decide the scheduler contribution's
readiness, so they stay part of a committed generation.  Daily times belong
to the host timer: changing only them rewrites the committed generation's
``scheduled_tasks`` rows and must not stage a new generation.
"""

from __future__ import annotations

from typing import Any, Mapping


def schedule_generation_shape(schedule: Any) -> Any:
    """Return the generation-relevant projection of one schedule DTO."""

    if not isinstance(schedule, Mapping):
        return schedule
    if schedule.get("kind") != "daily_times":
        return dict(schedule)
    return {"kind": "daily_times", "enabled": schedule.get("enabled")}
