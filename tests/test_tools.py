import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from evocode.tasks.workspace import Workspace
from evocode.tools import (
    Tool, ToolCall, ToolContext, ToolOutput, ToolRegistry, create_default_registry,
)
from evocode.tools.registry import TRUNCATION_MARKER


PROJECT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT / "examples" / "repos" / "add_bug"


def snapshot_files(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def call(registry, context, name, arguments=None, call_id="call-1"):
    return registry.execute(ToolCall(call_id, name, arguments if arguments is not None else {}), context)


def test_all_seven_tools_work_through_registry_on_sample():
    source = snapshot_files(SAMPLE)
    registry = create_default_registry()
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        context = ToolContext(workspace)
        listing = call(registry, context, "list_files", call_id="list")
        assert listing.call_id == "list" and listing.status == "ok"
        assert set(listing.content.splitlines()) == {"calculator.py", "test_calculator.py"}
        assert call(registry, context, "git_status").content == ""

        found = call(registry, context, "search_code", {"query": "return a - b"})
        assert found.status == "ok" and "calculator.py:2:" in found.content
        original = call(registry, context, "read_file", {"path": "calculator.py"})
        assert original.status == "ok" and "return a - b" in original.content

        failed = call(registry, context, "run_tests")
        assert failed.status == "ok"
        assert failed.metadata["exit_code"] == 1
        assert failed.metadata["test_outcome"] == "failed"
        assert "2 failed, 1 passed" in failed.content

        repaired = original.content.replace("return a - b", "return a + b")
        written = call(registry, context, "write_file", {"path": "calculator.py", "content": repaired})
        assert written.status == "ok"
        status = call(registry, context, "git_status")
        assert status.status == "ok" and " M calculator.py" in status.content
        assert ".pytest_cache" not in status.content and "__pycache__" not in status.content
        diff = call(registry, context, "git_diff")
        assert diff.status == "ok" and diff.metadata["has_changes"]
        assert "-    return a - b" in diff.content and "+    return a + b" in diff.content
        assert ".pytest_cache" not in diff.content and "__pycache__" not in diff.content
        assert "M  calculator.py" in call(registry, context, "git_status").content
        passed = call(registry, context, "run_tests")
        assert passed.status == "ok" and passed.metadata["test_outcome"] == "passed"
        assert passed.metadata["exit_code"] == 0 and "3 passed" in passed.content
        assert snapshot_files(SAMPLE) == source

    assert snapshot_files(SAMPLE) == source


def test_specs_are_json_and_registry_validates_custom_tool(tmp_path):
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    registry = create_default_registry()
    specs = registry.get_specs()
    assert {spec["name"] for spec in specs} == {
        "list_files", "read_file", "search_code", "write_file", "run_tests", "git_diff", "git_status",
    }
    json.dumps(specs, ensure_ascii=False)
    read_spec = next(spec for spec in specs if spec["name"] == "read_file")
    read_spec["parameters"]["required"].clear()
    assert next(spec for spec in registry.get_specs() if spec["name"] == "read_file")["parameters"]["required"] == ["path"]

    schema = {
        "type": "object", "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 3},
            "words": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        }, "required": ["count", "words"], "additionalProperties": False,
    }
    registry.register(Tool("repeat", "重复文本", schema,
                           lambda _ctx, args: ToolOutput(" ".join(args["words"]) * args["count"])))
    schema["properties"]["count"]["minimum"] = 0
    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace)
        assert call(registry, context, "repeat", {"count": 2, "words": ["x"]}).content == "xx"
        for invalid in (
            {"count": 0, "words": ["x"]}, {"count": True, "words": ["x"]},
            {"count": 1, "words": [2]}, {"count": 1, "words": [], "extra": 1},
        ):
            result = call(registry, context, "repeat", invalid)
            assert result.status == "error" and result.metadata["error_code"] == "invalid_arguments"

        registry.register(Tool("enum_test", "校验嵌套枚举", {
            "type": "object", "properties": {"value": {"type": "array", "items": {"type": "integer"},
                                                        "enum": [[1]]}},
            "required": ["value"], "additionalProperties": False,
        }, lambda _ctx, _args: ToolOutput("ok")))
        assert call(registry, context, "enum_test", {"value": [1]}).status == "ok"
        invalid_json = call(registry, context, "repeat", {"count": 1, "words": ["x"], 9: "invalid"})
        assert invalid_json.status == "error" and invalid_json.metadata["error_code"] == "invalid_arguments"

    with pytest.raises(ValueError, match="已注册"):
        registry.register(Tool("repeat", "重复", schema, lambda _ctx, _args: ToolOutput("")))
    with pytest.raises(ValueError, match="不支持"):
        registry.register(Tool("unsupported", "无效 schema", {"type": "object", "oneOf": []},
                               lambda _ctx, _args: ToolOutput("")))


