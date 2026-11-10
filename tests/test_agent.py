"""Offline controller tests: the simulated executor never runs edited code."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evocode.agent import AgentController, AgentStatus
from evocode.models import LLMError, LLMProtocolError, LLMProvider, LLMResponse, ModelConfig
from evocode.tasks.workspace import Workspace
from evocode.tools import ToolCall, create_default_registry


PROJECT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT / "examples" / "repos" / "add_bug"
CORRECT = "def add(a, b):\n    return a + b\n"
WRONG = "def add(a, b):\n    return a * b\n"
REASONING_SENTINEL = "private-reasoning-never-record"


def snapshot(directory: Path) -> dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


class ContentExecutor:
    """Read the current text to simulate a result; do not execute it."""

    def __init__(self, mode="normal"):
        self.mode = mode
        self.contents = []

    def __call__(self, repo_dir):
        content = (repo_dir / "calculator.py").read_text(encoding="utf-8")
        self.contents.append(content)
        if self.mode == "error":
            raise OSError("simulated executor failure")
        if self.mode == "timeout":
            raise subprocess.TimeoutExpired(["fake-test"], 2, output=b"partial failure")
        if self.mode == "no_tests":
            return subprocess.CompletedProcess(["fake-test"], 5, "no tests collected", "")
        if self.mode == "internal_error":
            return subprocess.CompletedProcess(["fake-test"], 3, "internal error", "")
        passed = "return a + b" in content
        return subprocess.CompletedProcess(
            ["fake-test"], 0 if passed else 1,
            "simulated tests passed" if passed else "simulated assertion failed", "",
        )


def model_config(profile):
    return ModelConfig(
        profile=profile, base_url="https://gateway.example/v1", model_id="fake-model",
        api_key="dummy-key-never-record", request_timeout_seconds=5, max_output_tokens=100,
        reasoning_effort="medium" if profile == "gpt" else "high", max_retries=0,
    )


def tool_response(profile, calls, content=""):
    if profile == "gpt":
        items = [{"type": "reasoning", "id": "reasoning-item", "encrypted_content": REASONING_SENTINEL}]
        items.extend({"type": "function_call", "id": f"item-{index}", "call_id": item.call_id,
                      "name": item.name, "arguments": json.dumps(item.arguments)}
                     for index, item in enumerate(calls))
        protocol = "responses"
    else:
        items = [{"role": "assistant", "content": content or None, "reasoning_content": REASONING_SENTINEL,
                  "tool_calls": [{"id": item.call_id, "type": "function", "function": {
                      "name": item.name, "arguments": json.dumps(item.arguments)}} for item in calls]}]
        protocol = "chat_completions"
    return LLMResponse(content, calls, assistant_items=items, protocol=protocol)


def text_response(profile, text):
    if profile == "gpt":
        items = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
        protocol = "responses"
    else:
        items = [{"role": "assistant", "content": text}]
        protocol = "chat_completions"
    return LLMResponse(text, [], assistant_items=items, protocol=protocol)


class ScriptedProvider:
    def __init__(self, profile, script):
        self.profile = profile
        self.script = list(script)
        self.requests = []
        self.result_batches = []
        # Only the already-implemented message conversion is used; the client
        # has no network capability and generate() below is entirely fake.
        self.converter = LLMProvider(model_config(profile), client=object())

    def generate(self, messages, specs):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(specs)))
        if not self.script:
            raise AssertionError("unexpected extra model request")
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def tool_results(self, response, observations):
        self.result_batches.append(list(observations))
        return self.converter.tool_results(response, observations)


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_offline_read_edit_and_independent_final_verification(profile, tmp_path):
    before = snapshot(SAMPLE)
    reader = ToolCall("read-id", "read_file", {"path": "calculator.py"})
    writer = ToolCall("write-id", "write_file", {"path": "calculator.py", "content": CORRECT})
    provider = ScriptedProvider(profile, [tool_response(profile, [reader]),
                                          tool_response(profile, [writer]), text_response(profile, "已完成修复")])
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        controller = AgentController(provider, create_default_registry(), max_steps=4, max_tool_calls=4)
        assert controller.state == AgentStatus.READY
        result = controller.run(issue="修复数值运算", workspace=workspace, test_executor=executor)
        assert result.status == controller.state == AgentStatus.SUCCEEDED
        assert result.model_requests == 3 and result.tool_calls == 3
        assert result.final_explanation == "已完成修复"
        assert result.last_verification.status == "ok"
        assert result.last_verification.metadata["exit_code"] == 0
        assert result.last_verification.metadata["test_outcome"] == "passed"
        assert len(executor.contents) == 1 and "return a + b" in executor.contents[0]
        assert "-    return a - b" in result.patch and "+    return a + b" in result.patch
        assert [event["state"] for event in result.events if event["type"] == "state"] == [
            "READY", "RUNNING", "VERIFYING", "SUCCEEDED",
        ]
        assert len(provider.result_batches) == 2
        assert provider.result_batches[0][0].call_id == "read-id"
        followup = provider.requests[1][0]
        if profile == "gpt":
            assert sum(item.get("type") == "function_call" and item.get("call_id") == "read-id"
                       for item in followup) == 1
            assert any(item.get("type") == "function_call_output" and item.get("call_id") == "read-id"
                       for item in followup)
            assert any(item.get("encrypted_content") == REASONING_SENTINEL for item in followup)
        else:
            assert sum(item.get("role") == "assistant" and item.get("tool_calls") is not None
                       for item in followup) == 1
            assert any(item.get("role") == "tool" and item.get("tool_call_id") == "read-id"
                       for item in followup)
            assert any(item.get("reasoning_content") == REASONING_SENTINEL for item in followup)
        assert REASONING_SENTINEL not in json.dumps(result.events)
        assert "dummy-key-never-record" not in json.dumps(result.events)
        assert snapshot(SAMPLE) == before

    assert snapshot(SAMPLE) == before
    clean = tmp_path / "clean"
    shutil.copytree(SAMPLE, clean, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    checked = subprocess.run(["git", "apply", "--check", "-"], cwd=clean,
                             input=result.patch.encode("utf-8"), capture_output=True)
    assert checked.returncode == 0, checked.stderr.decode(errors="replace")


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_failed_verification_reaches_next_model_request_and_can_be_repaired(profile):
    wrong = ToolCall("wrong", "write_file", {"path": "calculator.py", "content": WRONG})
    fixed = ToolCall("fixed", "write_file", {"path": "calculator.py", "content": CORRECT})
    provider = ScriptedProvider(profile, [tool_response(profile, [wrong]), text_response(profile, "我已修好"),
                                          tool_response(profile, [fixed]), text_response(profile, "修复完成")])
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=4, max_tool_calls=4).run(
            issue="修复计算问题", workspace=workspace, test_executor=executor,
        )
        assert result.status == AgentStatus.SUCCEEDED
        assert result.model_requests == 4 and result.tool_calls == 4
        assert len(executor.contents) == 2
        assert "return a * b" in executor.contents[0] and "return a + b" in executor.contents[1]
        feedback_request = provider.requests[2][0]
        assert feedback_request[-1]["role"] == "user"
        assert "simulated assertion failed" in feedback_request[-1]["content"]
        assert '"exit_code": 1' in feedback_request[-1]["content"]
        if profile == "gpt":
            assert sum(item.get("type") == "function_call_output" for item in feedback_request) == 1
        else:
            assert sum(item.get("role") == "tool" for item in feedback_request) == 1
        # tool_results is only used for model-issued calls, never verification.
        assert len(provider.result_batches) == 2
        checks = [event for event in result.events if event["type"] == "observation"
                  and event["origin"] == "verification"]
        assert [event["test_outcome"] for event in checks] == ["failed", "passed"]


def test_model_claim_without_repair_cannot_succeed():
    provider = ScriptedProvider("gpt", [text_response("gpt", "测试已通过")])
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=1, max_tool_calls=2).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.EXHAUSTED
    assert result.last_verification.metadata["test_outcome"] == "failed"
    assert result.model_requests == 1 and result.tool_calls == 1 and len(executor.contents) == 1
    assert len(provider.requests) == 1 and not result.patch


def test_passed_old_test_cannot_replace_final_verification():
    script = [
        tool_response("gpt", [ToolCall("fix", "write_file", {"path": "calculator.py", "content": CORRECT})]),
        tool_response("gpt", [ToolCall("old-test", "run_tests", {})]),
        tool_response("gpt", [ToolCall("break", "write_file", {"path": "calculator.py", "content": WRONG})]),
        text_response("gpt", "完成"),
    ]
    provider = ScriptedProvider("gpt", script)
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=4, max_tool_calls=4).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.EXHAUSTED
    assert len(executor.contents) == 2
    assert "return a + b" in executor.contents[0] and "return a * b" in executor.contents[1]
    assert provider.result_batches[1][0].metadata["test_outcome"] == "passed"
    assert result.last_verification.metadata["test_outcome"] == "failed"
    assert "+    return a * b" in result.patch and result.tool_calls == 4


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_tool_errors_are_returned_to_model_in_order(profile):
    calls = [ToolCall("unknown", "not_a_tool", {}), ToolCall("invalid", "read_file", {}),
             ToolCall("denied", "write_file", {"path": "test_calculator.py", "content": "changed"})]
    provider = ScriptedProvider(profile, [tool_response(profile, calls), text_response(profile, "完成")])
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=2, max_tool_calls=4).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
    assert result.status == AgentStatus.EXHAUSTED
    assert result.tool_calls == 4
    errors = provider.result_batches[0]
    assert [item.call_id for item in errors] == ["unknown", "invalid", "denied"]
    assert [item.metadata["error_code"] for item in errors] == [
        "unknown_tool", "invalid_arguments", "permission_denied",
    ]
    next_messages = provider.requests[1][0]
    if profile == "gpt":
        outputs = [item for item in next_messages if item.get("type") == "function_call_output"]
        assert [item["call_id"] for item in outputs] == ["unknown", "invalid", "denied"]
        assert [json.loads(item["output"])["metadata"]["error_code"] for item in outputs] == [
            "unknown_tool", "invalid_arguments", "permission_denied",
        ]
    else:
        outputs = [item for item in next_messages if item.get("role") == "tool"]
        assert [item["tool_call_id"] for item in outputs] == ["unknown", "invalid", "denied"]
        assert [json.loads(item["content"])["metadata"]["error_code"] for item in outputs] == [
            "unknown_tool", "invalid_arguments", "permission_denied",
        ]


def test_step_budget_stops_before_another_model_request():
    call = ToolCall("read", "read_file", {"path": "calculator.py"})
    provider = ScriptedProvider("gpt", [tool_response("gpt", [call]), tool_response("gpt", [call])])
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=2, max_tool_calls=5).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
    assert result.status == AgentStatus.EXHAUSTED and "轮数" in result.reason
    assert result.model_requests == 2 and result.tool_calls == 2 and len(provider.requests) == 2


def test_error_calls_use_budget_and_stop_before_another_request():
    calls = [ToolCall("unknown", "bad_tool", {}), ToolCall("invalid", "read_file", {})]
    provider = ScriptedProvider("gpt", [tool_response("gpt", calls)])
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=5, max_tool_calls=2).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
    assert result.status == AgentStatus.EXHAUSTED and "工具调用" in result.reason
    assert result.tool_calls == 2 and result.model_requests == 1
    assert len(provider.requests) == 1 and len(provider.result_batches[0]) == 2


def test_oversized_batch_never_partially_executes():
    calls = [ToolCall("write", "write_file", {"path": "calculator.py", "content": CORRECT}),
             ToolCall("read", "read_file", {"path": "calculator.py"})]
    provider = ScriptedProvider("gpt", [tool_response("gpt", calls)])
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=3, max_tool_calls=1).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
        assert "return a - b" in workspace.read_file("calculator.py")
    assert result.status == AgentStatus.EXHAUSTED and "批次" in result.reason
    assert result.model_requests == 1 and result.tool_calls == 0 and result.patch == ""
    assert not provider.result_batches and len(provider.requests) == 1


@pytest.mark.parametrize("mode,expected_status,expected_outcome", [
    ("normal", "ok", "failed"), ("no_tests", "ok", "no_tests"),
    ("internal_error", "ok", "internal_error"), ("error", "error", None), ("timeout", "timeout", None),
])
def test_unsuccessful_final_verification_never_succeeds(mode, expected_status, expected_outcome):
    provider = ScriptedProvider("gpt", [text_response("gpt", "完成")])
    executor = ContentExecutor(mode)
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=1, max_tool_calls=2).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.EXHAUSTED and len(executor.contents) == 1
    assert result.last_verification.status == expected_status
    assert result.last_verification.metadata.get("test_outcome") == expected_outcome


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
@pytest.mark.parametrize("mode,expected_status,expected_text", [
    ("no_tests", "ok", "no tests collected"),
    ("error", "error", "simulated executor failure"),
    ("timeout", "timeout", "partial failure"),
])
def test_unsuccessful_verification_is_plain_feedback_not_a_forged_tool_message(
    profile, mode, expected_status, expected_text,
):
    provider = ScriptedProvider(profile, [text_response(profile, "完成"), text_response(profile, "仍然完成")])
    executor = ContentExecutor(mode)
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=2, max_tool_calls=2).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.EXHAUSTED and len(executor.contents) == 2
    assert len(provider.requests) == 2 and not provider.result_batches
    feedback = provider.requests[1][0]
    assert feedback[-1]["role"] == "user" and expected_text in feedback[-1]["content"]
    assert f'"status": "{expected_status}"' in feedback[-1]["content"]
    assert not any(item.get("role") == "tool" or item.get("type") == "function_call_output"
                   for item in feedback)


def test_model_and_protocol_errors_stop_without_disclosing_private_data():
    secret = "dummy-key-never-record"
    provider = ScriptedProvider("gpt", [RuntimeError(f"provider failed {secret}")])
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.FAILED and result.model_requests == 1 and result.tool_calls == 0
    assert len(provider.requests) == 1 and not executor.contents
    assert secret not in result.reason and secret not in json.dumps(result.events)

    provider = ScriptedProvider("gpt", [LLMError(f"HTTP 401 secret={secret}")])
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
    assert result.status == AgentStatus.FAILED and "HTTP 401" in result.reason
    assert secret not in result.reason and secret not in json.dumps(result.events)

    duplicate = [ToolCall("same", "write_file", {"path": "calculator.py", "content": CORRECT}),
                 ToolCall("same", "read_file", {"path": "calculator.py"})]
    provider = ScriptedProvider("gpt", [tool_response("gpt", duplicate)])
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
    assert result.status == AgentStatus.FAILED and "ID" in result.reason
    assert result.tool_calls == 0 and result.patch == "" and not provider.result_batches


def test_tool_result_protocol_error_stops_and_keeps_already_written_patch():
    secret = "private-protocol-error"
    provider = ScriptedProvider("gpt", [
        tool_response("gpt", [ToolCall("write", "write_file", {"path": "calculator.py", "content": CORRECT})]),
        text_response("gpt", "不应请求"),
    ])

    def fail_conversion(_response, _observations):
        raise LLMProtocolError(secret)

    provider.tool_results = fail_conversion
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.FAILED and "模型消息" in result.reason
    assert result.model_requests == 1 and result.tool_calls == 1 and len(provider.requests) == 1
    assert "+    return a + b" in result.patch and not executor.contents
    assert secret not in result.reason and secret not in json.dumps(result.events)


def test_patch_export_failure_cannot_report_success(monkeypatch):
    provider = ScriptedProvider("gpt", [text_response("gpt", "完成")])

    def fail_export():
        raise OSError("private-path-and-credentials")

    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        workspace.write_file("calculator.py", CORRECT)
        monkeypatch.setattr(workspace, "export_patch", fail_export)
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=ContentExecutor(),
        )
    assert result.status == AgentStatus.FAILED and "导出工作区补丁失败" in result.reason
    assert result.last_verification.metadata["test_outcome"] == "passed" and result.patch == ""
    assert "private-path-and-credentials" not in result.reason
    assert "private-path-and-credentials" not in json.dumps(result.events)


def test_empty_model_response_is_failure_and_does_not_execute_tests():
    provider = ScriptedProvider("gpt", [text_response("gpt", "  ")])
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.FAILED and "有效文本" in result.reason
    assert result.model_requests == 1 and result.tool_calls == 0 and not executor.contents


def test_malformed_assistant_items_fail_before_verification():
    provider = ScriptedProvider("gpt", [LLMResponse("完成", [], assistant_items="invalid")])
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry()).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.FAILED and "格式" in result.reason
    assert result.model_requests == 1 and result.tool_calls == 0 and not executor.contents


@pytest.mark.parametrize("field,value", [
    ("max_steps", 0), ("max_steps", -1), ("max_steps", True), ("max_steps", 1.5),
    ("max_tool_calls", 0), ("max_tool_calls", -1), ("max_tool_calls", False), ("max_tool_calls", 1.5),
])
def test_budgets_must_be_positive_integers(field, value):
    with pytest.raises(ValueError, match=field):
        AgentController(ScriptedProvider("gpt", []), create_default_registry(), **{field: value})


def test_executor_is_required_and_never_defaults_to_host():
    provider = ScriptedProvider("gpt", [])
    controller = AgentController(provider, create_default_registry())
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        with pytest.raises(TypeError, match="test_executor"):
            controller.run(issue="修复问题", workspace=workspace)
        with pytest.raises(ValueError, match="test_executor"):
            controller.run(issue="修复问题", workspace=workspace, test_executor=None)
    assert not provider.requests


def test_assistant_text_arguments_and_test_output_do_not_enter_events():
    sensitive = "dummy-sensitive-payload"
    call = ToolCall("id-secret", "write_file", {"path": "calculator.py", "content": WRONG + f"# {sensitive}\n"})
    provider = ScriptedProvider("gpt", [tool_response("gpt", [call], content=sensitive),
                                        text_response("gpt", sensitive)])

    def executor(repo_dir):
        text = (repo_dir / "calculator.py").read_text(encoding="utf-8")
        return subprocess.CompletedProcess(["fake-test"], 1 if "return a + b" not in text else 0, sensitive, "")

    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=2, max_tool_calls=3).run(
            issue="修复问题", workspace=workspace, test_executor=executor,
        )
    assert result.status == AgentStatus.EXHAUSTED
    assert sensitive in result.last_verification.content and sensitive in result.patch
    assert sensitive not in json.dumps(result.events, ensure_ascii=False)
    assert REASONING_SENTINEL not in json.dumps(result.events, ensure_ascii=False)
