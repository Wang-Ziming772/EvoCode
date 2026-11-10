"""Run a fixed pytest command inside a disposable, resource-limited container."""

import csv
import io
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from evocode.tasks.workspace import _checked_stat, _check_source_path, _is_generated_directory


DEFAULT_IMAGE = "evocode-pytest:py312"
_LABEL = "io.evocode.test-run"
_OUTPUT_MARKER = "\n...[容器输出已截断]...\n"
_PYTEST = ["/usr/local/bin/python", "-m", "pytest", "-q", "-o", "cache_dir=/tmp/pytest-cache",
           "--basetemp=/tmp/pytest-temp"]
_RUNNER = "/opt/evocode/pytest_runner.py"
_REPORT_TAIL_BYTES = 4096
_PROBE = (
    "import runpy; import pytest; "
    "m=runpy.run_path('/opt/evocode/pytest_runner.py', run_name='evocode_preflight'); "
    "assert callable(m.get('main')) and callable(m.get('_report_summary')); "
    "print('EVOCODE_PYTEST_READY_V1')"
)


class DockerExecutionError(OSError):
    """Docker or container setup failed, rather than a pytest assertion."""


class DockerCleanupError(DockerExecutionError):
    """The executor could not confirm that its own container was removed."""


def _positive_int(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} 必须是正整数")


def _positive_number(value: float, name: str) -> None:
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{name} 必须是有限正数")


def _excluded_secret(name: str) -> bool:
    name = name.casefold()
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def _snapshot_workspace(source: Path, target: Path, *, max_bytes: int, max_entries: int, deadline: float,
                        timeout_seconds: float) -> None:
    """Copy regular files into an independent snapshot without following links."""
    _check_source_path(source)
    count = 0
    total = 0
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)

    def walk(directory: Path, destination: Path) -> None:
        nonlocal count, total
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("workspace snapshot", timeout_seconds)
                count += 1
                if count > max_entries:
                    raise DockerExecutionError("工作区文件数量超过容器快照限制")
                names.append(entry.name)
        names.sort(key=lambda name: (name.casefold(), name))
        for name in names:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("workspace snapshot", timeout_seconds)
            path = directory / name
            info = _checked_stat(path)
            if name.casefold() == ".git":
                continue
            if stat.S_ISDIR(info.st_mode) and _is_generated_directory(name):
                continue
            if stat.S_ISREG(info.st_mode) and _excluded_secret(name):
                continue
            copied_path = destination / name
            if stat.S_ISDIR(info.st_mode):
                copied_path.mkdir(mode=0o755)
                os.chmod(copied_path, 0o755)
                walk(path, copied_path)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > max_bytes:
                    raise DockerExecutionError("工作区文件总大小超过容器快照限制")
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                with os.fdopen(fd, "rb") as file, copied_path.open("xb") as copied:
                    current = os.fstat(file.fileno())
                    if (not stat.S_ISREG(current.st_mode) or current.st_size != info.st_size
                            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)):
                        raise DockerExecutionError("创建快照时工作区文件发生变化")
                    remaining = info.st_size
                    while remaining:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired("workspace snapshot", timeout_seconds)
                        chunk = file.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise DockerExecutionError("创建快照时工作区文件发生变化")
                        copied.write(chunk)
                        remaining -= len(chunk)
                    after = os.fstat(file.fileno())
                    if after.st_size != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
                        raise DockerExecutionError("创建快照时工作区文件发生变化")
                os.chmod(copied_path, 0o644 | (stat.S_IMODE(info.st_mode) & 0o111))
            else:
                raise DockerExecutionError("工作区包含不受支持的特殊文件")

    walk(source, target)


def _mount(snapshot: Path) -> str:
    result = io.StringIO(newline="")
    csv.writer(result, lineterminator="").writerow(
        ["type=bind", f"source={snapshot.resolve()}", "destination=/workspace", "readonly"]
    )
    return result.getvalue()


class _Output:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.error: Exception | None = None

    def add(self, chunk: bytes) -> None:
        remaining = self.limit - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def drain(self, pipe: BinaryIO) -> None:
        try:
            while chunk := pipe.read(8192):
                self.add(chunk)
        except Exception as exc:
            self.error = exc
        finally:
            pipe.close()

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace") + (_OUTPUT_MARKER if self.truncated else "")


