"""Offline tests for the repair orchestration and CLI."""

import json
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evocode import cli, runner
from evocode.agent import AgentStatus
from evocode.artifacts import Redactor, RunArtifacts
from evocode.models import LLMError, LLMResponse
from evocode.sandbox import DockerExecutionError, DockerTestExecutor
from evocode.tasks.workspace import Workspace
from evocode.tools import ToolCall


@pytest.fixture
def repair_inputs(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("EVOCODE_"):
            monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    (repo / "calculator.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    task = tasks / "repair.json"
    task.write_text(json.dumps({"task_id": "repair-test", "repo": "../repo with spaces",
                                "issue": "修复计算器", "editable_files": ["calculator.py"]}), encoding="utf-8")
    env = tmp_path / "config.env"
    env.write_text("\n".join([
        "EVOCODE_DEFAULT_PROFILE=gpt", "EVOCODE_BASE_URL=https://gateway.example/v1",
        "EVOCODE_GPT_API_KEY=dummy-secret", "EVOCODE_GPT_MODEL_ID=fake-model",
        "EVOCODE_GPT_MAX_OUTPUT_TOKENS=50", "EVOCODE_GPT_REASONING_EFFORT=medium",
        "EVOCODE_REQUEST_TIMEOUT_SECONDS=5", "EVOCODE_MAX_RETRIES=0",
    ]), encoding="utf-8")
    return repo, task, env


class FakeExecutor:
    checks = 0
    calls = []
    fail_check = False

    def __init__(self, *, image, timeout_seconds):
        self.image = image
        self.timeout_seconds = timeout_seconds

    def check_available(self):
        type(self).checks += 1
        if type(self).fail_check:
            raise DockerExecutionError("Docker Linux 引擎不可用")

    def __call__(self, repo_dir):
        type(self).calls.append(repo_dir)
        content = (repo_dir / "calculator.py").read_text(encoding="utf-8")
        passed = "return a + b" in content
        return subprocess.CompletedProcess([], 0 if passed else 1, "passed" if passed else "failed", "")


class FakeProvider:
    created = []
    script = []

    def __init__(self, config):
        self.config = config
        self.responses = list(type(self).script)
        self.requests = 0
        type(self).created.append(self)

    def generate(self, messages, specs):
        self.requests += 1
        assert messages and specs
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def tool_results(self, response, observations):
        assert len(response.tool_calls) == len(observations)
        return [{"role": "assistant", "content": "tool call"}, {"role": "user", "content": "tool result"}]


@pytest.fixture
def fake_backends(monkeypatch):
    FakeExecutor.checks = 0
    FakeExecutor.calls = []
    FakeExecutor.fail_check = False
    FakeProvider.created = []
    FakeProvider.script = [
        LLMResponse("", [ToolCall("read", "read_file", {"path": "calculator.py"})]),
        LLMResponse("", [ToolCall("write", "write_file", {
            "path": "calculator.py", "content": "def add(a, b):\n    return a + b\n",
        })]),
        LLMResponse("修复完成", []),
    ]
    monkeypatch.setattr(runner, "DockerTestExecutor", FakeExecutor)
    monkeypatch.setattr(runner, "LLMProvider", FakeProvider)
    return FakeExecutor, FakeProvider


def test_repair_orchestration_uses_task_relative_repo_and_cleans_workspace(repair_inputs, fake_backends, tmp_path, monkeypatch):
    repo, task, env = repair_inputs
    before = (repo / "calculator.py").read_bytes()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, image="custom:tag", test_timeout=7))
    assert execution.result.status == AgentStatus.SUCCEEDED
    assert execution.config.model_id == "fake-model" and execution.source_dir.resolve() == repo.resolve()
    assert execution.result.model_requests == 3 and execution.result.tool_calls == 3
    assert execution.result.last_verification.metadata["test_outcome"] == "passed"
    assert "+    return a + b" in execution.result.patch
    assert FakeExecutor.checks == 1 and len(FakeExecutor.calls) == 1
    assert not FakeExecutor.calls[0].exists() and (repo / "calculator.py").read_bytes() == before


