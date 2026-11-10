import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

from evocode.models import LLMProtocolError, LLMProvider, ModelConfig

spec = importlib.util.spec_from_file_location(
    "smoke_llm", Path(__file__).resolve().parents[1] / "scripts" / "smoke_llm.py"
)
smoke_llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke_llm)


def config(profile, retries=0):
    return ModelConfig(profile=profile, base_url="https://gateway.example/v1", model_id="dummy-model",
                       api_key="dummy-secret-value", request_timeout_seconds=10,
                       max_output_tokens=128000 if profile == "gpt" else 393216,
                       reasoning_effort="medium" if profile == "gpt" else "high", max_retries=retries)


def first_response(profile, *, name="read_file", arguments='{"path":"calculator.py"}'):
    if profile == "gpt":
        return {"status": "completed", "output": [{"type": "function_call", "id": "fc-one", "call_id": "call-one",
            "name": name, "arguments": arguments}]}
    return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
        "reasoning_content": "opaque", "tool_calls": [{"id": "call-one", "type": "function",
            "function": {"name": name, "arguments": arguments}}]}}]}


def final_response(profile, text="The function subtracts."):
    if profile == "gpt":
        return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": text}]}]}
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


def prepare(monkeypatch, profile, first, second=None, retries=0):
    monkeypatch.setattr(smoke_llm, "load_model_config", lambda *_args: config(profile, retries))
    fake = Mock()
    method = fake.responses if profile == "gpt" else fake.completion
    method.side_effect = [first] + ([second] if second is not None else [])
    return fake, method, lambda cfg: LLMProvider(cfg, client=fake)


def snapshot(directory: Path):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_fixed_smoke_roundtrip_uses_only_read_file_and_two_requests(monkeypatch, profile):
    source = smoke_llm.PROJECT / "examples" / "repos" / "add_bug"
    before = snapshot(source)
    fake, method, factory = prepare(monkeypatch, profile, first_response(profile), final_response(profile))
    assert smoke_llm.run_smoke(profile, provider_factory=factory) == "The function subtracts."
    assert method.call_count == 2
    first_request, second_request = [item.kwargs for item in method.call_args_list]
    if profile == "gpt":
        assert [tool["name"] for tool in first_request["tools"]] == ["read_file"]
        assert any(item.get("type") == "function_call_output" and item.get("call_id") == "call-one"
                   for item in second_request["input"])
    else:
        assert [tool["function"]["name"] for tool in first_request["tools"]] == ["read_file"]
        assert any(item.get("role") == "tool" and item.get("tool_call_id") == "call-one"
                   for item in second_request["messages"])
    assert snapshot(source) == before


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
@pytest.mark.parametrize(("name", "arguments"), [
    ("write_file", '{"path":"calculator.py","content":"changed"}'),
    ("read_file", '{"path":"../outside"}'),
    ("read_file", '{"path":"calculator.py","extra":1}'),
])
def test_smoke_rejects_unapproved_tools_before_second_request(monkeypatch, profile, name, arguments):
    _, method, factory = prepare(monkeypatch, profile, first_response(profile, name=name, arguments=arguments))
    with pytest.raises(smoke_llm.SmokeError, match="不允许"):
        smoke_llm.run_smoke(profile, provider_factory=factory)
    assert method.call_count == 1


def test_smoke_rejects_missing_invalid_or_repeated_calls(monkeypatch):
    _, method, factory = prepare(monkeypatch, "gpt", final_response("gpt"))
    with pytest.raises(smoke_llm.SmokeError, match="没有按要求"):
        smoke_llm.run_smoke("gpt", provider_factory=factory)
    assert method.call_count == 1
    _, method, factory = prepare(monkeypatch, "gpt", first_response("gpt", arguments="{"))
    with pytest.raises(LLMProtocolError, match="JSON"):
        smoke_llm.run_smoke("gpt", provider_factory=factory)
    assert method.call_count == 1
    _, method, factory = prepare(monkeypatch, "gpt", first_response("gpt"), first_response("gpt"))
    with pytest.raises(smoke_llm.SmokeError, match="仍要求"):
        smoke_llm.run_smoke("gpt", provider_factory=factory)
    assert method.call_count == 2


def test_smoke_enforces_retry_limit_and_redacts_output(monkeypatch):
    _, method, factory = prepare(monkeypatch, "gpt", first_response("gpt"), retries=1)
    with pytest.raises(smoke_llm.SmokeError, match="MAX_RETRIES"):
        smoke_llm.run_smoke("gpt", provider_factory=factory)
    assert method.call_count == 0
    _, method, factory = prepare(monkeypatch, "gpt", first_response("gpt"),
                                 final_response("gpt", "dummy-secret-value"))
    assert smoke_llm.run_smoke("gpt", provider_factory=factory) == "[REDACTED]"
    assert method.call_count == 2
