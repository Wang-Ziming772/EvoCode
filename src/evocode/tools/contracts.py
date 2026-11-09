"""Provider-independent tool calls, observations, and execution context."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from evocode.tasks.workspace import Workspace
from evocode.tools.testing import run_tests


class TestResult(Protocol):
    returncode: int
    stdout: str | None
    stderr: str | None


TestExecutor = Callable[[Path], TestResult]


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Observation:
    call_id: str
    status: Literal["ok", "error", "timeout"]
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolContext:
    workspace: Workspace
    # A replacement backend returns a CompletedProcess-compatible result and
    # raises TimeoutExpired or TimeoutError on timeout.
    test_executor: TestExecutor = run_tests


@dataclass(frozen=True, slots=True)
class ToolOutput:
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolOutput]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


class ToolError(Exception):
    """An expected tool failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
