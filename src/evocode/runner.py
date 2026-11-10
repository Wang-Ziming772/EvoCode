"""Orchestrate one repair and persist its bounded, credential-aware artifacts."""

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evocode.agent import AgentController, AgentStatus, RunResult
from evocode.agent.controller import EventCallbackError
from evocode.artifacts import ArtifactError, Redactor, RunArtifacts, observation_summary, utc_now
from evocode.models import ConfigError, LLMProvider, ModelConfig, load_model_config
from evocode.sandbox import DockerExecutionError, DockerTestExecutor
from evocode.tasks.loader import load_task
from evocode.tasks.workspace import Workspace
from evocode.tools import create_default_registry


@dataclass(frozen=True, slots=True)
class RepairOptions:
    task: Path
    profile: str | None = None
    env_file: Path = Path(".env")
    output_dir: Path = Path("runs")
    max_steps: int = 10
    max_tool_calls: int = 30
    image: str = "evocode-pytest:py312"
    test_timeout: float = 30


@dataclass(frozen=True, slots=True)
class RepairExecution:
    task: dict | None
    task_path: Path
    source_dir: Path | None
    config: ModelConfig | None = field(repr=False)
    result: RunResult | None = field(repr=False)
    elapsed_seconds: float
    run_dir: Path | None
    status: str
    reason: str
    exit_code: int
    model_requests: int
    tool_calls: int
    redactor: Redactor = field(repr=False)


def _error_reason(exc: Exception, redactor: Redactor) -> str:
    if isinstance(exc, EventCallbackError):
        return "保存运行轨迹失败"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "Docker 启动检查超时"
    if isinstance(exc, (ConfigError, DockerExecutionError, ArtifactError, ValueError)):
        return redactor.summary(str(exc), 1000)[0]
    return "运行准备、工作区清理或产物保存失败"


def _append_terminal_event(artifacts: RunArtifacts, status: str, reason: str, exit_code: int) -> None:
    """Append the overall outcome after the live trajectory stream has closed."""
    event = {"type": "run_finished", "time": utc_now(), "status": status,
             "reason": artifacts.redactor.summary(reason, 1000)[0], "exit_code": exit_code}
    with artifacts.trajectory_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
        stream.flush()


