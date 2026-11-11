"""Offline tests for local configuration and the native Ollama protocol."""

import json
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from evocode.models import ollama
from evocode.models import (ConfigError, LLMContextError, LLMEmptyResponseError, LLMIncompleteError, LLMLocalError,
                            OllamaConfig, OllamaProvider, load_model_config)
from evocode.models.provider import OllamaProtocolError, OllamaTimeoutError
from evocode.tools import Observation


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("EVOCODE_"):
            monkeypatch.delenv(key, raising=False)
    path = tmp_path / "local.env"
    lines = ["EVOCODE_DEFAULT_PROFILE=qwen4b", "EVOCODE_OLLAMA_BASE_URL=http://127.0.0.1:11434",
             "EVOCODE_OLLAMA_KEEP_ALIVE=5m", "EVOCODE_GPT_API_KEY=", "EVOCODE_BASE_URL=invalid"]
    profiles = (("QWEN4B", "qwen3.5:4b-q8_0", 131072, 81920, 3600),
                ("QWEN9B", "qwen3.5:9b-q4_K_M", 131072, 81920, 3600),
                ("QWEN2B_Q8", "qwen3.5:2b-q8_0", 131072, 8192, 600),
                ("QWEN2B_BF16", "qwen3.5:2b-bf16", 131072, 8192, 600),
                ("QWEN27B", "batiai/qwen3.8-27b:q2", 24576, 4096, 600))
    for profile, model, ctx, maximum, timeout in profiles:
        lines.extend([f"EVOCODE_{profile}_MODEL_ID={model}", f"EVOCODE_{profile}_THINK=true",
                      f"EVOCODE_{profile}_NUM_CTX={ctx}", f"EVOCODE_{profile}_MAX_OUTPUT_TOKENS={maximum}",
                      f"EVOCODE_{profile}_REQUEST_TIMEOUT_SECONDS={timeout}", f"EVOCODE_{profile}_MAX_STEPS=200",
                      f"EVOCODE_{profile}_MAX_TOOL_CALLS=600", f"EVOCODE_{profile}_MAX_RETRIES=0"])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def config(path, profile="qwen4b") -> OllamaConfig:
    result = load_model_config(profile, path)
    assert isinstance(result, OllamaConfig)
    return result


def native_response(message, *, reason="stop", done=True, **usage):
    return {"model": "local", "message": {"role": "assistant", **message},
            "done": done, "done_reason": reason, **usage}


def test_local_profiles_are_isolated_and_environment_wins(local_env, monkeypatch):
    assert load_model_config(env_file=local_env).profile == "qwen4b"
    assert config(local_env, "qwen9b").model_id == "qwen3.5:9b-q4_K_M"
    monkeypatch.setenv("EVOCODE_QWEN4B_THINK", "false")
    monkeypatch.setenv("EVOCODE_QWEN4B_NUM_CTX", "262144")
    local = config(local_env)
    assert local.think is False and local.num_ctx == 262144
    assert local.max_steps == 200 and local.max_tool_calls == 600
    assert config(local_env, "qwen9b").think is True
    monkeypatch.setenv("EVOCODE_QWEN9B_MAX_STEPS", "333")
    assert config(local_env, "qwen9b").max_steps == 333 and config(local_env).max_steps == 200


