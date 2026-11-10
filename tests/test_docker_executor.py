import io
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from evocode.agent import AgentController, AgentStatus
from evocode.models import LLMResponse
from evocode.sandbox import DockerCleanupError, DockerExecutionError, DockerTestExecutor
from evocode.sandbox.docker import (
    _Output, _ReportOutput, _check_passing_report, _check_runner_report, _mount, _snapshot_workspace,
)
from evocode.tasks.workspace import Workspace
from evocode.tools import ToolCall, ToolContext, create_default_registry


PROJECT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT / "examples" / "repos" / "add_bug"


def snapshot(directory: Path) -> dict[str, bytes]:
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def docker_ready():
    try:
        server = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, timeout=8)
        image = subprocess.run(["docker", "image", "inspect", "evocode-pytest:py312"], capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker 引擎不可用")
    if server.returncode or server.stdout.strip() != b"linux" or image.returncode:
        pytest.skip("需要 Linux Docker 引擎和 evocode-pytest:py312 镜像")


class RecordingExecutor(DockerTestExecutor):
    def _create_args(self, image_id, name, token, snapshot_path):
        self.last_token = token
        self.last_snapshot = snapshot_path
        return super()._create_args(image_id, name, token, snapshot_path)


def assert_cleaned(token):
    result = subprocess.run(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter",
                             f"label=io.evocode.test-run={token}"], capture_output=True, timeout=10)
    assert result.returncode == 0 and not result.stdout.strip()


@pytest.mark.parametrize("name,value", [
    ("timeout_seconds", 0), ("timeout_seconds", float("inf")), ("timeout_seconds", True),
    ("memory_mb", 0), ("memory_mb", True), ("cpus", 0), ("pids_limit", -1),
    ("tmpfs_mb", 0), ("max_output_bytes", 0), ("max_workspace_bytes", 0), ("max_entries", 0),
])
def test_invalid_resource_limits_are_rejected(name, value):
    with pytest.raises(ValueError, match=name):
        DockerTestExecutor(**{name: value})


@pytest.mark.parametrize("image", ["", "--privileged", "image name", "image\nother"])
def test_rejects_invalid_image_names(image):
    with pytest.raises(ValueError, match="镜像"):
        DockerTestExecutor(image=image)


