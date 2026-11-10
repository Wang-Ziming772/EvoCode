"""Exercise LiteLLM against a local mock gateway, never a real API."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from evocode.models import LLMError, LLMProvider, ModelConfig
from evocode.tools import Observation, create_default_registry


@pytest.fixture
def mock_gateway(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    requests = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            status, payload = responses.pop(0)
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, responses
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def config(base_url, profile, retries=0):
    return ModelConfig(
        profile=profile, base_url=base_url, model_id=f"vendor/{profile}-model",
        api_key="dummy-local-secret", request_timeout_seconds=5,
        max_output_tokens=128000 if profile == "gpt" else 393216,
        reasoning_effort="medium" if profile == "gpt" else "high", max_retries=retries,
    )


def response_data():
    return {"id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
            "model": "vendor/gpt-model", "output": [
                {"id": "rs_test", "type": "reasoning", "summary": [], "encrypted_content": "opaque"},
                {"id": "fc_test", "type": "function_call", "call_id": "call_gpt",
                 "name": "read_file", "arguments": '{"path":"calculator.py"}', "status": "completed"},
            ]}


def chat_data():
    return {"id": "chat_test", "object": "chat.completion", "created": 1,
            "model": "vendor/deepseek-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "reasoning_content": "opaque-thinking",
                    "tool_calls": [{"id": "call_deep", "type": "function", "function": {
                        "name": "read_file", "arguments": '{"path":"calculator.py"}'}}]}}]}


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_actual_litellm_request_fields_and_roundtrip(mock_gateway, profile):
    base, requests, responses = mock_gateway
    responses.extend([(200, response_data() if profile == "gpt" else chat_data()),
                      (200, response_data() if profile == "gpt" else chat_data())])
    provider = LLMProvider(config(base, profile))
    tools = [item for item in create_default_registry().get_specs() if item["name"] == "read_file"]
    messages = [{"role": "user", "content": "read calculator"}]
    first = provider.generate(messages, tools)
    expected_id = "call_gpt" if profile == "gpt" else "call_deep"
    assert first.tool_calls[0].call_id == expected_id
    assert first.usage is None
    followup = provider.tool_results(first, [Observation(expected_id, "ok", "content", {})])
    provider.generate(messages + followup, tools)

    assert len(requests) == 2
    path, body = requests[0]
    assert body["model"] == f"vendor/{profile}-model"
    assert body.get("stream", False) is False
    if profile == "gpt":
        assert path == "/v1/responses"
        assert body["max_output_tokens"] == 128000 and body["reasoning"] == {"effort": "medium"}
        assert body["tools"][0]["name"] == "read_file"
        assert "max_tokens" not in body and "reasoning_effort" not in body
        replay = requests[1][1]["input"]
        assert any(item.get("type") == "reasoning" and item.get("encrypted_content") == "opaque" for item in replay)
        assert any(item.get("type") == "function_call_output" and item.get("call_id") == expected_id for item in replay)
    else:
        assert path == "/v1/chat/completions"
        assert body["max_tokens"] == 393216 and body["reasoning_effort"] == "high"
        assert body["tools"][0]["function"]["name"] == "read_file"
        assert "thinking" not in body and "max_output_tokens" not in body
        replay = requests[1][1]["messages"]
        assert replay[1]["reasoning_content"] == "opaque-thinking"
        assert replay[2]["tool_call_id"] == expected_id


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_no_hidden_sdk_retries_on_error(mock_gateway, profile, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    base, requests, responses = mock_gateway
    responses.extend([(503, {"error": {"message": "dummy-local-secret", "type": "server_error"}})] * 4)
    provider = LLMProvider(config(base, profile, retries=0))
    with pytest.raises(LLMError) as exc:
        provider.generate([{"role": "user", "content": "hello"}])
    assert "dummy-local-secret" not in str(exc.value)
    captured = capsys.readouterr()
    assert "dummy-local-secret" not in caplog.text + captured.out + captured.err
    assert len(requests) == 1


def test_chat_usage_is_not_invented_and_explicit_zero_is_preserved(mock_gateway):
    base, _, responses = mock_gateway
    raw = chat_data()
    raw["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    responses.append((200, raw))
    response = LLMProvider(config(base, "deepseek")).generate([{"role": "user", "content": "hello"}])
    assert response.usage == raw["usage"]


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_configured_retry_makes_exactly_one_additional_http_request(mock_gateway, profile):
    base, requests, responses = mock_gateway
    responses.extend([
        (503, {"error": {"message": "temporary error", "type": "server_error"}}),
        (200, response_data() if profile == "gpt" else chat_data()),
    ])
    response = LLMProvider(config(base, profile, retries=1)).generate([{"role": "user", "content": "hello"}])
    assert response.tool_calls and len(requests) == 2


@pytest.mark.parametrize("profile", ["gpt", "deepseek"])
def test_gateway_error_reports_safe_reason_without_key(mock_gateway, profile):
    base, requests, responses = mock_gateway
    responses.append((400, {"error": {"message": "unsupported reasoning_effort; dummy-local-secret"}}))
    with pytest.raises(LLMError) as exc:
        LLMProvider(config(base, profile)).generate([{"role": "user", "content": "hello"}])
    assert "HTTP 400" in str(exc.value)
    assert "unsupported reasoning_effort" in str(exc.value)
    assert "dummy-local-secret" not in str(exc.value)
    assert len(requests) == 1
