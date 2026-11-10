import json
import traceback
from dataclasses import replace
from unittest.mock import Mock

import pytest

from evocode.models import ConfigError, LLMError, LLMProtocolError, LLMProvider, LLMTimeoutError, ModelConfig
from evocode.tools import Observation, ToolCall, create_default_registry


def config(profile="gpt", retries=0):
    return ModelConfig(
        profile=profile, base_url="https://gateway.example/v1", model_id=f"vendor/{profile}-model",
        api_key="dummy-secret-value", request_timeout_seconds=12.5,
        max_output_tokens=128000 if profile == "gpt" else 393216,
        reasoning_effort="medium" if profile == "gpt" else "high", max_retries=retries,
    )


@pytest.fixture
def read_spec():
    return [spec for spec in create_default_registry().get_specs() if spec["name"] == "read_file"]


def responses_call(arguments='{"path":"calculator.py"}'):
    return {"status": "completed", "output": [
        {"id": "reasoning-id", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
        {"id": "function-item-id", "type": "function_call", "call_id": "call-original-id",
         "name": "read_file", "arguments": arguments, "status": "completed"},
    ]}


def chat_call(arguments='{"path":"calculator.py"}'):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "reasoning_content": "opaque reasoning",
        "tool_calls": [{"id": "call-chat-id", "type": "function",
                        "function": {"name": "read_file", "arguments": arguments}}],
    }}]}


def test_gpt_responses_request_and_tool_result_roundtrip(read_spec):
    fake = Mock()
    fake.responses.return_value = responses_call()
    provider = LLMProvider(config(), client=fake)
    messages = [{"role": "user", "content": "read calculator"}]
    response = provider.generate(messages, read_spec)
    assert response.content == "" and response.usage is None
    assert response.tool_calls == [ToolCall("call-original-id", "read_file", {"path": "calculator.py"})]
    args = fake.responses.call_args.kwargs
    assert args["model"] == "openai/vendor/gpt-model"
    assert args["api_base"] == "https://gateway.example/v1"
    assert args["max_output_tokens"] == 128000 and args["reasoning"] == {"effort": "medium"}
    assert args["timeout"] == 12.5 and args["num_retries"] == 0 and args["max_retries"] == 0
    assert args["stream"] is False and args["store"] is False
    assert "max_tokens" not in args and "reasoning_effort" not in args
    assert args["tools"][0]["type"] == "function" and args["tools"][0]["name"] == "read_file"
    assert args["tools"][0]["parameters"] == read_spec[0]["parameters"]

    observation = Observation("call-original-id", "ok", "source code", {"truncated": False})
    followup = provider.tool_results(response, [observation])
    assert followup[0]["encrypted_content"] == "opaque"
    assert followup[1]["id"] == "function-item-id" and followup[1]["call_id"] == "call-original-id"
    assert followup[2]["type"] == "function_call_output" and followup[2]["call_id"] == "call-original-id"
    assert json.loads(followup[2]["output"])["content"] == "source code"
    fake.responses.return_value = {"status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}
    ], "usage": {"input_tokens": 4, "output_tokens": 5}}
    assert provider.generate(messages + followup, read_spec).content == "done"
    assert fake.responses.call_args.kwargs["input"][1:] == followup


def test_deepseek_chat_request_preserves_reasoning_and_tool_id(read_spec):
    fake = Mock()
    fake.completion.return_value = chat_call()
    provider = LLMProvider(config("deepseek"), client=fake)
    response = provider.generate([{"role": "user", "content": "read calculator"}], read_spec)
    assert response.content == "" and response.usage is None
    assert response.tool_calls == [ToolCall("call-chat-id", "read_file", {"path": "calculator.py"})]
    args = fake.completion.call_args.kwargs
    assert args["model"] == "openai/vendor/deepseek-model"
    assert args["api_base"] == "https://gateway.example/v1"
    assert args["max_tokens"] == 393216 and args["reasoning_effort"] == "high"
    assert "thinking" not in args and "reasoning" not in args and "max_output_tokens" not in args
    assert args["tools"][0]["function"]["name"] == "read_file"
    followup = provider.tool_results(response, [Observation("call-chat-id", "ok", "source", {})])
    assert followup[0]["reasoning_content"] == "opaque reasoning"
    assert followup[0]["tool_calls"][0]["id"] == "call-chat-id"
    assert followup[1]["role"] == "tool" and followup[1]["tool_call_id"] == "call-chat-id"
    assert json.loads(followup[1]["content"])["content"] == "source"


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
@pytest.mark.parametrize("arguments", ["{", "[]", "null", '{"x":NaN}'])
def test_invalid_tool_arguments_are_explicit_errors(profile, arguments):
    fake = Mock()
    if profile == "gpt":
        fake.responses.return_value = responses_call(arguments)
    else:
        fake.completion.return_value = chat_call(arguments)
    with pytest.raises(LLMProtocolError, match="JSON"):
        LLMProvider(config(profile), client=fake).generate([{"role": "user", "content": "test"}])