def test_repair_cli_works_with_injected_backends(repair_inputs, fake_backends, tmp_path, monkeypatch, capsys):
    _repo, task, env = repair_inputs
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    output = capsys.readouterr()
    assert "repair-test" in output.out and "SUCCEEDED" in output.out and "fake-model" in output.out
    assert "dummy-secret" not in output.out + output.err


def test_repair_cli_handles_restricted_terminal_encoding(repair_inputs, fake_backends, tmp_path, monkeypatch):
    _repo, task, env = repair_inputs
    FakeProvider.script[-1] = LLMResponse("完成 😀", [])
    output_bytes = io.BytesIO()
    output = io.TextIOWrapper(output_bytes, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    output.flush()
    assert exc.value.code == 0 and b"SUCCEEDED" in output_bytes.getvalue()
    assert b"\\U0001f600" in output_bytes.getvalue()


def test_docker_preflight_failure_never_creates_provider(repair_inputs, fake_backends, tmp_path, monkeypatch, capsys):
    _repo, task, env = repair_inputs
    FakeExecutor.fail_check = True
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and not FakeProvider.created
    captured = capsys.readouterr()
    assert "Docker" in captured.out + captured.err
    run_dir = next((tmp_path / "runs").iterdir())
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "FAILED" and "Docker" in result["reason"]
    assert result["model_requests"] == 0 and (run_dir / "patch.diff").read_bytes() == b""


@pytest.mark.parametrize("option,value", [
    ("--max-steps", "0"), ("--max-tool-calls", "-1"), ("--test-timeout", "nan"),
])
def test_repair_rejects_invalid_budgets_before_running(option, value, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", "missing.json", option, value])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_repair_help_describes_options(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["evocode", "--help"])
    with pytest.raises(SystemExit) as root:
        cli.main()
    assert root.value.code == 0 and "repair" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--help"])
    with pytest.raises(SystemExit) as subcommand:
        cli.main()
    help_text = capsys.readouterr().out
    assert subcommand.value.code == 0
    assert all(option in help_text for option in ("--profile", "--env-file", "--output-dir", "--image", "--test-timeout"))


def test_cli_relative_paths_from_another_working_directory(repair_inputs, fake_backends, tmp_path, monkeypatch, capsys):
    _repo, _task, _env = repair_inputs
    elsewhere = tmp_path / "another-directory"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", "../tasks/repair.json",
                                     "--env-file", "../config.env", "--output-dir", "results"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and "SUCCEEDED" in capsys.readouterr().out
    run_dir = next((elsewhere / "results").iterdir())
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert Path(result["task_file"]).resolve() == (tmp_path / "tasks" / "repair.json").resolve()
    assert Path(result["source_dir"]).resolve() == (tmp_path / "repo with spaces").resolve()


def test_output_artifacts_are_writable_before_docker_preflight_and_model(repair_inputs, fake_backends, tmp_path, monkeypatch):
    _repo, task, env = repair_inputs
    root = tmp_path / "runs"

    def check_available(_self):
        run_dir = next(root.iterdir())
        assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["status"] == "RUNNING"
        assert (run_dir / "patch.diff").is_file() and (run_dir / "trajectory.jsonl").is_file()
        assert not FakeProvider.created

    monkeypatch.setattr(FakeExecutor, "check_available", check_available)
    result = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=root))
    assert result.exit_code == 0 and FakeProvider.created[0].requests == 3


def test_docker_preflight_checks_engine_and_image_before_probing(monkeypatch):
    executor = DockerTestExecutor()
    commands = []

    def command(args, **_kwargs):
        commands.append(args)
        output = b"linux\n" if args[0] == "info" else ("sha256:" + "a" * 64 + " linux null").encode()
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(executor, "_command", command)
    probed = []
    monkeypatch.setattr(executor, "_probe_image", lambda image_id, _deadline: probed.append(image_id))
    assert executor.check_available() == "sha256:" + "a" * 64
    assert [command[0] for command in commands] == ["info", "image"]
    assert probed == ["sha256:" + "a" * 64]