@pytest.mark.parametrize("profile,model,ctx,maximum", [
    ("qwen2b_q8", "qwen3.5:2b-q8_0", 131072, 8192),
    ("qwen2b_bf16", "qwen3.5:2b-bf16", 131072, 8192),
    ("qwen27b", "batiai/qwen3.8-27b:q2", 24576, 4096),
])
def test_new_profiles_use_exact_native_request_parameters(local_env, profile, model, ctx, maximum):
    captured = []
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        captured.append(json.loads(request.content))
        assert request.extensions["timeout"]["read"] == 600
        return httpx.Response(200, json=native_response({"content": "ok"}, prompt_eval_count=17))

    selected = config(local_env, profile)
    assert selected.model_id == model and selected.think is True
    assert selected.num_ctx == ctx and selected.max_output_tokens == maximum
    assert selected.request_timeout_seconds == 600 and selected.max_retries == 0
    assert (selected.max_steps, selected.max_tool_calls) == (200, 600)
    provider = OllamaProvider(selected, transport=httpx.MockTransport(handler))
    response = provider.generate([{"role": "user", "content": "test"}], [])
    assert response.usage["prompt_eval_count"] == 17
    assert captured[0]["model"] == model and captured[0]["think"] is True
    assert captured[0]["options"] == {"num_ctx": ctx, "num_predict": maximum, "temperature": 0.6,
                                      "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                                      "presence_penalty": 0.0, "repeat_penalty": 1.0}


def test_profile_address_precedence_and_isolation(local_env, monkeypatch):
    with local_env.open("a", encoding="utf-8") as stream:
        stream.write("\nEVOCODE_QWEN27B_BASE_URL=http://127.0.0.1:11436\n")
    monkeypatch.setenv("EVOCODE_OLLAMA_BASE_URL", "http://127.0.0.1:11450")
    assert config(local_env, "qwen27b").base_url == "http://127.0.0.1:11436"
    assert config(local_env, "qwen2b_q8").base_url == "http://127.0.0.1:11450"
    monkeypatch.setenv("EVOCODE_QWEN27B_BASE_URL", "http://127.0.0.1:11437")
    assert config(local_env, "qwen27b").base_url == "http://127.0.0.1:11437"
    monkeypatch.setenv("EVOCODE_QWEN27B_BASE_URL", "not-an-address")
    with pytest.raises(ConfigError, match="QWEN27B_BASE_URL"):
        config(local_env, "qwen27b")
    assert config(local_env, "qwen2b_bf16").base_url == "http://127.0.0.1:11450"


def test_local_address_default_when_both_settings_missing(local_env, monkeypatch):
    lines = local_env.read_text(encoding="utf-8").splitlines()
    local_env.write_text("\n".join(line for line in lines if not line.startswith("EVOCODE_OLLAMA_BASE_URL=")), encoding="utf-8")
    monkeypatch.delenv("EVOCODE_OLLAMA_BASE_URL", raising=False)
    assert config(local_env, "qwen27b").base_url == "http://127.0.0.1:11434"


def test_missing_other_profile_does_not_block_selected_profile(local_env):
    lines = local_env.read_text(encoding="utf-8").splitlines()
    local_env.write_text("\n".join(line for line in lines if not line.startswith("EVOCODE_QWEN2B_BF16_")), encoding="utf-8")
    assert config(local_env, "qwen27b").model_id == "batiai/qwen3.8-27b:q2"
    with pytest.raises(ConfigError):
        config(local_env, "qwen2b_bf16")


def test_missing_model_does_not_fall_back_to_common_address(local_env, monkeypatch):
    monkeypatch.setenv("EVOCODE_QWEN27B_BASE_URL", "http://127.0.0.1:11436")
    hosts = []
    def handler(request):
        hosts.append(str(request.url))
        return httpx.Response(200, json={"version": "test"} if request.url.path == "/api/version" else {"models": []})
    provider = OllamaProvider(config(local_env, "qwen27b"), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMLocalError, match="不存在"):
        provider.prepare_for_run()
    assert len(hosts) == 2 and all(url.startswith("http://127.0.0.1:11436/") for url in hosts)


def test_prepare_releases_other_models_across_configured_local_services(local_env, monkeypatch):
    monkeypatch.setenv("EVOCODE_QWEN27B_BASE_URL", "http://127.0.0.1:11436/")
    monkeypatch.setenv("EVOCODE_QWEN2B_Q8_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("EVOCODE_OLLAMA_UNLOAD_BASE_URLS", "http://127.0.0.1:11435")
    selected = config(local_env, "qwen27b")
    resident = {
        11436: [{"name": selected.model_id, "digest": "target"}, {"name": "local-old"}],
        11434: [{"name": "other-port-old"}],
    }
    unloads = []
    checks = []

    def handler(request):
        port = request.url.port
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "test"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": selected.model_id, "digest": "target"}]})
        if path == "/api/ps":
            checks.append(port)
            if port == 11435:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(200, json={"models": resident[port]})
        assert path == "/api/generate" and request.method == "POST"
        body = json.loads(request.content)
        assert body == {"model": body["model"], "keep_alive": 0, "stream": False}
        unloads.append((port, body["model"]))
        resident[port] = [model for model in resident[port] if model["name"] != body["model"]]
        return httpx.Response(200, json={"done": True, "done_reason": "unload"})

    provider = OllamaProvider(selected, transport=httpx.MockTransport(handler))
    released = provider.prepare_for_run()
    assert unloads == [(11436, "local-old"), (11434, "other-port-old")]
    assert checks == [11436, 11436, 11434, 11434, 11435]
    assert resident[11436] == [{"name": selected.model_id, "digest": "target"}]
    assert released == [{"base_url": selected.base_url, "model_id": "local-old"},
                        {"base_url": "http://127.0.0.1:11434", "model_id": "other-port-old"}]


def test_prepare_reports_models_that_are_still_resident(local_env, monkeypatch):
    selected = config(local_env)
    monkeypatch.setattr(ollama, "_UNLOAD_WAIT_SECONDS", 0)

    def handler(request):
        responses = {"/api/version": {"version": "test"},
                     "/api/tags": {"models": [{"name": selected.model_id}]},
                     "/api/ps": {"models": [{"name": "still-loaded"}]},
                     "/api/generate": {"done": True, "done_reason": "unload"}}
        return httpx.Response(200, json=responses[request.url.path])

    provider = OllamaProvider(selected, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMLocalError, match="尚未卸载.*still-loaded"):
        provider.prepare_for_run()


@pytest.mark.parametrize("key,value", [("THINK", "maybe"), ("NUM_CTX", "0"),
                                         ("MAX_OUTPUT_TOKENS", "24576"), ("BASE_URL", "")])
def test_invalid_selected_new_profile_does_not_affect_other_profiles(local_env, monkeypatch, key, value):
    monkeypatch.setenv("EVOCODE_QWEN27B_" + key, value)
    with pytest.raises(ConfigError):
        config(local_env, "qwen27b")
    assert config(local_env, "qwen2b_q8").model_id == "qwen3.5:2b-q8_0"


def test_false_thinking_is_sent_as_boolean(local_env, monkeypatch):
    monkeypatch.setenv("EVOCODE_QWEN4B_THINK", "false")
    requests = []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=native_response({"content": "done"}))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    response = provider.generate([{"role": "user", "content": "test"}], [])
    assert requests[0]["think"] is False
    assert response.usage is None


@pytest.mark.parametrize("key,value", [
    ("THINK", "maybe"), ("NUM_CTX", "0"), ("MAX_OUTPUT_TOKENS", "131072"),
    ("MAX_STEPS", "0"), ("MAX_TOOL_CALLS", "-1"), ("MAX_RETRIES", "-1"),
    ("REQUEST_TIMEOUT_SECONDS", "nan"), ("MODEL_ID", ""),
])
def test_invalid_local_config_is_rejected(local_env, monkeypatch, key, value):
    monkeypatch.setenv("EVOCODE_QWEN4B_" + key, value)
    with pytest.raises(ConfigError):
        load_model_config("qwen4b", local_env)
    assert config(local_env, "qwen9b").profile == "qwen9b"


def test_native_multi_call_roundtrip_replays_only_latest_thinking(local_env):
    requests = []
    responses = [
        native_response({"content": "", "thinking": "private-first", "tool_calls": [
            {"function": {"index": 0, "name": "read_file", "arguments": {"path": "a.py"}}},
            {"function": {"index": 1, "name": "read_file", "arguments": {"path": "a.py"}}},
        ]}, prompt_eval_count=10, eval_count=4, total_duration=100),
        native_response({"content": "", "thinking": "private-second", "tool_calls": [
            {"function": {"index": 0, "name": "read_file", "arguments": '{"path":"a.py"}'}},
        ]}, prompt_eval_count=20, eval_count=8),
        native_response({"content": "", "thinking": "private-third", "tool_calls": [
            {"function": {"index": 0, "name": "read_file", "arguments": {"path": "a.py"}}},
        ]}, prompt_eval_count=30, eval_count=16),
        native_response({"content": "完成", "thinking": "private-final"}, eval_count=3),
    ]

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b-q8_0", "context_length": 131072}]})
        assert request.url.path == "/api/chat"
        requests.append(json.loads(request.content))
        assert request.extensions["timeout"]["read"] == 3600
        assert "authorization" not in request.headers
        return httpx.Response(200, json=responses.pop(0))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    with provider._client(5) as client:
        assert client.trust_env is False
    specs = [{"name": "read_file", "description": "read", "parameters": {"type": "object"}}]
    messages = [{"role": "user", "content": "read"}]
    first = provider.generate(messages, specs)
    assert first.thinking_present is True and first.content == ""
    assert len(first.tool_calls) == 2 and first.tool_calls[0].call_id != first.tool_calls[1].call_id
    observations = [Observation(first.tool_calls[1].call_id, "ok", "B"), Observation(first.tool_calls[0].call_id, "ok", "A")]
    followup = provider.tool_results(first, observations)
    assert "thinking" not in followup[0] and "private-first" not in json.dumps(followup)
    assert [x["tool_name"] for x in followup[1:]] == ["read_file", "read_file"]
    assert json.loads(followup[1]["content"])["content"] == "A"
    assert json.loads(followup[2]["content"])["content"] == "B"
    messages.extend(followup)
    second = provider.generate(messages, specs)
    messages.extend(provider.tool_results(second, [Observation(second.tool_calls[0].call_id, "ok", "C")]))
    third = provider.generate(messages, specs)
    messages.extend(provider.tool_results(third, [Observation(third.tool_calls[0].call_id, "ok", "D")]))
    final = provider.generate(messages, specs)
    assert final.content == "完成" and final.thinking_present is True
    assert requests[1]["messages"][1]["thinking"] == "private-first"
    assert "thinking" not in requests[2]["messages"][1]
    assert requests[2]["messages"][-2]["thinking"] == "private-second"
    assistants = [m for m in requests[3]["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 3
    assert all("thinking" not in m for m in assistants[:-1])
    assert assistants[-1]["thinking"] == "private-third"
    assert len([m for m in requests[3]["messages"] if m["role"] == "tool"]) == 4
    assert all("_evocode_ollama_turn_id" not in m for r in requests for m in r["messages"])
    assert "private-" not in json.dumps(messages)
    assert provider._turn_thinking == {}
    payload = requests[0]
    assert payload["think"] is True and payload["stream"] is False
    assert payload["truncate"] is False and payload["shift"] is False
    assert payload["options"] == {"num_ctx": 131072, "num_predict": 81920, "temperature": 0.6,
                                  "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                                  "presence_penalty": 0.0, "repeat_penalty": 1.0}
    assert provider.usage_totals == {"prompt_eval_count": 60, "eval_count": 31, "total_duration": 100}
    assert provider.usage_observed_calls["prompt_eval_count"] == 3


def test_user_boundary_retains_tools_but_forgets_earlier_thinking(local_env):
    requests = []
    def call(thinking):
        return native_response({"content": "", "thinking": thinking, "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]})
    responses = [call("old-private"), call("new-private"), native_response({"content": "done"})]

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "first"}]
    first = provider.generate(messages, [])
    messages.extend(provider.tool_results(first, [Observation(first.tool_calls[0].call_id, "ok", "old result")]))
    messages.append({"role": "user", "content": "follow up"})
    second = provider.generate(messages, [])
    assert "old-private" not in json.dumps(requests[-1])
    assert [m["role"] for m in requests[-1]["messages"]] == ["user", "assistant", "tool", "user"]
    assert not provider._turn_thinking
    messages.extend(provider.tool_results(second, [Observation(second.tool_calls[0].call_id, "ok", "new result")]))
    provider.generate(messages, [])
    assistants = [m for m in requests[-1]["messages"] if m["role"] == "assistant"]
    assert "thinking" not in assistants[0] and assistants[1]["thinking"] == "new-private"
    assert all("_evocode_ollama_turn_id" not in m for m in requests[-1]["messages"])


def test_new_session_forgets_old_turn_even_with_identical_tools(local_env):
    requests = []
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=native_response({"thinking": "secret-old", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "same task"}]
    response = provider.generate(messages, [])
    messages.extend(provider.tool_results(response, [Observation(response.tool_calls[0].call_id, "ok", "ok")]))
    assert len(provider._turn_thinking) == 1
    provider.generate([{"role": "user", "content": "same task"}], [])
    assert not provider._turn_thinking and "secret-old" not in json.dumps(requests[-1])
    provider.generate(messages, [])
    assert "secret-old" not in json.dumps(requests[-1])
    assert [m["role"] for m in requests[-1]["messages"]] == ["user", "assistant", "tool"]


def test_estimate_includes_transmitted_thinking_without_internal_marker(local_env):
    requests = []
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            return httpx.Response(200, json=native_response({"content": "done"}, prompt_eval_count=2300))
        return httpx.Response(200, json=native_response({"thinking": "p" * 6000, "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}))

    small = replace(config(local_env), num_ctx=9000, max_output_tokens=2000)
    provider = OllamaProvider(small, transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "read"}]
    response = provider.generate(messages, [])
    messages.extend(provider.tool_results(response, [Observation(response.tool_calls[0].call_id, "ok", "ok")]))
    final = provider.generate(messages, [])
    assert final.content == "done" and final.usage["prompt_eval_count"] == 2300
    assert provider.last_input_estimate_bytes + small.max_output_tokens > small.num_ctx
    assert requests[1]["messages"][1]["thinking"] == "p" * 6000
    assert "_evocode_ollama_turn_id" not in requests[1]["messages"][1]


def test_context_estimate_excludes_discarded_old_thinking(local_env):
    requests = []
    responses = [
        native_response({"thinking": "old-private-" * 250, "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}),
        native_response({"thinking": "new-private-" * 250, "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}),
        native_response({"content": "done"}),
    ]
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    provider = OllamaProvider(replace(config(local_env), num_ctx=9000, max_output_tokens=2000),
                              transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "read"}]
    for _ in range(2):
        response = provider.generate(messages, [])
        messages.extend(provider.tool_results(response, [Observation(response.tool_calls[0].call_id, "ok", "ok")]))
    provider.generate(messages, [])
    native = requests[-1]["messages"]
    assistants = [m for m in native if m["role"] == "assistant"]
    assert len(assistants) == 2 and "thinking" not in assistants[0]
    assert assistants[1]["thinking"] == "new-private-" * 250
    estimate = provider.last_input_estimate_bytes
    assert estimate == provider._input_estimate_bytes(native, [])
    assistants[0]["thinking"] = "old-private-" * 250
    assert provider._input_estimate_bytes(native, []) > estimate


def test_assistant_message_tampering_does_not_reassociate_thinking(local_env):
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=native_response({"thinking": "private", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "read"}]
    response = provider.generate(messages, [])
    messages.extend(provider.tool_results(response, [Observation(response.tool_calls[0].call_id, "ok", "ok")]))
    messages[1]["tool_calls"][0]["function"]["arguments"]["path"] = "other.py"
    with pytest.raises(OllamaProtocolError, match="不匹配"):
        provider.generate(messages, [])


def test_think_false_tool_roundtrip_has_no_thinking(local_env, monkeypatch):
    monkeypatch.setenv("EVOCODE_QWEN4B_THINK", "false")
    requests = []
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=native_response({"tool_calls": [
                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}))
        return httpx.Response(200, json=native_response({"content": "done"}))
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "read"}]
    response = provider.generate(messages, [])
    messages.extend(provider.tool_results(response, [Observation(response.tool_calls[0].call_id, "ok", "ok")]))
    provider.generate(messages, [])
    assert all(request["think"] is False for request in requests)
    assert all("thinking" not in m and "_evocode_ollama_turn_id" not in m
               for m in requests[-1]["messages"])


def test_empty_response_does_not_replace_an_accepted_tool_turn(local_env):
    requests = []
    responses = [
        native_response({"thinking": "accepted-private", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]}),
        native_response({"thinking": "rejected-private", "content": ""}),
        native_response({"content": "done"}),
    ]
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    messages = [{"role": "user", "content": "read"}]
    response = provider.generate(messages, [])
    messages.extend(provider.tool_results(response, [Observation(response.tool_calls[0].call_id, "ok", "ok")]))
    with pytest.raises(LLMEmptyResponseError):
        provider.generate(messages, [])
    provider.generate(messages, [])
    assert requests[-1]["messages"][1]["thinking"] == "accepted-private"
    assert "rejected-private" not in json.dumps(requests[-1])
    assert [m["role"] for m in requests[-1]["messages"]] == ["user", "assistant", "tool"]


def test_controller_failed_verification_starts_new_user_turn(local_env):
    from evocode.agent import AgentController, AgentStatus
    from evocode.tasks.workspace import Workspace
    from evocode.tools import create_default_registry
    from test_agent import ContentExecutor, CORRECT, SAMPLE

    def tool(name, arguments, thinking):
        return native_response({"thinking": thinking, "tool_calls": [
            {"function": {"name": name, "arguments": arguments}}]})
    responses = [tool("read_file", {"path": "calculator.py"}, "first-private"),
                 native_response({"content": "done"}),
                 tool("write_file", {"path": "calculator.py", "content": CORRECT}, "second-private"),
                 native_response({"content": "done"})]
    requests = []
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=5, max_tool_calls=5).run(
            issue="修复加法", workspace=workspace, test_executor=executor)
    assert result.status == AgentStatus.SUCCEEDED and result.tool_calls == 4
    assert len(executor.contents) == 2 and result.last_verification.metadata["test_outcome"] == "passed"
    assert requests[2]["messages"][-1]["role"] == "user"
    assert "first-private" not in json.dumps(requests[2])
    assistants = [m for m in requests[3]["messages"] if m["role"] == "assistant" and m.get("tool_calls")]
    assert "thinking" not in assistants[0] and assistants[1]["thinking"] == "second-private"
    assert len([m for m in requests[3]["messages"] if m["role"] == "tool"]) == 2


@pytest.mark.parametrize("message,done,reason,error", [
    ({"content": "", "thinking": "private"}, True, "length", LLMIncompleteError),
    ({"content": "partial"}, False, "stop", LLMIncompleteError),
    ({"content": ""}, True, "stop", LLMEmptyResponseError),
    ({"content": "  ", "thinking": "private"}, True, "stop", LLMEmptyResponseError),
    ({"content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": '{bad'}}]}, True, "stop", OllamaProtocolError),
    ({"content": "<think>private</think>answer"}, True, "stop", OllamaProtocolError),
])
def test_incomplete_or_malformed_responses_are_rejected(local_env, message, done, reason, error):
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=native_response(message, done=done, reason=reason, eval_count=8))
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    with pytest.raises(error):
        provider.generate([{"role": "user", "content": "test"}], [])
    assert provider.usage_totals["eval_count"] == 8


@pytest.mark.parametrize("num_ctx", [131072, 262144])
def test_byte_estimate_does_not_block_or_shorten_requests(local_env, monkeypatch, num_ctx):
    monkeypatch.setenv("EVOCODE_QWEN4B_NUM_CTX", str(num_ctx))
    requests = []

    def handle(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b-q8_0", "context_length": num_ctx}]})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=native_response({"content": "ok"}, prompt_eval_count=10000))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handle))
    messages = [{"role": "user", "content": "x" * (num_ctx - 60000)}]
    provider.generate(messages, [])
    assert provider.last_input_estimate_bytes + 81920 > num_ctx
    assert requests[0]["messages"] == messages
    assert requests[0]["options"]["num_ctx"] == num_ctx
    assert requests[0]["options"]["num_predict"] == 81920
    assert requests[0]["truncate"] is False and requests[0]["shift"] is False
    assert provider.runtime["actual_num_ctx"] == num_ctx


@pytest.mark.parametrize("form", ["nested", "direct", "other"])
def test_server_capacity_errors_are_classified_without_exposing_error_body(local_env, form):
    error = {"type": "exceed_context_size_error", "message": "PRIVATE_BODY",
             "n_prompt_tokens": 48016, "n_ctx": 24576}
    payload = {"error": json.dumps({"error": error}) if form == "nested" else error}
    if form == "other":
        payload = {"error": "PRIVATE_BODY: invalid option"}
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(400, json=payload)

    provider = OllamaProvider(replace(config(local_env, "qwen27b"), max_retries=2),
                              transport=httpx.MockTransport(handler))
    expected = LLMLocalError if form == "other" else LLMContextError
    with pytest.raises(expected) as exc:
        provider.generate([{"role": "user", "content": "test"}], [])
    assert type(exc.value) is expected and seen == ["/api/chat"]
    assert "PRIVATE_BODY" not in str(exc.value)
    if form != "other":
        assert "48016" in str(exc.value) and "24576" in str(exc.value)


def test_reported_context_smaller_than_requested_is_rejected(local_env):
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b-q8_0", "context_length": 4096}]})
        return httpx.Response(200, json=native_response({"content": "ok"}))
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMContextError, match="实际加载"):
        provider.generate([{"role": "user", "content": "hi"}], [])


def test_timeout_and_missing_model_are_clear(local_env):
    def timeout(request):
        raise httpx.ReadTimeout("untrusted body", request=request)
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(timeout))
    with pytest.raises(OllamaTimeoutError, match="超时"):
        provider.generate([{"role": "user", "content": "x"}], [])
    def missing(request):
        return httpx.Response(200, json={"version": "0.34.1"} if request.url.path == "/api/version" else {"models": []})
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(missing))
    with pytest.raises(LLMLocalError, match="不存在"):
        provider.check_available()