def run_repair(options: RepairOptions) -> RepairExecution:
    start = time.monotonic()
    started_at = utc_now()
    task_path = options.task.resolve()
    redactor = Redactor(options.env_file)
    task: dict | None = None
    source_dir: Path | None = None
    config: ModelConfig | None = None
    result: RunResult | None = None
    artifacts: RunArtifacts | None = None
    patch = ""
    status, reason, exit_code = "FAILED", "运行准备失败", 2
    preparation_error: BaseException | None = None

    try:
        if type(options.max_steps) is not int or options.max_steps <= 0:
            raise ValueError("max_steps 必须是正整数")
        if type(options.max_tool_calls) is not int or options.max_tool_calls <= 0:
            raise ValueError("max_tool_calls 必须是正整数")
        task = load_task(task_path)
        source_dir = (task_path.parent / task["repo"]).absolute()
        if not source_dir.is_dir():
            raise ValueError("任务代码目录不存在")
        config = load_model_config(options.profile, options.env_file)
    except (Exception, KeyboardInterrupt) as exc:
        preparation_error = exc

    def payload(current_status: str, current_reason: str, *, finished: bool) -> dict[str, Any]:
        explanation = result.final_explanation if result else None
        explanation_truncated = False
        if explanation is not None:
            explanation, explanation_truncated = redactor.summary(explanation)
        model = {
            "profile": config.profile if config else options.profile,
            "model_id": redactor.summary(config.model_id, 1000)[0] if config else None,
            "reasoning_effort": config.reasoning_effort if config else None,
            "max_output_tokens": config.max_output_tokens if config else None,
        }
        return {
            "schema_version": 1, "run_id": artifacts.run_id if artifacts else None,
            "task_id": redactor.summary(task["task_id"], 1000)[0] if task else None,
            "task_file": redactor.text(str(task_path)),
            "source_dir": redactor.text(str(source_dir.resolve())) if source_dir else None,
            "status": current_status, "reason": redactor.summary(current_reason, 1000)[0],
            "final_explanation": explanation, "final_explanation_truncated": explanation_truncated,
            "model": model, "docker": {"image": redactor.summary(options.image, 1000)[0],
                                       "test_timeout_seconds": options.test_timeout},
            "model_requests": result.model_requests if result else (artifacts.model_requests if artifacts else 0),
            "tool_calls": result.tool_calls if result else (artifacts.tool_calls if artifacts else 0),
            "elapsed_seconds": round(time.monotonic() - start, 6),
            "last_verification": observation_summary(result.last_verification, redactor) if result else None,
            "started_at": started_at, "finished_at": utc_now() if finished else None,
        }

    try:
        output_root = options.output_dir.resolve()
        if source_dir and output_root.is_relative_to(source_dir.resolve()):
            raise ArtifactError("输出目录不能位于源代码目录内")
        artifacts = RunArtifacts(output_root, redactor)
        artifacts.write_result(payload("RUNNING", "运行准备中", finished=False))
    except KeyboardInterrupt:
        status, reason, exit_code = "INTERRUPTED", "用户中断", 130
    except Exception as exc:
        status, reason, exit_code = "FAILED", _error_reason(exc, redactor), 2
    else:
        try:
            if preparation_error is not None:
                raise preparation_error
            executor = DockerTestExecutor(image=options.image, timeout_seconds=options.test_timeout)
            executor.check_available()
            with Workspace(source_dir, editable_files=task.get("editable_files", [])) as workspace:
                try:
                    controller = AgentController(
                        LLMProvider(config), create_default_registry(),
                        max_steps=options.max_steps, max_tool_calls=options.max_tool_calls,
                        event_callback=artifacts.event,
                    )
                    result = controller.run(issue=task["issue"], workspace=workspace, test_executor=executor)
                    patch = result.patch
                except BaseException:
                    try:
                        patch = workspace.export_patch()
                    except Exception:
                        pass
                    raise
            status, reason = result.status.value, result.reason
            exit_code = 0 if result.status == AgentStatus.SUCCEEDED else 1
        except KeyboardInterrupt:
            status, reason, exit_code = "INTERRUPTED", "用户中断", 130
        except Exception as exc:
            status, reason, exit_code = "FAILED", _error_reason(exc, redactor), 2

    if artifacts is not None:
        try:
            artifacts.write_patch(patch)
        except KeyboardInterrupt:
            status, reason, exit_code = "INTERRUPTED", "用户中断", 130
        except Exception as exc:
            if exit_code != 130:
                status, reason, exit_code = "FAILED", _error_reason(exc, redactor), 2
        try:
            artifacts.write_result(payload(status, reason, finished=True))
        except KeyboardInterrupt:
            status, reason, exit_code = "INTERRUPTED", "用户中断", 130
            try:
                artifacts.write_result(payload(status, reason, finished=True))
            except Exception:
                pass
        except Exception:
            if exit_code != 130:
                status, reason, exit_code = "FAILED", "保存运行结果失败", 2
            try:
                artifacts.event({"type": "artifact_error", "reason": reason})
            except KeyboardInterrupt:
                status, reason, exit_code = "INTERRUPTED", "用户中断", 130
            except Exception:
                pass
            try:
                artifacts.write_result(payload(status, reason, finished=True))
            except Exception:
                pass
        try:
            artifacts.close()
        except KeyboardInterrupt:
            status, reason, exit_code = "INTERRUPTED", "用户中断", 130
            try:
                artifacts.write_result(payload(status, reason, finished=True))
            except Exception:
                pass
            try:
                artifacts.close()
            except Exception:
                pass
        except Exception:
            if exit_code != 130:
                status, reason, exit_code = "FAILED", "关闭运行轨迹失败", 2
            try:
                artifacts.write_result(payload(status, reason, finished=True))
            except Exception:
                pass
            try:
                artifacts.close()
            except Exception:
                pass
        try:
            _append_terminal_event(artifacts, status, reason, exit_code)
        except KeyboardInterrupt:
            status, reason, exit_code = "INTERRUPTED", "用户中断", 130
            try:
                artifacts.write_result(payload(status, reason, finished=True))
            except Exception:
                pass
            try:
                _append_terminal_event(artifacts, status, reason, exit_code)
            except Exception:
                pass
        except Exception:
            if exit_code != 130:
                status, reason, exit_code = "FAILED", "保存运行轨迹失败", 2
            try:
                artifacts.write_result(payload(status, reason, finished=True))
            except Exception:
                pass
            try:
                _append_terminal_event(artifacts, status, reason, exit_code)
            except Exception:
                pass

    return RepairExecution(task, task_path, source_dir, config, result, time.monotonic() - start,
                           artifacts.path if artifacts else None, status, reason, exit_code,
                           result.model_requests if result else (artifacts.model_requests if artifacts else 0),
                           result.tool_calls if result else (artifacts.tool_calls if artifacts else 0), redactor)