class _ReportOutput(_Output):
    """Keep a bounded stderr tail for the runner's final report frame."""

    def __init__(self, limit: int, token: str) -> None:
        super().__init__(limit)
        self.marker = f"\n__EVOCODE_PYTEST_REPORT_{token}__:".encode("ascii")
        self.report: object = None

    def drain(self, pipe: BinaryIO) -> None:
        pending = bytearray()
        try:
            while chunk := pipe.read(8192):
                pending.extend(chunk)
                excess = len(pending) - _REPORT_TAIL_BYTES
                if excess > 0:
                    self.add(pending[:excess])
                    del pending[:excess]
            position = pending.rfind(self.marker)
            if position >= 0 and pending.endswith(b"\n"):
                payload = pending[position + len(self.marker):-1]
                try:
                    self.report = json.loads(payload)
                except (ValueError, UnicodeError):
                    pass
                self.add(pending[:position])
            else:
                self.add(pending)
        except Exception as exc:
            self.error = exc
        finally:
            pipe.close()


def _check_passing_report(report: object) -> None:
    if not isinstance(report, dict):
        raise DockerExecutionError("pytest 未返回有效的结构化报告")
    error = report.get("error")
    if isinstance(error, str) and error in {
        "pytest 未生成结构化报告", "pytest 结构化报告无效", "pytest 结构化报告超过大小限制",
    }:
        raise DockerExecutionError(error)
    names = ("tests", "skipped", "failures", "errors", "exit_code")
    if (any(type(report.get(name)) is not int or report[name] < 0 for name in names)
            or report["exit_code"] != 0
            or report["failures"] != 0 or report["errors"] != 0
            or report["tests"] <= report["skipped"]):
        raise DockerExecutionError("pytest 结构化报告未确认至少一个测试通过")


def _check_runner_report(report: object, exit_code: int) -> None:
    if not isinstance(report, dict) or type(report.get("exit_code")) is not int:
        raise DockerExecutionError("pytest 执行器未返回结构化报告")
    reported_code = report["exit_code"]
    normalized_code = reported_code if reported_code >= 0 else 128 - reported_code
    if normalized_code != exit_code:
        raise DockerExecutionError("pytest 报告与测试容器的退出状态不一致")
    if exit_code == 0:
        _check_passing_report(report)


class DockerTestResult(subprocess.CompletedProcess[str]):
    """CompletedProcess-compatible result with explicit output truncation flags."""

    def __init__(self, returncode: int, stdout: _Output, stderr: _Output) -> None:
        super().__init__(_PYTEST.copy(), returncode, stdout.text(), stderr.text())
        self.stdout_truncated = stdout.truncated
        self.stderr_truncated = stderr.truncated
        self.output_truncated = stdout.truncated or stderr.truncated


