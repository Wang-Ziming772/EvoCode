"""Temporary file workspaces backed by an independent Git baseline."""

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

from evocode.tasks.paths import validate_relative_path


_GENERATED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "build",
        "dist",
    }
)


def _is_generated_directory(name: str) -> bool:
    name = name.casefold()
    return name in _GENERATED_DIRS or name.endswith(".egg-info")


def _git_excludes() -> list[str]:
    excludes = [
        f":(exclude,glob,icase)**/{name}/**" for name in sorted(_GENERATED_DIRS)
    ]
    return [*excludes, ":(exclude,glob,icase)**/*.egg-info/**"]


def _is_link_or_reparse_point(path: Path, info: os.stat_result) -> bool:
    # On Windows this also rejects junctions and other reparse points.
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & reparse
    ) or getattr(os.path, "isjunction", lambda _: False)(path)


def _checked_stat(path: Path) -> os.stat_result:
    info = path.lstat()
    if _is_link_or_reparse_point(path, info):
        raise ValueError(f"不支持符号链接或目录联接：{path}")
    return info


def _check_source_path(path: Path) -> None:
    # Check ancestors too, so specifying a path beneath a link cannot bypass
    # the check on the source directory itself.
    for component in reversed((path, *path.parents)):
        _checked_stat(component)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"代码目录不存在：{path}")


def _copy_source(source: Path, destination: Path) -> None:
    destination.mkdir()
    with os.scandir(source) as entries:
        for entry in entries:
            src = source / entry.name
            info = _checked_stat(src)
            # A Git worktree may have a .git *file* pointing at another repo.
            if entry.name.casefold() == ".git":
                continue
            if stat.S_ISDIR(info.st_mode):
                if not _is_generated_directory(entry.name):
                    _copy_source(src, destination / entry.name)
            elif stat.S_ISREG(info.st_mode):
                shutil.copy2(src, destination / entry.name, follow_symlinks=False)
            else:
                raise ValueError(f"不支持特殊文件：{src}")


def _check_workspace_tree(directory: Path, *, root: bool = False) -> None:
    """Reject links introduced after copying before asking Git to traverse."""
    with os.scandir(directory) as entries:
        for entry in entries:
            path = directory / entry.name
            info = _checked_stat(path)
            if entry.name.casefold() == ".git":
                if not root or not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"不支持嵌套或替换的 Git 目录：{path}")
                continue
            if stat.S_ISDIR(info.st_mode):
                if not _is_generated_directory(entry.name):
                    _check_workspace_tree(path)
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f"不支持特殊文件：{path}")