def test_integer_schema_accepts_integral_numbers_and_rejects_invalid_values(tmp_path):
    seen = []

    def handle(_context, arguments):
        seen.append(arguments["count"])
        return ToolOutput("ok")

    registry = ToolRegistry()
    registry.register(Tool("integer_test", "校验整数", {
        "type": "object", "properties": {
            "count": {"type": "integer", "minimum": -2, "maximum": 3},
        }, "required": ["count"], "additionalProperties": False,
    }, handle))

    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace)
        for value in (-2, -2.0, -0.0, 0, 0.0, 1, 1.0, 3.0):
            result = call(registry, context, "integer_test", {"count": value})
            assert result.status == "ok", (value, result)
            assert type(seen[-1]) is type(value) and seen[-1] == value

        count = len(seen)
        for value in (True, False, 1.5, -1.5, float("nan"), float("inf"), float("-inf"), -3, 4.0):
            result = call(registry, context, "integer_test", {"count": value})
            assert result.status == "error", value
            assert result.metadata["error_code"] == "invalid_arguments"
            assert len(seen) == count


def test_unknown_invalid_and_forbidden_operations_return_observations(tmp_path):
    (tmp_path / "allowed.txt").write_text("allowed", encoding="utf-8")
    (tmp_path / "other.txt").write_text("original", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\x00")
    registry = create_default_registry()
    with Workspace(tmp_path, ["allowed.txt"]) as workspace:
        context = ToolContext(workspace)
        for name in ("missing_tool", "run_command", "apply_patch"):
            result = call(registry, context, name, call_id="unknown-id")
            assert result.call_id == "unknown-id" and result.status == "error"
            assert result.metadata["error_code"] == "unknown_tool"
        invalid_calls = [
            ("read_file", {}), ("read_file", {"path": 5}), ("read_file", {"path": ""}),
            ("search_code", {"query": ""}), ("run_tests", {"command": "anything"}),
            ("git_status", {"args": ["status"]}), ("write_file", {"path": "allowed.txt", "content": None}),
        ]
        for name, arguments in invalid_calls:
            result = call(registry, context, name, arguments)
            assert result.status == "error" and result.metadata["error_code"] == "invalid_arguments"
        non_object = registry.execute(ToolCall("bad", "read_file", ["file.txt"]), context)
        assert non_object.status == "error" and non_object.metadata["error_code"] == "invalid_arguments"
        escape = call(registry, context, "read_file", {"path": "../other.txt"})
        assert escape.status == "error" and escape.metadata["error_code"] == "invalid_value"
        private = call(registry, context, "read_file", {"path": ".git/config"})
        assert private.status == "error" and private.metadata["error_code"] == "invalid_value"
        denied = call(registry, context, "write_file", {"path": "other.txt", "content": "changed"})
        assert denied.status == "error" and denied.metadata["error_code"] == "permission_denied"
        invalid_text = call(registry, context, "read_file", {"path": "binary.bin"})
        assert invalid_text.status == "error" and invalid_text.metadata["error_code"] == "invalid_encoding"
        assert workspace.read_file("other.txt") == "original"
        assert call(registry, context, "git_diff").content == ""


def test_test_executor_injection_timeout_and_failures(tmp_path):
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    registry = create_default_registry()
    with Workspace(tmp_path) as workspace:
        seen = []

        def failed(repo_dir):
            seen.append(repo_dir)
            return subprocess.CompletedProcess(["pytest"], 1, stdout="assertion failed\n", stderr="warning\n")

        context = ToolContext(workspace, test_executor=failed)
        observation = call(registry, context, "run_tests", call_id="test-1")
        assert seen == [workspace.path]
        assert observation.call_id == "test-1" and observation.status == "ok"
        assert observation.metadata["exit_code"] == 1 and observation.metadata["test_outcome"] == "failed"
        assert "assertion failed" in observation.content and "warning" in observation.content
        json.dumps(asdict(observation))

        def timed_out(_repo_dir):
            raise subprocess.TimeoutExpired(["pytest"], 0.5, output=b"partial output", stderr=b"partial error")

        context.test_executor = timed_out
        timeout = call(registry, context, "run_tests")
        assert timeout.status == "timeout" and timeout.metadata["error_code"] == "timeout"
        assert timeout.metadata["timeout_seconds"] == 0.5
        assert "partial output" in timeout.content and "partial error" in timeout.content

        def fail_to_start(_repo_dir):
            raise OSError("interpreter unavailable")

        context.test_executor = fail_to_start
        failure = call(registry, context, "run_tests")
        assert failure.status == "error" and failure.metadata["error_code"] == "execution_error"
        assert "interpreter unavailable" in failure.content
        context.test_executor = lambda _repo_dir: subprocess.CompletedProcess([], 3, "", "internal error")
        internal = call(registry, context, "run_tests")
        assert internal.status == "ok" and internal.metadata["test_outcome"] == "internal_error"
        context.test_executor = lambda _repo_dir: {"returncode": 0}
        malformed = call(registry, context, "run_tests")
        assert malformed.status == "error" and malformed.metadata["error_code"] == "invalid_test_result"


def test_test_executor_is_not_called_after_workspace_closes(tmp_path):
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    seen = []
    registry = create_default_registry()
    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace, test_executor=lambda path: seen.append(path))
    result = call(registry, context, "run_tests")
    assert result.status == "error" and result.metadata["error_code"] == "execution_error"
    assert not seen