def read_artifacts(execution):
    path = execution.run_dir
    result = json.loads((path / "result.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()]
    patch = (path / "patch.diff").read_bytes()
    return result, events, patch


def test_success_artifacts_record_ordered_bounded_events_and_exact_patch(repair_inputs, fake_backends, tmp_path):
    repo, task, env = repair_inputs
    (repo / "calculator.py").write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    before = (repo / "calculator.py").read_bytes()
    with env.open("a", encoding="utf-8") as file:
        file.write("\nEVOCODE_DEEPSEEK_API_KEY=other-dummy-secret\n")
    first = FakeProvider.script[0]
    FakeProvider.script[0] = LLMResponse(
        "visible dummy-secret other-dummy-secret Authorization: Bearer header-secret\n"
        "Authorization: opaque-header-secret " + "A" * 5000,
        first.tool_calls,
        usage={"input_tokens": 10, "output_tokens": 2, "details": {"cached_tokens": 1},
               "reasoning_content": "private-reasoning-never-record"},
        assistant_items=[{"encrypted_content": "private-reasoning-never-record"}],
    )
    FakeProvider.script[-1] = LLMResponse("完成 dummy-secret", [])
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 0 and result["status"] == "SUCCEEDED"
    assert result["schema_version"] == 1 and result["run_id"] == execution.run_dir.name
    assert result["task_id"] == "repair-test" and Path(result["source_dir"]).resolve() == repo.resolve()
    assert result["model"] == {"profile": "gpt", "model_id": "fake-model",
                               "reasoning_effort": "medium", "max_output_tokens": 50}
    assert result["model_requests"] == 3 and result["tool_calls"] == 3
    assert result["last_verification"]["metadata"]["test_outcome"] == "passed"
    assert result["started_at"] and result["finished_at"] and result["elapsed_seconds"] >= 0
    assert patch == execution.result.patch.encode("utf-8", errors="surrogateescape")
    assert b"\r\n" in patch and b"return a + b" in patch
    with Workspace(repo) as clean:
        checked = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--check",
                                  str(execution.run_dir / "patch.diff")], cwd=clean.path, capture_output=True)
        assert checked.returncode == 0, checked.stderr.decode(errors="replace")
    assert (repo / "calculator.py").read_bytes() == before
    assert events[0]["type"] == "state" and events[0]["state"] == "READY"
    assert events[-1]["type"] == "run_finished" and events[-1]["status"] == "SUCCEEDED"
    responses = [event for event in events if event["type"] == "model_response"]
    assert responses[0]["usage"] == {"input_tokens": 10, "output_tokens": 2, "details": {"cached_tokens": 1}}
    assert responses[-1]["usage"] is None and responses[0]["content_truncated"] is True
    assert "[已截断]" in responses[0]["content"]
    calls = [event for event in events if event["type"] == "tool_call"]
    assert [(item["call_id"], item["name"]) for item in calls] == [
        ("read", "read_file"), ("write", "write_file"), ("controller-verification-3", "run_tests"),
    ]
    assert calls[1]["arguments"]["content_length"] > 0
    assert "content" not in calls[1]["arguments"]
    verification = [event for event in events if event["type"] == "observation" and event["origin"] == "verification"]
    assert len(verification) == 1 and verification[0]["metadata"]["exit_code"] == 0
    serialized = (execution.run_dir / "result.json").read_text() + (execution.run_dir / "trajectory.jsonl").read_text()
    assert "dummy-secret" not in serialized and "header-secret" not in serialized
    assert "private-reasoning-never-record" not in serialized
    assert "return a + b" not in (execution.run_dir / "trajectory.jsonl").read_text()
    assert not list(execution.run_dir.glob(".*.tmp"))


