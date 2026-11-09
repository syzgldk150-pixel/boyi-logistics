"""Contracts for AI tasks run through the Agent's shared gateway.

A task is a closed question with a validator: the model sees only the payload
the caller prepared and its JSON answer must pass ``validate`` before anyone
stores or shows it. Pure data; no environment, network or database access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

TASK_STATUSES = ("SUCCEEDED", "INVALID", "FAILED", "UNAVAILABLE")
TASK_OUTCOMES = ("ADOPTED", "EDITED", "REJECTED")
_TASK_ID = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")
_PROMPT_VERSION = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class AITaskError(RuntimeError):
    """A task did not produce a usable answer; ``code`` is stable, ``message`` is user-facing."""

    def __init__(self, code: str, message: str, *, run_id: str | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id
        self.detail = detail


@dataclass(frozen=True)
class AITaskSpec:
    """One closed AI question.

    ``validate`` receives the parsed JSON answer and returns the normalized
    result or raises ``ValueError``/``TypeError``. ``max_retries`` only covers
    answers that fail validation; provider errors are never retried.
    ``model_override`` is reserved: the gateway uses the single globally active
    model and refuses any other value until per-task models are introduced.
    """

    task_id: str
    prompt_version: str
    system_prompt: str
    validate: Callable[[Any], Any]
    max_retries: int = 1
    timeout_seconds: float = 60.0
    model_override: str | None = None

    def __post_init__(self) -> None:
        if not _TASK_ID.fullmatch(self.task_id):
            raise ValueError("task_id must be a lowercase dotted identifier")
        if not _PROMPT_VERSION.fullmatch(self.prompt_version):
            raise ValueError("prompt_version is invalid")
        if not self.system_prompt.strip():
            raise ValueError("system_prompt is required")
        if not callable(self.validate):
            raise TypeError("validate must be callable")
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or not 0 <= self.max_retries <= 2:
            raise ValueError("max_retries must be 0, 1 or 2")
        if not 1 <= float(self.timeout_seconds) <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")


@dataclass(frozen=True)
class AITaskResult:
    run_id: str
    result: Any
    provider: str
    model: str
    config_version_id: int | None
    attempts: int


__all__ = ["AITaskError", "AITaskResult", "AITaskSpec", "TASK_OUTCOMES", "TASK_STATUSES"]