def test_snapshot_excludes_generated_directories_and_private_env(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("value = 1\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=dummy-secret\n", encoding="utf-8")
    (source / ".env.local").write_text("SECRET=dummy-secret\n", encoding="utf-8")
    (source / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    for name in (".git", ".venv", "__pycache__", ".pytest_cache"):
        directory = source / name
        directory.mkdir()
        (directory / "generated").write_text("ignored", encoding="utf-8")
    before = snapshot(source)
    target = tmp_path / "snapshot"
    _snapshot_workspace(source, target, max_bytes=10000, max_entries=100,
                        deadline=time.monotonic() + 10, timeout_seconds=10)
    assert set(path.name for path in target.iterdir()) == {"code.py", ".env.example"}
    assert (target / "code.py").read_bytes() == (source / "code.py").read_bytes()
    assert snapshot(source) == before


def test_snapshot_rejects_symlinks_and_oversized_inputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_bytes(b"12345")
    with pytest.raises(DockerExecutionError, match="大小"):
        _snapshot_workspace(source, tmp_path / "large", max_bytes=4, max_entries=10,
                            deadline=time.monotonic() + 10, timeout_seconds=10)
    try:
        os.symlink(source / "file.txt", source / "link.txt")
    except OSError as exc:
        pytest.skip(f"无法创建符号链接：{exc}")
    with pytest.raises(ValueError, match="符号链接或目录联接"):
        _snapshot_workspace(source, tmp_path / "linked", max_bytes=100, max_entries=10,
                            deadline=time.monotonic() + 10, timeout_seconds=10)


def test_snapshot_rejects_too_many_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.py").write_text("a = 1", encoding="utf-8")
    (source / "b.py").write_text("b = 2", encoding="utf-8")
    with pytest.raises(DockerExecutionError, match="数量"):
        _snapshot_workspace(source, tmp_path / "snapshot", max_bytes=100, max_entries=1,
                            deadline=time.monotonic() + 10, timeout_seconds=10)


def test_excluded_files_also_count_towards_scan_limit(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env.one").write_text("", encoding="utf-8")
    (source / ".env.two").write_text("", encoding="utf-8")
    with pytest.raises(DockerExecutionError, match="数量"):
        _snapshot_workspace(source, tmp_path / "snapshot", max_bytes=100, max_entries=1,
                            deadline=time.monotonic() + 10, timeout_seconds=10)


def test_output_buffer_is_bounded_and_mount_is_read_only(tmp_path):
    output = _Output(8)
    output.drain(io.BytesIO(b"123456789012345"))
    assert output.text().startswith("12345678") and "截断" in output.text()
    executor = DockerTestExecutor()
    args = executor._create_args("sha256:" + "a" * 64, "container", "b" * 32, tmp_path)
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args and args[args.index("--user") + 1] == "65534:65534"
    assert args[args.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges=true" in args and "seccomp=builtin" in args
    assert "readonly" in args[args.index("--mount") + 1]
    assert "--privileged" not in args and not any("docker.sock" in item for item in args)
    assert '"source=' in _mount(tmp_path / "comma,path")


def test_report_frame_is_processed_after_stderr_truncation():
    report = _ReportOutput(8, "token")
    report.drain(io.BytesIO(
        b"x" * 10000 + b'\n__EVOCODE_PYTEST_REPORT_token__:'
        b'{"tests":1,"skipped":0,"failures":0,"errors":0,"exit_code":0}\n'
    ))
    assert report.truncated is True
    assert report.text().startswith("x" * 8)
    assert "EVOCODE_PYTEST_REPORT" not in report.text()
    _check_passing_report(report.report)


@pytest.mark.parametrize("report", [
    None, {}, {"error": "pytest 未生成结构化报告"},
    {"tests": 0, "skipped": 0, "failures": 0, "errors": 0, "exit_code": 0},
    {"tests": 1, "skipped": 1, "failures": 0, "errors": 0, "exit_code": 0},
    {"tests": 1, "skipped": 0, "failures": 1, "errors": 0, "exit_code": 0},
    {"tests": 1, "skipped": 0, "failures": 0, "errors": 1, "exit_code": 0},
    {"tests": 1, "skipped": 0, "failures": 0, "errors": 0, "exit_code": 1},
    {"tests": True, "skipped": 0, "failures": 0, "errors": 0, "exit_code": 0},
])
def test_passing_report_must_confirm_executed_tests(report):
    with pytest.raises(DockerExecutionError, match="结构化报告"):
        _check_passing_report(report)


def test_runner_must_report_matching_exit_code_even_for_failures():
    with pytest.raises(DockerExecutionError, match="未返回"):
        _check_runner_report(None, 1)
    with pytest.raises(DockerExecutionError, match="不一致"):
        _check_runner_report({"exit_code": 5}, 1)
    _check_runner_report({"exit_code": 1, "error": "pytest 未生成结构化报告"}, 1)
    _check_runner_report({"exit_code": -9}, 137)


def test_image_declaring_volumes_is_rejected(monkeypatch):
    executor = DockerTestExecutor()
    monkeypatch.setattr(
        executor, "_command",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, ("sha256:" + "a" * 64 + ' linux {"/writable":{}}').encode(), b""
        ),
    )
    with pytest.raises(DockerExecutionError, match="卷"):
        executor._image_id(time.monotonic() + 10)


def test_out_of_memory_is_infrastructure_failure(monkeypatch):
    executor = DockerTestExecutor()
    monkeypatch.setattr(
        executor, "_command",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, b'{"Status":"exited","ExitCode":137,"OOMKilled":true,"Error":""}', b""
        ),
    )
    with pytest.raises(DockerExecutionError, match="内存"):
        executor._state("a" * 64, time.monotonic() + 10)


def test_cleanup_refuses_to_remove_container_without_matching_label(monkeypatch):
    executor = DockerTestExecutor()
    calls = []
    fake_id = "a" * 64

    def command(args, **_kwargs):
        calls.append(args)
        output = fake_id.encode() if args[0] == "ps" else b"different-label\n"
        return subprocess.CompletedProcess(args, 0, output, b"")

    monkeypatch.setattr(executor, "_command", command)
    with pytest.raises(DockerCleanupError):
        executor._cleanup("expected-token")
    assert [args[0] for args in calls] == ["ps", "inspect"]


def test_cleanup_reports_docker_failure(monkeypatch):
    executor = DockerTestExecutor()
    monkeypatch.setattr(
        executor, "_command", lambda args, **_kwargs: subprocess.CompletedProcess(args, 1, b"", b"error")
    )
    with pytest.raises(DockerCleanupError):
        executor._cleanup("test-token")


def test_create_timeout_attempts_owned_container_cleanup(tmp_path, monkeypatch):
    (tmp_path / "file.py").write_text("x = 1", encoding="utf-8")
    executor = DockerTestExecutor()
    cleaned = []
    monkeypatch.setattr(executor, "_image_id", lambda _deadline: "sha256:" + "a" * 64)

    def timed_out(args, **_kwargs):
        assert args[0] == "create"
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(executor, "_command", timed_out)
    monkeypatch.setattr(executor, "_cleanup", cleaned.append)
    with pytest.raises(subprocess.TimeoutExpired):
        executor(tmp_path)
    assert len(cleaned) == 1


def test_real_docker_tests_fail_then_pass_without_changing_sources(docker_ready):
    before = snapshot(SAMPLE)
    registry = create_default_registry()
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        workspace_before = snapshot(workspace.path)
        context = ToolContext(workspace, test_executor=executor)
        failed = registry.execute(ToolCall("failed", "run_tests", {}), context)
        assert failed.status == "ok" and failed.metadata["test_outcome"] == "failed"
        assert failed.metadata["exit_code"] == 1 and "2 failed, 1 passed" in failed.content
        assert_cleaned(executor.last_token)
        assert not executor.last_snapshot.exists()
        assert snapshot(workspace.path) == workspace_before
        original = workspace.read_file("calculator.py")
        workspace.write_file("calculator.py", original.replace("return a - b", "return a + b"))
        repaired = workspace.read_file("calculator.py")
        passed = registry.execute(ToolCall("passed", "run_tests", {}), context)
        assert passed.status == "ok" and passed.metadata["exit_code"] == 0
        assert passed.metadata["test_outcome"] == "passed" and "3 passed" in passed.content
        assert passed.metadata["truncated"] is False
        assert workspace.read_file("calculator.py") == repaired
        assert_cleaned(executor.last_token)
    assert snapshot(SAMPLE) == before


def test_real_container_isolation_and_no_host_credentials(docker_ready, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("EVOCODE_TEST_HOST_SECRET=dummy-secret\n", encoding="utf-8")
    (source / ".env.example").write_text("sample\n", encoding="utf-8")
    (source / "test_isolation.py").write_text(
        "import os, socket\nfrom pathlib import Path\n\n"
        "def test_isolation(tmp_path):\n"
        "    assert os.getuid() == 65534\n"
        "    assert os.getenv('EVOCODE_TEST_HOST_SECRET') is None\n"
        "    assert all(not os.getenv(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',\n"
        "        'http_proxy', 'https_proxy', 'all_proxy'))\n"
        "    assert not Path('/var/run/docker.sock').exists()\n"
        "    assert not Path('/workspace/.git').exists()\n"
        "    assert not Path('/workspace/.env').exists()\n"
        "    assert Path('/workspace/.env.example').exists()\n"
        "    status = Path('/proc/self/status').read_text()\n"
        "    assert 'NoNewPrivs:\\t1' in status\n"
        "    assert 'Seccomp:\\t2' in status\n"
        "    mounts = Path('/proc/self/mountinfo').read_text().splitlines()\n"
        "    assert any(' /workspace ' in line and ' ro,' in line for line in mounts)\n"
        "    try:\n"
        "        Path('/workspace/unwanted.txt').write_text('bad')\n"
        "    except OSError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('workspace writable')\n"
        "    (tmp_path / 'writable.txt').write_text('ok')\n"
        "    sock = socket.socket()\n"
        "    sock.settimeout(0.5)\n"
        "    try:\n"
        "        assert sock.connect_ex(('1.1.1.1', 53)) != 0\n"
        "    finally:\n"
        "        sock.close()\n", encoding="utf-8",
    )
    monkeypatch.setenv("EVOCODE_TEST_HOST_SECRET", "dummy-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://dummy:dummy-secret@proxy.invalid")
    before = snapshot(source)
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(source) as workspace:
        result = executor(workspace.path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        assert not (workspace.path / "unwanted.txt").exists()
        assert_cleaned(executor.last_token)
    assert snapshot(source) == before


def test_real_container_timeout_is_cleaned(docker_ready, tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
    (tmp_path / "test_hang.py").write_text(
        "import os, time\ndef test_hang():\n    os.write(2, b'X' * 20000)\n    time.sleep(60)\n", encoding="utf-8",
    )
    executor = RecordingExecutor(timeout_seconds=5, max_output_bytes=512)
    with Workspace(tmp_path) as workspace:
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            executor(workspace.path)
        assert exc.value.timeout == 5
        assert exc.value.output_truncated is True
        assert_cleaned(executor.last_token)
        assert not executor.last_snapshot.exists()
        registry = create_default_registry()
        observation = registry.execute(
            ToolCall("timeout", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "timeout" and observation.metadata["error_code"] == "timeout"
        assert observation.metadata["timeout_seconds"] == 5
        assert observation.metadata["truncated"] is True
        assert "容器输出已截断" in observation.content
        assert_cleaned(executor.last_token)


def test_missing_image_fails_without_automatic_pull(docker_ready, tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    with Workspace(tmp_path) as workspace:
        with pytest.raises(DockerExecutionError, match="镜像不可用"):
            DockerTestExecutor(image="evocode-image-does-not-exist:unit4")(workspace.path)


def test_real_container_reports_no_tests(docker_ready, tmp_path):
    (tmp_path / "code.py").write_text("value = 1\n", encoding="utf-8")
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(tmp_path) as workspace:
        observation = create_default_registry().execute(
            ToolCall("no-tests", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "ok"
        assert observation.metadata["exit_code"] == 5
        assert observation.metadata["test_outcome"] == "no_tests"
        assert_cleaned(executor.last_token)


@pytest.mark.parametrize("noisy", [False, True])
def test_controller_rejects_zero_exit_without_pytest_report(docker_ready, tmp_path, noisy):
    class ClaimProvider:
        def generate(self, _messages, _specs):
            return LLMResponse("已完成", [])

        def tool_results(self, _response, _observations):
            raise AssertionError("没有模型工具调用")

    before = snapshot(SAMPLE)
    source = tmp_path / "sample"
    shutil.copytree(SAMPLE, source)
    (source / "pytest.ini").write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
    output = "os.write(2, b'X' * 20000)\n" if noisy else ""
    (source / "test_00_exit.py").write_text("import os\n" + output + "os._exit(0)\n", encoding="utf-8")
    executor = RecordingExecutor(timeout_seconds=30, max_output_bytes=512)
    with Workspace(source, ["calculator.py"]) as workspace:
        controller = AgentController(ClaimProvider(), create_default_registry(), max_steps=1)
        result = controller.run(issue="修复计算器", workspace=workspace, test_executor=executor)
        assert result.status == AgentStatus.EXHAUSTED
        assert result.last_verification.status == "error"
        assert result.last_verification.metadata["error_code"] == "execution_error"
        assert result.last_verification.metadata["truncated"] is noisy
        assert "结构化报告" in result.last_verification.content
        assert "return a - b" in workspace.read_file("calculator.py") and result.patch == ""
        assert_cleaned(executor.last_token)
    assert snapshot(SAMPLE) == before


@pytest.mark.parametrize("report", [
    "<testsuites>",
    '<testsuites><testsuite tests="1" skipped="0" failures="0" errors="0"/></testsuites>',
])
def test_zero_exit_with_invalid_junit_report_is_rejected(docker_ready, tmp_path, report):
    (tmp_path / "test_exit.py").write_text(
        "import os\nfrom pathlib import Path\n"
        f"Path('/tmp/evocode-junit.xml').write_text({report!r})\n"
        "os._exit(0)\n", encoding="utf-8",
    )
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(tmp_path) as workspace:
        observation = create_default_registry().execute(
            ToolCall("invalid-report", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "error" and observation.metadata["error_code"] == "execution_error"
        assert "结构化报告无效" in observation.content
        assert_cleaned(executor.last_token)


def test_all_skipped_is_not_a_successful_verification(docker_ready, tmp_path):
    (tmp_path / "test_skip.py").write_text(
        "import pytest\n@pytest.mark.skip(reason='not executed')\ndef test_skip():\n    pass\n",
        encoding="utf-8",
    )
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(tmp_path) as workspace:
        observation = create_default_registry().execute(
            ToolCall("skipped", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "error" and observation.metadata["error_code"] == "execution_error"
        assert "至少一个测试通过" in observation.content
        assert_cleaned(executor.last_token)


def test_zero_exit_with_failed_test_in_junit_report_is_rejected(docker_ready, tmp_path):
    (tmp_path / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n", encoding="utf-8",
    )
    (tmp_path / "test_failure.py").write_text("def test_failure():\n    assert False\n", encoding="utf-8")
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(tmp_path) as workspace:
        observation = create_default_registry().execute(
            ToolCall("false-zero", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "error" and observation.metadata["error_code"] == "execution_error"
        assert "结构化报告" in observation.content
        assert_cleaned(executor.last_token)


def test_one_pass_and_one_skip_is_successful(docker_ready, tmp_path):
    (tmp_path / "test_mixed.py").write_text(
        "import pytest\n"
        "def test_pass():\n    assert True\n"
        "@pytest.mark.skip(reason='not executed')\ndef test_skip():\n    pass\n", encoding="utf-8",
    )
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(tmp_path) as workspace:
        observation = create_default_registry().execute(
            ToolCall("mixed", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "ok" and observation.metadata["test_outcome"] == "passed"
        assert "1 passed, 1 skipped" in observation.content
        assert_cleaned(executor.last_token)


def test_real_container_output_is_truncated(docker_ready, tmp_path):
    (tmp_path / "test_output.py").write_text(
        "def test_failure():\n    assert False, 'X' * 20000\n", encoding="utf-8",
    )
    executor = RecordingExecutor(timeout_seconds=30, max_output_bytes=512)
    with Workspace(tmp_path) as workspace:
        result = executor(workspace.path)
        assert result.returncode == 1
        assert result.output_truncated is True and result.stdout_truncated is True
        assert "容器输出已截断" in result.stdout
        assert len(result.stdout.encode("utf-8")) <= 512 + 100
        assert_cleaned(executor.last_token)
        observation = create_default_registry().execute(
            ToolCall("truncated", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "ok" and observation.metadata["test_outcome"] == "failed"
        assert observation.metadata["truncated"] is True
        assert "容器输出已截断" in observation.content
        assert_cleaned(executor.last_token)


def test_success_report_survives_stderr_truncation(docker_ready, tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
    (tmp_path / "test_pass.py").write_text(
        "import os\ndef test_pass():\n    os.write(2, b'X' * 20000)\n    assert True\n", encoding="utf-8",
    )
    executor = RecordingExecutor(timeout_seconds=30, max_output_bytes=512)
    with Workspace(tmp_path) as workspace:
        observation = create_default_registry().execute(
            ToolCall("pass", "run_tests", {}), ToolContext(workspace, test_executor=executor)
        )
        assert observation.status == "ok" and observation.metadata["test_outcome"] == "passed"
        assert observation.metadata["truncated"] is True
        assert "容器输出已截断" in observation.content
        assert "EVOCODE_PYTEST_REPORT" not in observation.content
        assert_cleaned(executor.last_token)


def test_real_container_accepts_snapshot_path_with_comma(docker_ready, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    temp_root = tmp_path / "temporary,space"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    executor = RecordingExecutor(timeout_seconds=30)
    with Workspace(source) as workspace:
        result = executor(workspace.path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed" in result.stdout
        assert_cleaned(executor.last_token)


def test_controller_can_use_real_docker_executor_without_host_pytest(docker_ready):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def generate(self, _messages, _specs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse("", [ToolCall("edit", "write_file", {
                    "path": "calculator.py", "content": "def add(a, b):\n    return a + b\n",
                })])
            return LLMResponse("已完成", [])

        def tool_results(self, response, observations):
            return [{"role": "assistant", "content": "已编辑"},
                    {"role": "user", "content": f"工具状态：{observations[0].status}"}]

    executor = RecordingExecutor(timeout_seconds=30)
    before = snapshot(SAMPLE)
    with Workspace(SAMPLE, ["calculator.py"]) as workspace:
        result = AgentController(FakeProvider(), create_default_registry()).run(
            issue="修复数值运算", workspace=workspace, test_executor=executor,
        )
        assert result.status == AgentStatus.SUCCEEDED
        assert result.last_verification.metadata["exit_code"] == 0
        assert "+    return a + b" in result.patch
        assert_cleaned(executor.last_token)
    assert snapshot(SAMPLE) == before