def test_runs_are_unique_and_task_id_never_controls_path(repair_inputs, fake_backends, tmp_path):
    _repo, task, env = repair_inputs
    data = json.loads(task.read_text(encoding="utf-8"))
    data["task_id"] = "../../outside\\escape"
    task.write_text(json.dumps(data), encoding="utf-8")
    options = runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs")
    first, second = runner.run_repair(options), runner.run_repair(options)
    assert first.exit_code == second.exit_code == 0
    assert first.run_dir != second.run_dir and first.run_dir.parent == second.run_dir.parent == (tmp_path / "runs")
    assert read_artifacts(first)[0]["task_id"] == data["task_id"]
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("mode", ["failed", "exhausted"])
def test_failed_or_exhausted_run_preserves_result_trace_and_patch(mode, repair_inputs, fake_backends, tmp_path):
    repo, task, env = repair_inputs
    FakeProvider.script = [FakeProvider.script[1], LLMError("model failure dummy-secret")]
    options = runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs",
                                   max_steps=1 if mode == "exhausted" else 2)
    execution = runner.run_repair(options)
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 1 and result["status"] == mode.upper()
    assert result["reason"] and events[-1]["type"] == "run_finished"
    assert b"return a + b" in patch and "return a - b" in (repo / "calculator.py").read_text()
    assert result["last_verification"] is None
    assert "dummy-secret" not in json.dumps(result) + json.dumps(events)


def test_cli_budget_exhaustion_exits_one_and_keeps_patch(repair_inputs, fake_backends, tmp_path, monkeypatch, capsys):
    _repo, task, env = repair_inputs
    FakeProvider.script = [FakeProvider.script[1]]
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--output-dir", str(tmp_path / "runs"), "--max-steps", "1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    output = capsys.readouterr()
    assert exc.value.code == 1 and "EXHAUSTED" in output.out and "产物目录" in output.out
    path = next((tmp_path / "runs").iterdir())
    assert b"return a + b" in (path / "patch.diff").read_bytes()
    assert json.loads((path / "result.json").read_text(encoding="utf-8"))["status"] == "EXHAUSTED"
    assert json.loads((path / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()[-1])["status"] == "EXHAUSTED"


def test_config_failure_and_docker_failure_leave_results_without_model_requests(repair_inputs, fake_backends, tmp_path):
    _repo, task, env = repair_inputs
    env.write_text("EVOCODE_DEFAULT_PROFILE=gpt\n", encoding="utf-8")
    options = runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs")
    bad_config = runner.run_repair(options)
    result, events, patch = read_artifacts(bad_config)
    assert bad_config.exit_code == 2 and result["status"] == "FAILED"
    assert "EVOCODE_BASE_URL" in result["reason"] and result["model_requests"] == 0
    assert events[-1]["type"] == "run_finished" and patch == b""
    assert FakeExecutor.checks == 0 and not FakeProvider.created


def test_output_directory_inside_source_is_rejected_before_model_request(repair_inputs, fake_backends):
    repo, task, env = repair_inputs
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=repo / "runs"))
    assert execution.exit_code == 2 and execution.run_dir is None
    assert "输出目录" in execution.reason and not (repo / "runs").exists()
    assert not FakeProvider.created and FakeExecutor.checks == 0


def test_unwritable_output_path_prevents_model_request(repair_inputs, fake_backends, tmp_path):
    _repo, task, env = repair_inputs
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("existing", encoding="utf-8")
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=blocker / "runs"))
    assert execution.exit_code == 2 and execution.run_dir is None
    assert not FakeProvider.created and FakeExecutor.checks == 0


def test_patch_save_failure_does_not_report_success(repair_inputs, fake_backends, tmp_path, monkeypatch):
    _repo, task, env = repair_inputs
    monkeypatch.setattr(runner.RunArtifacts, "write_patch", lambda *_args: (_ for _ in ()).throw(OSError("dummy-secret")))
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 2 and execution.status == result["status"] == "FAILED"
    assert execution.result.status == AgentStatus.SUCCEEDED and patch == b""
    assert "dummy-secret" not in json.dumps(result) + json.dumps(events)


def test_cli_save_failure_exits_two_without_success_message(repair_inputs, fake_backends, tmp_path, monkeypatch, capsys):
    _repo, task, env = repair_inputs
    monkeypatch.setattr(runner.RunArtifacts, "write_patch", lambda *_args: (_ for _ in ()).throw(OSError("no space")))
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    output = capsys.readouterr()
    assert exc.value.code == 2 and "最终状态：FAILED" in output.out
    assert "最终状态：SUCCEEDED" not in output.out