class Workspace:
    """Copy a source directory and expose a restricted text-editing API.

    This is a file workspace, not a sandbox for processes run inside it.
    """

    def __init__(
        self,
        source_dir: str | os.PathLike[str],
        editable_files: Iterable[str | os.PathLike[str]] | None = None,
    ) -> None:
        self.source_dir = Path(os.path.abspath(source_dir))
        if isinstance(editable_files, (str, bytes, os.PathLike)):
            raise ValueError("editable_files 必须是路径列表")
        if editable_files is None:
            editable_files = ()
        try:
            paths = iter(editable_files)
        except TypeError as exc:
            raise ValueError("editable_files 必须是路径列表") from exc
        self._editable_files = {
            Path(*validate_relative_path(path).parts) for path in paths
        }
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._path: Path | None = None
        self.baseline_commit: str | None = None

    @property
    def path(self) -> Path:
        if self._path is None:
            raise RuntimeError("工作区尚未创建")
        return self._path

    def __enter__(self) -> "Workspace":
        if self._temporary_directory is not None:
            raise RuntimeError("工作区已经打开")
        _check_source_path(self.source_dir)
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root.is_relative_to(self.source_dir.resolve()):
            raise ValueError("临时目录不能位于源目录内部")
        temp = tempfile.TemporaryDirectory(prefix="evocode-workspace-")
        self._temporary_directory = temp
        self._path = Path(temp.name) / "repo"
        self.baseline_commit = None
        try:
            _copy_source(self.source_dir, self.path)
            self._git("init", "--quiet", "--template=")
            self._stage_changes()
            self._git(
                "-c", "user.name=EvoCode", "-c", "user.email=evocode@localhost",
                "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty",
                "-m", "Workspace baseline",
            )
            self.baseline_commit = self._git("rev-parse", "HEAD").strip()
        except BaseException:
            self._temporary_directory = None
            temp.cleanup()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        temp = self._temporary_directory
        self._temporary_directory = None
        if temp is not None:
            temp.cleanup()

    def _require_active(self) -> Path:
        if self._temporary_directory is None:
            raise RuntimeError("工作区未打开")
        return self.path

    @property
    def active_path(self) -> Path:
        """Return the workspace path only while its context is active."""
        return self._require_active()

    def _file_path(self, relative_path: PurePosixPath) -> Path:
        root = self._require_active()
        current = root
        _checked_stat(current)
        for part in relative_path.parts:
            current = current / part
            info = _checked_stat(current)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"路径不是普通文件：{relative_path}")
        # On Windows, a short (8.3) filename can be an alias for .git.
        # Check the filesystem's canonical name as well as the supplied path.
        try:
            canonical = current.resolve(strict=True).relative_to(root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError(f"路径越过工作区边界：{relative_path}") from exc
        if any(part.casefold() == ".git" for part in canonical.parts):
            raise ValueError("不能访问 .git 内部路径")
        return current

    def read_file(self, relative_path: str | os.PathLike[str]) -> str:
        path = self._file_path(validate_relative_path(relative_path))
        with path.open("r", encoding="utf-8", newline="") as file:
            return file.read()

    def iter_files(self) -> Iterator[str]:
        """Yield accessible workspace-relative paths, without following links."""
        root = self._require_active()
        _checked_stat(root)

        def walk(directory: Path, relative: PurePosixPath) -> Iterator[str]:
            with os.scandir(directory) as entries:
                names = sorted((entry.name for entry in entries), key=lambda name: (name.casefold(), name))
            for name in names:
                path = directory / name
                info = _checked_stat(path)
                if name.casefold() == ".git":
                    continue
                child = relative / name
                try:
                    validate_relative_path(child.as_posix())
                except ValueError:
                    # Names rejected by the file API cannot be accessed there.
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if not _is_generated_directory(name):
                        yield from walk(path, child)
                elif stat.S_ISREG(info.st_mode):
                    yield child.as_posix()
                else:
                    raise ValueError(f"不支持特殊文件：{path}")

        yield from walk(root, PurePosixPath())

    def write_file(self, relative_path: str | os.PathLike[str], content: str) -> None:
        relative = validate_relative_path(relative_path)
        self._require_active()
        if Path(*relative.parts) not in self._editable_files:
            raise PermissionError(f"文件不在编辑白名单中：{relative}")
        if not isinstance(content, str):
            raise TypeError("文件内容必须是字符串")
        encoded = content.encode("utf-8")
        path = self._file_path(relative)
        # Verify that the existing file is UTF-8 text before replacing it.
        existing = path.read_bytes().decode("utf-8")
        if "\0" in existing or "\0" in content:
            raise ValueError("只能编辑 UTF-8 文本文件")

        # Keep the temporary file on the same filesystem but outside the Git
        # worktree. Replacing also avoids modifying another hard link target.
        fd, temp_name = tempfile.mkstemp(prefix=".evocode-edit-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(encoded)
            os.chmod(temp_name, stat.S_IMODE(path.stat().st_mode))
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            except PermissionError:
                # Windows cannot unlink a read-only file. It inherited that
                # attribute above so that a successful replacement keeps it.
                os.chmod(temp_name, stat.S_IREAD | stat.S_IWRITE)
                os.unlink(temp_name)

    def _git(self, *args: str) -> str:
        # Ignore inherited Git overrides and user/system config. Never change
        # the user's Git configuration, identity, hooks, or signing settings.
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        result = subprocess.run(
            [
                "git", "-c", "core.autocrlf=false",
                # Suppress the default user attributes without suppressing
                # .gitattributes files copied from the source tree.
                "-c", f"core.attributesFile={os.devnull}", *args,
            ],
            cwd=self.path,
            env=env,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(f"Git 操作失败：{detail or result.returncode}")
        # Bytes avoid newline conversion in patches for CRLF files.
        return result.stdout.decode("utf-8", errors="surrogateescape")

    def _stage_changes(self) -> None:
        if not stat.S_ISDIR(_checked_stat(self.path / ".git").st_mode):
            raise ValueError("工作区的 Git 目录无效")
        _check_workspace_tree(self.path, root=True)
        self._git("add", "--all", "--force", "--", ".", *_git_excludes())

    def git_status(self) -> str:
        """Return the worktree status without staging or accepting Git arguments."""
        self._require_active()
        if not stat.S_ISDIR(_checked_stat(self.path / ".git").st_mode):
            raise ValueError("工作区的 Git 目录无效")
        _check_workspace_tree(self.path, root=True)
        return self._git(
            "-c", "core.quotePath=false", "status", "--porcelain=v1",
            "--untracked-files=all", "--", ".", *_git_excludes(),
        )

    def export_patch(self) -> str:
        self._require_active()
        self._stage_changes()
        return self._git(
            "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
            self.baseline_commit or "HEAD", "--",
        )