@pytest.mark.parametrize("position", ["before_edit", "after_test"])
@pytest.mark.parametrize("empty_count", [1, 2])
def test_empty_turn_recovery_keeps_tool_history_and_requires_fresh_verification(local_env, position, empty_count):
    from evocode.agent import AgentController, AgentStatus
    from evocode.tasks.workspace import Workspace
    from evocode.tools import create_default_registry
    from test_agent import ContentExecutor, CORRECT, SAMPLE

    def tool(name, arguments, thinking):
        return native_response({"content": "", "thinking": thinking,
                                "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}, eval_count=1)

    responses = [tool("write_file", {"path": "calculator.py", "content": CORRECT}, "private-edit"),
                 tool("run_tests", {}, "private-test"), native_response({"content": "修复完成"}, eval_count=1)]
    index = 0 if position == "before_edit" else 2
    responses[index:index] = [native_response({"thinking": "private-empty", "content": "  "}, eval_count=1)
                              for _ in range(empty_count)]
    requests, events = [], []

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses.pop(0))

    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    executor = ContentExecutor()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=8, max_tool_calls=4,
                                 event_callback=events.append).run(
            issue="修复加法", workspace=workspace, test_executor=executor)
    assert result.status == AgentStatus.SUCCEEDED
    assert result.model_requests == 3 + empty_count and result.tool_calls == 3
    assert len(executor.contents) == 2 and all("return a + b" in content for content in executor.contents)
    assert result.last_verification.metadata["test_outcome"] == "passed"
    retries = [e for e in events if e["type"] == "model_retry"]
    assert [e["retry"] for e in retries] == list(range(1, empty_count + 1))
    errors = [e for e in events if e["type"] == "model_error"]
    assert errors[0]["response_metadata"] == {"done": True, "done_reason": "stop", "thinking_chars": 13,
                                              "content_chars": 2, "tool_call_count": 0}
    assert all(request["think"] is True for request in requests)
    assert provider.usage_totals["eval_count"] == result.model_requests
    assert "private-" not in json.dumps(events) + json.dumps(result.events)
    for request in requests[index + 1:index + empty_count + 1]:
        messages = request["messages"]
        assert messages[-1]["role"] == "user" and "最终回答正文" in messages[-1]["content"]
        assert "private-empty" not in json.dumps(messages)
        if position == "after_test":
            # The retry is a new user turn; old tool results stay, but old
            # thinking is not replayed across the user's message boundary.
            assert sum(m["role"] == "tool" for m in messages) == 2
            assert all("thinking" not in m for m in messages if m["role"] == "assistant")
    assert "return a - b" in (SAMPLE / "calculator.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("max_steps,done,reason,expected_requests,expected_status", [
    (10, True, "stop", 3, "FAILED"),
    (1, True, "stop", 1, "EXHAUSTED"),
    (2, True, "stop", 2, "EXHAUSTED"),
    (10, True, "length", 1, "FAILED"),
    (10, False, "stop", 1, "FAILED"),
])
def test_empty_recovery_is_bounded_and_does_not_retry_truncation(
    local_env, max_steps, done, reason, expected_requests, expected_status,
):
    from evocode.agent import AgentController
    from evocode.tasks.workspace import Workspace
    from evocode.tools import create_default_registry
    from test_agent import ContentExecutor, SAMPLE

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=native_response({"thinking": "private", "content": ""},
                                                       done=done, reason=reason))

    executor = ContentExecutor()
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=max_steps).run(
            issue="修复加法", workspace=workspace, test_executor=executor)
    assert result.status.value == expected_status and result.model_requests == expected_requests
    assert result.tool_calls == 0 and result.last_verification is None and not executor.contents


def test_recovery_counter_resets_but_a_claim_cannot_bypass_failing_tests(local_env):
    from evocode.agent import AgentController, AgentStatus
    from evocode.tasks.workspace import Workspace
    from evocode.tools import create_default_registry
    from test_agent import ContentExecutor, SAMPLE

    responses = [native_response({"thinking": "private", "content": ""}),
                 native_response({"content": "完成"})] * 3

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(200, json=responses.pop(0))

    executor = ContentExecutor()
    provider = OllamaProvider(config(local_env), transport=httpx.MockTransport(handler))
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(provider, create_default_registry(), max_steps=6, max_tool_calls=10).run(
            issue="修复加法", workspace=workspace, test_executor=executor)
    assert result.status == AgentStatus.EXHAUSTED and result.model_requests == 6
    assert [e["retry"] for e in result.events if e["type"] == "model_retry"] == [1, 1, 1]
    assert len(executor.contents) == 3 and result.last_verification.metadata["test_outcome"] == "failed"
