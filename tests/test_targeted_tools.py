"""Bounded reading and authorized, unambiguous editing."""

import pytest

from evocode.sandbox import DockerTestExecutor
from evocode.tasks.workspace import Workspace
from evocode.tools import ToolCall, ToolContext, create_default_registry


def call(context, name, **arguments):
    return create_default_registry().execute(ToolCall("id", name, arguments), context)


def test_line_ranges_and_legacy_read(tmp_path):
    text = "".join(f"line {n}\r\n" for n in range(1, 302))
    (tmp_path / "code.py").write_bytes(text.encode())
    with Workspace(tmp_path) as workspace:
        context = ToolContext(workspace)
        assert call(context, "read_file", path="code.py").content == text
        result = call(context, "read_file", path="code.py", start_line=200, end_line=201)
        assert result.content == "line 200\r\nline 201\r\n"
        assert result.metadata["total_lines"] == 301 and result.metadata["has_more"] is True
        assert call(context, "read_file", path="code.py", start_line=1.0, end_line=1.0).content == "line 1\r\n"
        for args in ({"start_line": 0}, {"start_line": 302}, {"start_line": 3, "end_line": 2},
                     {"start_line": 1, "end_line": 201}, {"start_line": True}):
            assert call(context, "read_file", path="code.py", **args).status == "error"


def test_replace_requires_unique_nonempty_partial_match_and_permission(tmp_path):
    original = b"first\r\nneedle\r\nthird\r\n"
    (tmp_path / "code.py").write_bytes(original)
    (tmp_path / "locked.py").write_text("needle plus text", encoding="utf-8")
    (tmp_path / "duplicate.py").write_bytes(b"aaa\n")
    with Workspace(tmp_path, ["code.py", "duplicate.py"]) as workspace:
        context = ToolContext(workspace)
        for path, old in (("code.py", ""), ("code.py", "missing"), ("code.py", original.decode()),
                          ("duplicate.py", "aa"), ("locked.py", "needle"), ("../outside", "needle")):
            outcome = call(context, "replace_text", path=path, old_text=old, new_text="changed")
            assert outcome.status == "error"
        assert workspace.path.joinpath("code.py").read_bytes() == original
        assert workspace.read_file("locked.py") == "needle plus text"
        assert workspace.read_file("duplicate.py") == "aaa\n"
        assert call(context, "replace_text", path="code.py", old_text="needle", new_text="updated").status == "ok"
        assert workspace.path.joinpath("code.py").read_bytes() == original.replace(b"needle", b"updated")
    assert (tmp_path / "code.py").read_bytes() == original


@pytest.mark.parametrize("targets", [("../x",), ("/abs",), ("--help",), (".git/config",), (3,), "tests"])
def test_pytest_targets_reject_unsafe_arguments(targets):
    with pytest.raises(ValueError):
        DockerTestExecutor(pytest_targets=targets)


def test_pytest_targets_are_fixed_paths_not_shell_commands(tmp_path):
    executor = DockerTestExecutor(pytest_targets=("tests/test_issue.py",))
    args = executor._create_args("sha256:" + "a" * 64, "name", "b" * 32, tmp_path)
    assert args[-1] == "tests/test_issue.py"