class DockerTestExecutor:
    """Callable compatible with ToolContext.test_executor and AgentController."""

    def __init__(
        self, *, image: str = DEFAULT_IMAGE, timeout_seconds: float = 30,
        memory_mb: int = 512, cpus: float = 1, pids_limit: int = 64,
        tmpfs_mb: int = 64, max_output_bytes: int = 65_536,
        max_workspace_bytes: int = 128 * 1024 * 1024, max_entries: int = 10_000,
        cleanup_timeout_seconds: float = 15,
    ) -> None:
        if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", image):
            raise ValueError("Docker 镜像名称无效")
        for name, value in (("memory_mb", memory_mb), ("pids_limit", pids_limit), ("tmpfs_mb", tmpfs_mb),
                            ("max_output_bytes", max_output_bytes), ("max_workspace_bytes", max_workspace_bytes),
                            ("max_entries", max_entries)):
            _positive_int(value, name)
        for name, value in (("timeout_seconds", timeout_seconds), ("cpus", cpus),
                            ("cleanup_timeout_seconds", cleanup_timeout_seconds)):
            _positive_number(value, name)
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.tmpfs_mb = tmpfs_mb
        self.max_output_bytes = max_output_bytes
        self.max_workspace_bytes = max_workspace_bytes
        self.max_entries = max_entries
        self.cleanup_timeout_seconds = cleanup_timeout_seconds

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("docker test", self.timeout_seconds)
        return remaining

    def _command(self, args: list[str], *, deadline: float, stdin: BinaryIO | None = None) -> subprocess.CompletedProcess[bytes]:
        command = ["docker", *args]
        try:
            return subprocess.run(command, stdin=stdin or subprocess.DEVNULL, capture_output=True,
                                  timeout=self._remaining(deadline), check=False)
        except subprocess.TimeoutExpired as exc:
            raise subprocess.TimeoutExpired(command, self.timeout_seconds, output=exc.output, stderr=exc.stderr) from None
        except OSError:
            raise DockerExecutionError("无法启动 Docker，请检查 Docker Desktop 和 docker 命令") from None

    def _image_id(self, deadline: float) -> str:
        result = self._command(["image", "inspect", "--format", '{{.Id}} {{.Os}} {{json (index .Config "Volumes")}}', self.image],
                               deadline=deadline)
        if result.returncode:
            raise DockerExecutionError("Docker 镜像不可用或 Docker 引擎未启动；请先构建指定镜像")
        try:
            image_id, os_name, volumes_json = result.stdout.decode("utf-8").strip().split(" ", 2)
            volumes = json.loads(volumes_json)
        except (UnicodeError, ValueError):
            raise DockerExecutionError("无法读取 Docker 镜像信息") from None
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or os_name != "linux":
            raise DockerExecutionError("测试镜像必须是本地 Linux 镜像")
        if volumes:
            raise DockerExecutionError("测试镜像不能声明额外的可写卷")
        return image_id

    def check_available(self) -> str:
        """Check the Linux engine and test image before any model request."""
        deadline = time.monotonic() + self.timeout_seconds
        result = self._command(["info", "--format", "{{.OSType}}"], deadline=deadline)
        if result.returncode or result.stdout.strip() != b"linux":
            raise DockerExecutionError("Docker Linux 引擎不可用，请检查 Docker Desktop")
        image_id = self._image_id(deadline)
        self._probe_image(image_id, deadline)
        return image_id

    def _probe_image(self, image_id: str, deadline: float) -> None:
        token = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="evocode-docker-probe-") as temp:
            snapshot = Path(temp) / "empty"
            snapshot.mkdir(mode=0o755)
            os.chmod(snapshot, 0o755)
            args = self._create_args(image_id, f"evocode-probe-{token}", token, snapshot)
            args[-2:] = ["-c", _PROBE]
            try:
                created = self._command(args, deadline=deadline)
                if created.returncode:
                    raise DockerExecutionError("Docker 测试镜像探测容器创建失败")
                container_id = created.stdout.decode("ascii").strip()
                if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                    raise DockerExecutionError("Docker 返回了无效的容器 ID")
                cli_code, stdout, _stderr = self._attach(container_id, token, deadline)
                state = self._state(container_id, deadline)
                if (cli_code != 0 or state["ExitCode"] != 0
                        or "EVOCODE_PYTEST_READY_V1" not in stdout.text().splitlines()):
                    raise DockerExecutionError("Docker 测试镜像缺少可用的 pytest 或 EvoCode 测试执行器")
            finally:
                self._cleanup(token)

    def _create_args(self, image_id: str, name: str, token: str, snapshot: Path) -> list[str]:
        memory = f"{self.memory_mb}m"
        return [
            "create", "--pull=never", "--name", name, "--label", f"{_LABEL}={token}",
            "--network", "none", "--read-only", "--user", "65534:65534",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges=true",
            "--security-opt", "seccomp=builtin", "--pids-limit", str(self.pids_limit),
            "--memory", memory, "--memory-swap", memory, "--cpus", str(self.cpus),
            "--shm-size", "16m", "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={self.tmpfs_mb}m,mode=1777",
            "--no-healthcheck", "--log-driver", "none", "--workdir", "/workspace", "--mount", _mount(snapshot),
            "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PYTHONUNBUFFERED=1",
            # Docker's client configuration may otherwise inject proxy URLs,
            # including credentials, even when no host variables are passed.
            *[item for name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY",
                                "http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy")
              for item in ("--env", f"{name}=")],
            "--entrypoint", _PYTEST[0], image_id, _RUNNER, token,
        ]

    def _attach(self, container_id: str, token: str, deadline: float) -> tuple[int, _Output, _ReportOutput]:
        command = ["docker", "start", "--attach", container_id]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise DockerExecutionError("无法启动 Docker 测试容器") from None
        stdout, stderr = _Output(self.max_output_bytes), _ReportOutput(self.max_output_bytes, token)
        threads = [threading.Thread(target=stdout.drain, args=(process.stdout,), daemon=True),
                   threading.Thread(target=stderr.drain, args=(process.stderr,), daemon=True)]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=self._remaining(deadline))
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for thread in threads:
                thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads) or stdout.error or stderr.error:
            raise DockerExecutionError("无法完整收集容器输出")
        if timed_out:
            exc = subprocess.TimeoutExpired(command, self.timeout_seconds,
                                            output=stdout.text(), stderr=stderr.text())
            exc.output_truncated = stdout.truncated or stderr.truncated
            raise exc
        return process.returncode, stdout, stderr

    def _state(self, container_id: str, deadline: float) -> dict:
        result = self._command(["inspect", "--format", "{{json .State}}", container_id], deadline=deadline)
        if result.returncode:
            raise DockerExecutionError("无法读取测试容器的退出状态")
        try:
            state = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise DockerExecutionError("测试容器的退出状态无效") from None
        if not isinstance(state, dict) or state.get("Status") != "exited" or type(state.get("ExitCode")) is not int:
            raise DockerExecutionError("测试容器未正常结束")
        if state.get("OOMKilled"):
            raise DockerExecutionError("测试容器因内存限制被终止")
        if state.get("Error"):
            raise DockerExecutionError("Docker 无法运行测试进程")
        return state

    def _cleanup(self, token: str) -> None:
        deadline = time.monotonic() + self.cleanup_timeout_seconds
        try:
            listed = self._command(["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={_LABEL}={token}"],
                                   deadline=deadline)
            if listed.returncode:
                raise DockerCleanupError("无法确认 Docker 测试容器是否已清理")
            ids = listed.stdout.decode("ascii").split()
            for container_id in ids:
                if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                    raise DockerCleanupError("Docker 清理目标无效")
                check = self._command(["inspect", "--format", f'{{{{ index .Config.Labels "{_LABEL}" }}}}', container_id],
                                      deadline=deadline)
                if check.returncode or check.stdout.decode("utf-8").strip() != token:
                    raise DockerCleanupError("Docker 清理目标的所有权校验失败")
                removed = self._command(["rm", "--force", "--volumes", container_id], deadline=deadline)
                if removed.returncode:
                    raise DockerCleanupError("Docker 测试容器清理失败")
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise DockerCleanupError("无法清理本次 Docker 测试容器，请检查 Docker Desktop") from None

    def __call__(self, repo_dir: Path) -> subprocess.CompletedProcess[str]:
        source = Path(os.path.abspath(repo_dir))
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root.is_relative_to(source.resolve()):
            raise DockerExecutionError("临时目录不能位于测试源码目录内部")
        deadline = time.monotonic() + self.timeout_seconds
        image_id = self._image_id(deadline)
        token = uuid.uuid4().hex
        name = f"evocode-test-{token}"
        with tempfile.TemporaryDirectory(prefix="evocode-docker-") as temp:
            snapshot_path = Path(temp) / "workspace"
            _snapshot_workspace(source, snapshot_path, max_bytes=self.max_workspace_bytes,
                                max_entries=self.max_entries, deadline=deadline, timeout_seconds=self.timeout_seconds)
            attempted_create = False
            try:
                attempted_create = True
                created = self._command(self._create_args(image_id, name, token, snapshot_path), deadline=deadline)
                if created.returncode:
                    raise DockerExecutionError("Docker 测试容器创建失败")
                container_id = created.stdout.decode("ascii").strip()
                if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                    raise DockerExecutionError("Docker 返回了无效的容器 ID")
                cli_code, stdout, stderr = self._attach(container_id, token, deadline)
                try:
                    state = self._state(container_id, deadline)
                    if cli_code != state["ExitCode"]:
                        raise DockerExecutionError("Docker 客户端与测试容器的退出状态不一致")
                    _check_runner_report(stderr.report, state["ExitCode"])
                except (DockerExecutionError, subprocess.TimeoutExpired) as exc:
                    exc.output_truncated = stdout.truncated or stderr.truncated
                    if isinstance(exc, subprocess.TimeoutExpired):
                        exc.output, exc.stderr = stdout.text(), stderr.text()
                    raise
                return DockerTestResult(state["ExitCode"], stdout, stderr)
            finally:
                if attempted_create:
                    self._cleanup(token)