def test_result_write_failure_is_reported_and_recovered_if_writable(repair_inputs, fake_backends, tmp_path, monkeypatch):
    _repo, task, env = repair_inputs
    original = runner.RunArtifacts.write_result
    attempts = []

    def write_result(self, value):
        attempts.append(value["status"])
        if len(attempts) == 2:
            raise OSError("dummy-secret")
        return original(self, value)

    monkeypatch.setattr(runner.RunArtifacts, "write_result", write_result)
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 2 and result["status"] == "FAILED"
    assert "保存运行结果失败" in result["reason"] and b"return a + b" in patch
    assert any(event["type"] == "artifact_error" for event in events)
    assert events[-1]["type"] == "run_finished" and events[-1]["status"] == result["status"]
    assert events[-1]["exit_code"] == execution.exit_code and "dummy-secret" not in json.dumps(result)


def test_trajectory_write_error_preserves_partial_patch_and_stops_requests(repair_inputs, fake_backends, tmp_path, monkeypatch):
    repo, task, env = repair_inputs
    original = runner.RunArtifacts.event

    def fail_after_edit(self, event):
        if event["type"] == "observation" and event["name"] == "write_file":
            raise OSError("dummy-secret")
        return original(self, event)

    monkeypatch.setattr(runner.RunArtifacts, "event", fail_after_edit)
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 2 and result["status"] == "FAILED" and "轨迹" in result["reason"]
    assert b"return a + b" in patch and len(FakeProvider.created) == 1
    assert FakeProvider.created[0].requests == 2 and events[-1]["type"] == "run_finished"
    assert "return a - b" in (repo / "calculator.py").read_text()


def test_interrupt_after_edit_preserves_patch_and_status(repair_inputs, fake_backends, tmp_path):
    repo, task, env = repair_inputs
    FakeProvider.script = [FakeProvider.script[1], KeyboardInterrupt()]
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 130 and result["status"] == "INTERRUPTED"
    assert b"return a + b" in patch and events[-1]["status"] == "INTERRUPTED"
    assert "return a - b" in (repo / "calculator.py").read_text()


def test_patch_with_known_credential_is_not_saved(repair_inputs, fake_backends, tmp_path):
    _repo, task, env = repair_inputs
    FakeProvider.script[1] = LLMResponse("", [ToolCall("write", "write_file", {
        "path": "calculator.py", "content": "def add(a, b):\n    return a + b\n# dummy-secret\n",
    })])
    execution = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    result, events, patch = read_artifacts(execution)
    assert execution.exit_code == 2 and result["status"] == "FAILED"
    assert "凭据" in result["reason"] and patch == b""
    assert "dummy-secret" not in json.dumps(result) + json.dumps(events)


def test_patch_artifact_preserves_surrogateescaped_bytes_and_newlines(tmp_path):
    artifacts = RunArtifacts(tmp_path / "runs", Redactor(tmp_path / "missing.env"))
    try:
        raw = b"patch\xff\r\nnext\n"
        artifacts.write_patch(raw.decode("utf-8", errors="surrogateescape"))
        assert artifacts.patch_path.read_bytes() == raw
    finally:
        artifacts.close()


def test_trajectory_event_is_flushed_before_run_finishes(tmp_path):
    artifacts = RunArtifacts(tmp_path / "runs", Redactor(tmp_path / "missing.env"))
    try:
        artifacts.event({"type": "state", "state": "READY"})
        line = artifacts.trajectory_path.read_text(encoding="utf-8").splitlines()
        assert len(line) == 1 and json.loads(line[0])["state"] == "READY"
    finally:
        artifacts.close()


def test_usage_truncation_is_explicit(tmp_path):
    artifacts = RunArtifacts(tmp_path / "runs", Redactor(tmp_path / "missing.env"))
    try:
        artifacts.event({"type": "model_response", "request": 1, "tool_count": 0, "content": "",
                         "usage": {f"token_{index}": index for index in range(51)}})
        event = json.loads(artifacts.trajectory_path.read_text(encoding="utf-8"))
        assert event["usage"]["_truncated"] is True
    finally:
        artifacts.close()
