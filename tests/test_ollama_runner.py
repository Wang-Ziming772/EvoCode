"""Exercise profile-specific budgets and artifact metadata without a local server."""

import json
import sys

import httpx
import pytest

from evocode import cli, runner
from evocode.models import LLMEmptyResponseError, LLMIncompleteError, LLMResponse, OllamaProvider
from evocode.tools import ToolCall
from test_repair import FakeExecutor, FakeProvider, fake_backends, read_artifacts, repair_inputs


class FakeLocalProvider:
    def __init__(self, config):
        self.config = config
        self.runtime = {"ollama_version": "0.34.1", "model_digest": "digest", "actual_num_ctx": 131072}
        self.usage_totals = {"eval_count": 8}
        self.usage_observed_calls = {"eval_count": 1}
        self.last_thinking_present = True
        self.last_usage = {"eval_count": 8}

    def prepare_for_run(self):
        return []

    def generate(self, _messages, _tools):
        raise LLMIncompleteError("Ollama 响应未完整结束或达到输出上限，仅收到思考内容")


def add_local_settings(env):
    with env.open("a", encoding="utf-8") as file:
        file.write("\n" + "\n".join([
            "EVOCODE_QWEN4B_MODEL_ID=qwen3.5:4b-q8_0", "EVOCODE_QWEN4B_THINK=true",
            "EVOCODE_QWEN4B_NUM_CTX=131072", "EVOCODE_QWEN4B_MAX_OUTPUT_TOKENS=81920",
            "EVOCODE_QWEN4B_REQUEST_TIMEOUT_SECONDS=3600", "EVOCODE_QWEN4B_MAX_STEPS=200",
            "EVOCODE_QWEN4B_MAX_TOOL_CALLS=600", "EVOCODE_QWEN4B_MAX_RETRIES=0",
        ]))


@pytest.mark.parametrize("steps,calls,expected", [(None, None, (200, 600)), (20, 60, (20, 60))])
def test_local_budget_precedence_and_runtime_artifacts(repair_inputs, fake_backends, tmp_path, monkeypatch, steps, calls, expected):
    _repo, task, env = repair_inputs
    add_local_settings(env)
    monkeypatch.setattr(runner, "OllamaProvider", FakeLocalProvider)
    result = runner.run_repair(runner.RepairOptions(task=task, env_file=env, profile="qwen4b",
                                                   output_dir=tmp_path / "runs", max_steps=steps, max_tool_calls=calls))
    data, events, _patch = read_artifacts(result)
    assert result.exit_code == 1 and "输出上限" in data["reason"]
    assert (data["model"]["max_steps"], data["model"]["max_tool_calls"]) == expected
    assert data["model"]["think"] is True and data["model"]["num_ctx"] == 131072
    assert data["model"]["num_predict"] == 81920 and data["model"]["request_timeout_seconds"] == 3600
    assert data["model"]["ollama_version"] == "0.34.1" and data["model"]["model_digest"] == "digest"
    assert data["model"]["sampling"]["temperature"] == 0.6
    assert data["usage"]["totals"] == {"eval_count": 8}
    errors = [item for item in events if item["type"] == "model_error"]
    assert errors[0]["thinking_present"] is True and errors[0]["usage"]["eval_count"] == 8


@pytest.mark.parametrize("steps,calls,expected", [(None, None, (10, 30)), (20, 60, (20, 60))])
def test_missing_local_budgets_use_fallback_or_cli(repair_inputs, fake_backends, tmp_path, monkeypatch, steps, calls, expected):
    _repo, task, env = repair_inputs
    add_local_settings(env)
    lines = env.read_text(encoding="utf-8").splitlines()
    env.write_text("\n".join(line for line in lines if not line.startswith((
        "EVOCODE_QWEN4B_MAX_STEPS=", "EVOCODE_QWEN4B_MAX_TOOL_CALLS="))), encoding="utf-8")
    monkeypatch.setattr(runner, "OllamaProvider", FakeLocalProvider)
    result = runner.run_repair(runner.RepairOptions(task=task, env_file=env, profile="qwen4b",
                                                   output_dir=tmp_path / "runs", max_steps=steps, max_tool_calls=calls))
    data, _events, _patch = read_artifacts(result)
    assert result.exit_code == 1
    assert (data["model"]["max_steps"], data["model"]["max_tool_calls"]) == expected


