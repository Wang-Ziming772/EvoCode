import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evocode import cli


@pytest.fixture
def task_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "tasks with spaces"
    tasks.mkdir()
    task_path = tasks / "task.json"
    task_path.write_text(
        json.dumps(
            {
                "task_id": "smoke-review",
                "issue": "修复加法函数",
                "repo": "../repo with spaces",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return repo, task_path


def run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "evocode.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


@pytest.mark.parametrize("args", [[], ["--help"]])
def test_help(args, tmp_path):
    completed = run_cli(args, tmp_path)
    assert completed.returncode == 0
    assert "--task" in completed.stdout
    assert not completed.stderr


@pytest.mark.parametrize(
    ("fixed", "expected_code", "summary"),
    [(False, 1, "1 failed"), (True, 0, "1 passed")],
)
def test_runs_task_relative_to_task_file(
    task_repo, tmp_path, fixed, expected_code, summary
):
    repo, task_path = task_repo
    if fixed:
        (repo / "calculator.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )

    completed = run_cli(["--task", str(task_path)], tmp_path)

    assert completed.returncode == expected_code
    assert "任务编号：smoke-review" in completed.stdout
    assert "问题描述：修复加法函数" in completed.stdout
    assert summary in completed.stdout
    assert f"测试退出码：{expected_code}" in completed.stdout
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize("repo_value", ["missing", "task.json"])
def test_rejects_missing_directory_or_regular_file(task_repo, tmp_path, repo_value):
    _, task_path = task_repo
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task["repo"] = repo_value
    task_path.write_text(json.dumps(task), encoding="utf-8")

    completed = run_cli(["--task", str(task_path)], tmp_path)

    assert completed.returncode == 2
    assert "代码目录不存在" in completed.stderr
    assert not completed.stdout
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize("contents", [None, "{", '{}'])
def test_reports_task_loading_errors(tmp_path, contents):
    task_path = tmp_path / "invalid.json"
    if contents is not None:
        task_path.write_text(contents, encoding="utf-8")

    completed = run_cli(["--task", str(task_path)], tmp_path)

    assert completed.returncode == 2
    assert "error:" in completed.stderr
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired(["pytest"], 0.01), "测试执行超时（0.01 秒）"),
        (OSError("cannot start interpreter"), "cannot start interpreter"),
    ],
)
def test_reports_test_execution_errors(task_repo, monkeypatch, capsys, error, message):
    _, task_path = task_repo

    def fail(_repo_dir):
        raise error

    monkeypatch.setattr(cli, "run_tests", fail)
    monkeypatch.setattr(sys, "argv", ["evocode", "--task", str(task_path)])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_forwards_both_output_streams_and_test_exit_code(task_repo, monkeypatch, capsys):
    _, task_path = task_repo
    result = subprocess.CompletedProcess(
        args=["pytest"],
        returncode=5,
        stdout="captured stdout\n",
        stderr="captured stderr\n",
    )
    monkeypatch.setattr(cli, "run_tests", lambda _repo_dir: result)
    monkeypatch.setattr(sys, "argv", ["evocode", "--task", str(task_path)])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 5
    captured = capsys.readouterr()
    assert "captured stdout\n" in captured.out
    assert "captured stderr\n" == captured.err
    assert "测试退出码：5" in captured.out