def test_test_executor_output_truncation_is_propagated(tmp_path):
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    result = subprocess.CompletedProcess(["pytest"], 1, "short output", "")
    result.output_truncated = True
    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace, test_executor=lambda _path: result)
        observation = call(create_default_registry(), context, "run_tests")
        assert observation.status == "ok" and observation.metadata["truncated"] is True
        result.output_truncated = "yes"
        invalid = call(create_default_registry(), context, "run_tests")
        assert invalid.status == "error" and invalid.metadata["error_code"] == "invalid_test_result"


def test_output_truncation_applies_to_all_large_results(tmp_path):
    for index in range(30):
        (tmp_path / f"file_{index:02}.txt").write_text("needle\n" * 20, encoding="utf-8")
    registry = create_default_registry(max_output_chars=120)
    with Workspace(tmp_path, ["file_00.txt"]) as workspace:
        context = ToolContext(workspace, test_executor=lambda _p: subprocess.CompletedProcess(
            [], 1, "begin\n" + "a" * 500, "error tail\n",
        ))
        for index in range(20):
            (workspace.path / f"new_{index:02}.txt").write_text("new", encoding="utf-8")
        results = [
            call(registry, context, "list_files"),
            call(registry, context, "read_file", {"path": "file_00.txt"}),
            call(registry, context, "search_code", {"query": "needle"}),
            call(registry, context, "run_tests"),
            call(registry, context, "git_status"),
        ]
        workspace.write_file("file_00.txt", "changed\n" * 100)
        results.append(call(registry, context, "git_diff"))
        for result in results:
            assert result.status == "ok"
            assert len(result.content) <= 120
            assert TRUNCATION_MARKER in result.content
            assert result.metadata["truncated"] is True
        assert "error tail" in results[3].content


def test_custom_tool_error_and_metadata_are_also_bounded(tmp_path):
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")
    registry = ToolRegistry(max_output_chars=120)
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def fail(_context, _arguments):
        raise OSError("x" * 500)

    registry.register(Tool("fail", "返回较长错误", schema, fail))
    registry.register(Tool("metadata", "返回较长元数据", schema,
                           lambda _ctx, _args: ToolOutput("ok", {"large": "x" * 5000})))
    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace)
        failure = call(registry, context, "fail")
        assert failure.status == "error" and len(failure.content) <= 120
        assert TRUNCATION_MARKER in failure.content and failure.metadata["truncated"] is True
        metadata = call(registry, context, "metadata")
        assert metadata.status == "ok" and metadata.metadata["metadata_truncated"] is True
        assert metadata.metadata["truncated"] is True and "large" not in metadata.metadata


def test_search_uses_literal_matching_and_skips_non_text_files(tmp_path):
    (tmp_path / "code.txt").write_bytes(b"x = 'a.b'\ny = 'axb'\nnext line\n")
    (tmp_path / "binary.bin").write_bytes(b"\xff\x00")
    (tmp_path / "null.bin").write_bytes(b"a.b\x00")
    registry = create_default_registry()
    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace)
        result = call(registry, context, "search_code", {"query": "a.b"})
        assert result.status == "ok"
        assert "code.txt:1:" in result.content and "code.txt:2:" not in result.content
        assert result.metadata["skipped_non_text_files"] == 2
        multiline = call(registry, context, "search_code", {"query": "axb'\nnext"})
        assert multiline.status == "ok" and "code.txt:2:" in multiline.content


def test_long_search_snippets_are_explicitly_marked(tmp_path):
    (tmp_path / "file.txt").write_text("a" * 800 + "needle" + "b" * 800, encoding="utf-8")
    with Workspace(tmp_path) as workspace:
        result = call(create_default_registry(), ToolContext(workspace), "search_code", {"query": "needle"})
        assert result.status == "ok" and "needle" in result.content
        assert "[片段已截断]" in result.content
        assert result.metadata["snippets_truncated"] == 1 and result.metadata["truncated"] is True


def test_tools_reject_symlink_added_inside_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("visible", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    registry = create_default_registry()
    with Workspace(source) as workspace:
        try:
            os.symlink(outside, workspace.path / "linked", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"无法创建符号链接：{exc}")
        context = ToolContext(workspace)
        for name, arguments in (("list_files", {}), ("search_code", {"query": "secret"}),
                                ("read_file", {"path": "linked/secret.txt"}), ("git_status", {})):
            result = call(registry, context, name, arguments)
            assert result.status == "error" and result.metadata["error_code"] == "invalid_value"
            assert "secret" not in result.content
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "secret"