def test_cli_passes_explicit_local_budgets(repair_inputs, fake_backends, tmp_path, monkeypatch):
    _repo, task, env = repair_inputs
    add_local_settings(env)
    monkeypatch.setattr(runner, "OllamaProvider", FakeLocalProvider)
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--profile", "qwen4b", "--max-steps", "20", "--max-tool-calls", "60",
                                     "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    path = next((tmp_path / "runs").iterdir()) / "result.json"
    model = json.loads(path.read_text(encoding="utf-8"))["model"]
    assert model["max_steps"] == 20 and model["max_tool_calls"] == 60


@pytest.mark.parametrize("profile,model_id,ctx,maximum", [
    ("qwen2b_q8", "qwen3.5:2b-q8_0", 131072, 8192),
    ("qwen2b_bf16", "qwen3.5:2b-bf16", 131072, 8192),
    ("qwen27b", "batiai/qwen3.8-27b:q2", 24576, 4096),
])
def test_cli_accepts_new_profiles_and_records_address(repair_inputs, fake_backends, tmp_path, monkeypatch,
                                                      profile, model_id, ctx, maximum):
    _repo, task, env = repair_inputs
    prefix = "EVOCODE_" + profile.upper() + "_"
    values = {"BASE_URL": "http://127.0.0.1:11436", "MODEL_ID": model_id, "THINK": "true",
              "NUM_CTX": ctx, "MAX_OUTPUT_TOKENS": maximum, "REQUEST_TIMEOUT_SECONDS": 600,
              "MAX_STEPS": 200, "MAX_TOOL_CALLS": 600, "MAX_RETRIES": 0}
    with env.open("a", encoding="utf-8") as stream:
        stream.write("\n" + "\n".join(f"{prefix}{key}={value}" for key, value in values.items()))
    monkeypatch.setattr(runner, "OllamaProvider", FakeLocalProvider)
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--profile", profile, "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1  # FakeLocalProvider deliberately raises an incomplete-response error.
    result = json.loads((next((tmp_path / "runs").iterdir()) / "result.json").read_text(encoding="utf-8"))
    assert result["model"]["profile"] == profile and result["model"]["model_id"] == model_id
    assert result["model"]["base_url"] == "http://127.0.0.1:11436"
    assert (result["model"]["num_ctx"], result["model"]["num_predict"]) == (ctx, maximum)


