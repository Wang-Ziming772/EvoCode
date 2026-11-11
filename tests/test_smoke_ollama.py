"""The manual local round trip stores metadata without private thinking."""

import json
import sys

import pytest

from evocode.models import LLMIncompleteError, LLMResponse
from evocode.tools import ToolCall
from scripts import smoke_ollama
from test_ollama import local_env


@pytest.mark.parametrize("profile", ["qwen4b", "qwen2b_q8", "qwen2b_bf16", "qwen27b"])
def test_roundtrip_evidence_omits_thinking(local_env, tmp_path, profile):
    class FakeProvider:
        def __init__(self, config):
            self.runtime = {"ollama_version": "fake", "model_digest": "fake", "actual_num_ctx": config.num_ctx}
            self.usage_totals = {"eval_count": 4}
            self.usage_observed_calls = {"eval_count": 2}
            self.calls = 0

        def prepare_for_run(self):
            return []

        def generate(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse("", [ToolCall("id", "read_file", {"path": "calculator.py"})],
                                   {"eval_count": 2}, [{"thinking": "private-thinking"}], "ollama", True)
            return LLMResponse("done", [], {"eval_count": 2}, [], "ollama", True)

        def tool_results(self, _response, observations):
            assert observations[0].status == "ok"
            return [{"role": "assistant", "content": ""}, {"role": "tool", "content": "ok"}]

    code, path = smoke_ollama.run(profile, local_env, tmp_path / "runs", provider_factory=FakeProvider)
    raw = (path / "result.json").read_text(encoding="utf-8")
    result = json.loads(raw)
    assert code == 0 and result["status"] == "SUCCEEDED"
    assert result["profile"] == profile and result["base_url"] == "http://127.0.0.1:11434"
    assert [item["thinking_present"] for item in result["requests"]] == [True, True]
    assert "private-thinking" not in raw


@pytest.mark.parametrize("profile", ["qwen2b_q8", "qwen2b_bf16", "qwen27b"])
def test_script_main_accepts_new_profile_names(monkeypatch, tmp_path, profile):
    calls = []
    def run(selected, env_file, output_dir):
        calls.append(selected)
        return 0, tmp_path
    monkeypatch.setattr(smoke_ollama, "run", run)
    monkeypatch.setattr(sys, "argv", ["smoke_ollama.py", "--profile", profile])
    with pytest.raises(SystemExit) as exc:
        smoke_ollama.main()
    assert exc.value.code == 0 and calls == [profile]


def test_roundtrip_records_unstructured_tool_markup_without_raw_text(local_env, tmp_path):
    class FakeProvider:
        runtime = {}; usage_totals = {}; usage_observed_calls = {}
        def __init__(self, config): pass
        def prepare_for_run(self): return []
        def generate(self, _messages, _tools):
            return LLMResponse('<tool_call>private-content</tool_call>', [], None, [], "ollama", True)
    code, path = smoke_ollama.run("qwen27b", local_env, tmp_path / "runs", provider_factory=FakeProvider)
    raw = (path / "result.json").read_text(encoding="utf-8")
    result = json.loads(raw)
    assert code == 1 and result["requests"][0]["tool_count"] == 0
    assert result["requests"][0]["tool_markup_in_content"] is True
    assert "private-content" not in raw


def test_roundtrip_records_incomplete_response_metadata(local_env, tmp_path):
    class FakeProvider:
        runtime = {}; usage_totals = {}; usage_observed_calls = {}
        last_thinking_present = True
        last_usage = {"eval_count": 4096}
        last_response_metadata = {"done": True, "done_reason": "length", "thinking_chars": 12000,
                                  "content_chars": 0, "tool_call_count": 0}
        def __init__(self, config): pass
        def prepare_for_run(self): return []
        def generate(self, _messages, _tools): raise LLMIncompleteError("达到输出上限")
    code, path = smoke_ollama.run("qwen27b", local_env, tmp_path / "runs", provider_factory=FakeProvider)
    result = json.loads((path / "result.json").read_text(encoding="utf-8"))
    assert code == 1 and result["requests"][0]["status"] == "error"
    assert result["requests"][0]["response_metadata"]["done_reason"] == "length"
    assert result["requests"][0]["usage"]["eval_count"] == 4096