def test_invalid_json_exception_does_not_retain_original_arguments():
    fake = Mock()
    fake.responses.return_value = responses_call('{"value":"dummy-secret-value"')
    with pytest.raises(LLMProtocolError) as error:
        LLMProvider(config(), client=fake).generate([{"role": "user", "content": "test"}])
    assert error.value.__context__ is None
    assert "dummy-secret-value" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_text_responses_and_absent_usage(profile):
    fake = Mock()
    fake.responses.return_value = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "hello"}]}]}
    fake.completion.return_value = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    response = LLMProvider(config(profile), client=fake).generate([{"role": "user", "content": "test"}])
    assert response.content == "hello" and response.tool_calls == [] and response.usage is None


def test_mismatched_and_duplicate_call_ids_are_rejected():
    fake = Mock()
    fake.responses.return_value = responses_call()
    provider = LLMProvider(config(), client=fake)
    response = provider.generate([{"role": "user", "content": "test"}])
    with pytest.raises(LLMProtocolError, match="调用 ID"):
        provider.tool_results(response, [Observation("wrong-id", "ok", "", {})])
    duplicate = responses_call()
    duplicate["output"].append(dict(duplicate["output"][-1]))
    fake.responses.return_value = duplicate
    with pytest.raises(LLMProtocolError, match="重复"):
        provider.generate([{"role": "user", "content": "test"}])


class HttpFailure(Exception):
    status_code = 400


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_api_error_and_timeout_never_expose_secret(profile, caplog):
    fake = Mock()
    method = fake.responses if profile == "gpt" else fake.completion
    method.side_effect = HttpFailure("dummy-secret-value vendor/" + profile + "-model leaked in upstream body")
    provider = LLMProvider(config(profile), client=fake)
    with pytest.raises(LLMError, match="HTTP 400") as error:
        provider.generate([{"role": "user", "content": "test"}])
    formatted = "".join(traceback.format_exception(error.value))
    assert "dummy-secret-value" not in str(error.value) + formatted + caplog.text
    assert f"vendor/{profile}-model" not in str(error.value) + formatted + caplog.text
    assert error.value.__context__ is None
    method.side_effect = TimeoutError("dummy-secret-value")
    with pytest.raises(LLMTimeoutError) as timeout:
        provider.generate([{"role": "user", "content": "test"}])
    assert "dummy-secret-value" not in "".join(traceback.format_exception(timeout.value))


def test_configured_retries_are_bounded_without_sdk_retries():
    fake = Mock()
    fake.responses.side_effect = [TimeoutError("timeout"), {"output": []}]
    provider = LLMProvider(config(retries=1), client=fake)
    assert provider.generate([{"role": "user", "content": "test"}]).content == ""
    assert fake.responses.call_count == 2
    assert all(item.kwargs["num_retries"] == item.kwargs["max_retries"] == 0 for item in fake.responses.call_args_list)
    fake.responses.reset_mock()
    fake.responses.side_effect = HttpFailure("bad request")
    with pytest.raises(LLMError):
        provider.generate([{"role": "user", "content": "test"}])
    assert fake.responses.call_count == 1


def test_invalid_profile_never_falls_back_to_another_protocol():
    with pytest.raises(ConfigError, match="gpt 或 deepseek"):
        LLMProvider(replace(config(), profile="unsupported"), client=Mock())