@pytest.mark.parametrize("recover_empty", [False, True])
def test_local_wait_interrupt_preserves_result_and_cleans_workspace(
    repair_inputs, fake_backends, tmp_path, monkeypatch, recover_empty,
):
    repo, task, env = repair_inputs
    add_local_settings(env)
    workspace_paths = []

    class TrackedWorkspace(runner.Workspace):
        def __enter__(self):
            workspace = super().__enter__()
            workspace_paths.append(workspace.path)
            return workspace

    class Interrupted(FakeLocalProvider):
        def __init__(self, config):
            super().__init__(config)
            self.calls = 0

        def generate(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse("", [ToolCall("edit", "write_file", {
                    "path": "calculator.py", "content": "def add(a, b):\n    return a + b\n",
                })])
            if recover_empty and self.calls == 2:
                raise LLMEmptyResponseError("Ollama 仅返回思考内容或空响应，没有可执行调用或最终文本")
            raise KeyboardInterrupt()

        def tool_results(self, _response, _observations):
            return [{"role": "assistant", "content": "edit"}, {"role": "user", "content": "ok"}]

    monkeypatch.setattr(runner, "OllamaProvider", Interrupted)
    monkeypatch.setattr(runner, "Workspace", TrackedWorkspace)
    result = runner.run_repair(runner.RepairOptions(task=task, env_file=env, profile="qwen4b",
                                                   output_dir=tmp_path / "runs"))
    data, events, patch = read_artifacts(result)
    assert result.exit_code == 130 and data["status"] == "INTERRUPTED" and b"return a + b" in patch
    assert events[-1]["type"] == "run_finished" and events[-1]["status"] == "INTERRUPTED"
    assert len([e for e in events if e["type"] == "model_retry"]) == int(recover_empty)
    assert len(workspace_paths) == 1 and not workspace_paths[0].exists()
    assert "return a - b" in (repo / "calculator.py").read_text(encoding="utf-8")


def test_cloud_profile_keeps_legacy_budget_defaults(repair_inputs, fake_backends, tmp_path, monkeypatch):
    _repo, task, env = repair_inputs
    real_controller = runner.AgentController
    seen = []

    def controller(*args, **kwargs):
        seen.append((kwargs["max_steps"], kwargs["max_tool_calls"]))
        return real_controller(*args, **kwargs)

    monkeypatch.setattr(runner, "AgentController", controller)
    result = runner.run_repair(runner.RepairOptions(task=task, env_file=env, output_dir=tmp_path / "runs"))
    assert result.exit_code == 0 and seen == [(10, 30)]


@pytest.mark.parametrize("overflow", [False, True])
def test_native_thinking_stays_out_of_terminal_and_artifacts(
    repair_inputs, fake_backends, tmp_path, monkeypatch, capsys, overflow,
):
    _repo, task, env = repair_inputs
    add_local_settings(env)
    if overflow:
        value = env.read_text(encoding="utf-8").replace("EVOCODE_QWEN4B_NUM_CTX=131072", "EVOCODE_QWEN4B_NUM_CTX=9000")
        value = value.replace("EVOCODE_QWEN4B_MAX_OUTPUT_TOKENS=81920", "EVOCODE_QWEN4B_MAX_OUTPUT_TOKENS=2000")
        env.write_text(value, encoding="utf-8")
    requests = []
    thought = "PRIVATE_THINKING_SENTINEL" * (300 if overflow else 1)

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            message = {"role": "assistant", "thinking": thought, "content": "", "tool_calls": [
                {"function": {"name": "write_file", "arguments": {
                    "path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"}}}]}
        elif overflow:
            return httpx.Response(400, json={"error": json.dumps({"error": {
                "type": "exceed_context_size_error", "message": "PRIVATE_ERROR_BODY",
                "n_prompt_tokens": 9500, "n_ctx": 9000,
            }})})
        else:
            message = {"role": "assistant", "thinking": "PRIVATE_FINAL_SENTINEL", "content": "done"}
        return httpx.Response(200, json={"model": "local", "done": True, "done_reason": "stop",
                                         "message": message, "prompt_eval_count": 123, "eval_count": 5})

    class NativeLocal(OllamaProvider):
        def __init__(self, config):
            super().__init__(config, transport=httpx.MockTransport(handler))

        def check_available(self):
            return self.runtime

    monkeypatch.setattr(runner, "OllamaProvider", NativeLocal)
    monkeypatch.setattr(sys, "argv", ["evocode", "repair", "--task", str(task), "--env-file", str(env),
                                     "--profile", "qwen4b", "--output-dir", str(tmp_path / "runs")])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == (1 if overflow else 0)
    run_dir = next((tmp_path / "runs").iterdir())
    result_text = (run_dir / "result.json").read_text(encoding="utf-8")
    trajectory_text = (run_dir / "trajectory.jsonl").read_text(encoding="utf-8")
    result, events = json.loads(result_text), [json.loads(line) for line in trajectory_text.splitlines()]
    output = capsys.readouterr()
    for text in (result_text, trajectory_text, output.out, output.err):
        assert "PRIVATE_THINKING_SENTINEL" not in text and "PRIVATE_FINAL_SENTINEL" not in text
        assert "PRIVATE_ERROR_BODY" not in text
        assert "_evocode_ollama_turn_id" not in text
    assert [e["usage"]["prompt_eval_count"] for e in events if e["type"] == "model_response"] == (
        [123] if overflow else [123, 123])
    if overflow:
        assert len(requests) == 2 and result["status"] == "FAILED"
        assert all(s in result["reason"] for s in ("超出上下文容量", "9500", "9000"))
        error = next(e for e in events if e["type"] == "model_error")
        assert error["input_estimate_bytes"] > len(thought)
        assert result["last_verification"] is None
    else:
        assert len(requests) == 2 and result["status"] == "SUCCEEDED"
        assert requests[1]["messages"][-2]["thinking"] == thought
        assert all(e["input_estimate_bytes"] > 0 for e in events if e["type"] == "model_response")
        assert result["last_verification"]["metadata"]["test_outcome"] == "passed"
