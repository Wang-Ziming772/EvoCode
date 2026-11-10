"""In-memory controller state and results."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from evocode.tools import Observation


class AgentStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True, slots=True)
class RunResult:
    status: AgentStatus
    reason: str
    final_explanation: str | None
    model_requests: int
    tool_calls: int
    last_verification: Observation | None
    patch: str
    events: list[dict[str, Any]]
