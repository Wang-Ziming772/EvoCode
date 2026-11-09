import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from evocode.tasks.loader import load_task
from evocode.tasks.workspace import Workspace
from evocode.tools.testing import run_tests


PROJECT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT / "examples" / "repos" / "add_bug"


def snapshot_files(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def make_directory_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"无法创建目录符号链接：{exc}")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("无法创建 Windows 目录联接")


def test_sample_copy_repair_test_and_applicable_patch(tmp_path):
    before = snapshot_files(SAMPLE)
    clean = tmp_path / "clean"
    shutil.copytree(SAMPLE, clean, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))

    with Workspace(SAMPLE, editable_files=["calculator.py"]) as workspace:
        workspace_path = workspace.path
        assert workspace.path != SAMPLE
        assert workspace.baseline_commit
        assert workspace.read_file("calculator.py") == (SAMPLE / "calculator.py").read_bytes().decode(
            "utf-8"
        )
        assert not (workspace.path / "__pycache__").exists()
        assert workspace.export_patch() == ""

        original = workspace.read_file("calculator.py")
        repaired = original.replace("return a - b", "return a + b")
        assert repaired != original
        workspace.write_file("calculator.py", repaired)
        result = run_tests(workspace.path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "3 passed" in result.stdout

        patch = workspace.export_patch()
        assert "--- a/calculator.py" in patch
        assert "-    return a - b" in patch
        assert "+    return a + b" in patch
        assert ".pytest_cache" not in patch
        assert "__pycache__" not in patch
        checked = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=clean,
            input=patch.encode("utf-8"),
            capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr.decode(errors="replace")
        assert snapshot_files(SAMPLE) == before

    assert not workspace_path.exists()
    assert not workspace_path.parent.exists()
    assert snapshot_files(SAMPLE) == before


def test_copy_excludes_generated_directories_and_git_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.txt").write_text("contents\n", encoding="utf-8")
    (source / ".git").write_text("gitdir: ../another-repo/.git\n", encoding="utf-8")
    for name in (".venv", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "pkg.egg-info"):
        generated = source / "nested" / name
        generated.mkdir(parents=True)
        (generated / "generated.txt").write_text("ignored", encoding="utf-8")
    before = snapshot_files(source)

    with Workspace(source) as workspace:
        assert workspace.read_file("code.txt") == (source / "code.txt").read_bytes().decode("utf-8")
        assert (workspace.path / ".git").is_dir()
        assert not list((workspace.path / "nested").iterdir())
        assert workspace.export_patch() == ""

    assert snapshot_files(source) == before


def test_patch_includes_ignored_files_additions_and_deletions_but_not_cache(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (source / "ignored.txt").write_text("old\n", encoding="utf-8")
    (source / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    with Workspace(source, ["ignored.txt"]) as workspace:
        workspace.write_file("ignored.txt", "new\n")
        (workspace.path / "deleted.txt").unlink()
        (workspace.path / "added.txt").write_text("added\n", encoding="utf-8")
        cache = workspace.path / ".pytest_cache" / "nested"
        cache.mkdir(parents=True)
        (cache / "state").write_text("cache\n", encoding="utf-8")
        pycache = workspace.path / "nested" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "module.pyc").write_bytes(b"compiled")
        egg_info = workspace.path / "pkg.egg-info"
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("generated", encoding="utf-8")

        patch = workspace.export_patch()
        assert "a/ignored.txt" in patch
        assert "a/deleted.txt" in patch
        assert "b/added.txt" in patch
        assert ".pytest_cache" not in patch
        assert "__pycache__" not in patch
        assert "egg-info" not in patch
        assert workspace.export_patch() == patch
        checked = subprocess.run(
            ["git", "apply", "--check", "-"], cwd=source,
            input=patch.encode("utf-8"), capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr.decode(errors="replace")


def test_patch_becomes_empty_when_edit_is_reverted(tmp_path):
    (tmp_path / "file.txt").write_text("before\n", encoding="utf-8")
    with Workspace(tmp_path, ["file.txt"]) as workspace:
        original = workspace.read_file("file.txt")
        workspace.write_file("file.txt", "after\n")
        assert workspace.export_patch()
        workspace.write_file("file.txt", original)
        assert workspace.export_patch() == ""


def test_unicode_file_path_and_contents_produce_an_applicable_patch(tmp_path):
    source = tmp_path / "source"
    module_dir = source / "模块"
    module_dir.mkdir(parents=True)
    (module_dir / "代码 文件.py").write_bytes("说明 = '原内容'\n".encode("utf-8"))
    with Workspace(source, ["模块/代码 文件.py"]) as workspace:
        workspace.write_file("模块/代码 文件.py", "说明 = '新内容'\n")
        patch = workspace.export_patch()
        assert "原内容" in patch and "新内容" in patch
        checked = subprocess.run(
            ["git", "apply", "--check", "-"], cwd=source,
            input=patch.encode("utf-8"), capture_output=True,
        )
        assert checked.returncode == 0, checked.stderr.decode(errors="replace")


def test_rejects_unsafe_paths_and_unauthorized_or_nontext_edits(tmp_path):
    (tmp_path / "allowed.txt").write_bytes(b"line one\r\nline two\r\n")
    (tmp_path / "other.txt").write_text("other", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff\x00")
    paths = [
        "../outside.txt", "nested/../../outside.txt", r"..\outside.txt",
        "/absolute", r"C:\absolute.txt", "C:relative.txt", r"\absolute.txt",
        ".git/config", "nested/.GIT/config", ".git./config", "", ".", "x:stream",
    ]
    with Workspace(tmp_path, ["allowed.txt", "missing.txt", "binary.bin"]) as workspace:
        for path in paths:
            with pytest.raises(ValueError):
                workspace.read_file(path)
            with pytest.raises(ValueError):
                workspace.write_file(path, "data")
        with pytest.raises(PermissionError):
            workspace.write_file("other.txt", "changed")
        with pytest.raises(FileNotFoundError):
            workspace.write_file("missing.txt", "new")
        with pytest.raises(UnicodeDecodeError):
            workspace.write_file("binary.bin", "changed")
        with pytest.raises(ValueError):
            workspace.write_file("allowed.txt", "not\0text")
        assert workspace.read_file("allowed.txt") == "line one\r\nline two\r\n"
        workspace.write_file("allowed.txt", "new\r\n")
        assert (workspace.path / "allowed.txt").read_bytes() == b"new\r\n"

    with Workspace(tmp_path) as workspace:
        with pytest.raises(PermissionError):
            workspace.write_file("allowed.txt", "changed")


def test_exception_cleans_up_workspace(tmp_path):
    (tmp_path / "file.txt").write_text("original", encoding="utf-8")
    with pytest.raises(RuntimeError, match="failure"):
        with Workspace(tmp_path, ["file.txt"]) as workspace:
            workspace_path = workspace.path
            workspace.write_file("file.txt", "changed")
            raise RuntimeError("failure")
    assert not workspace_path.exists()
    assert not workspace_path.parent.exists()
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "original"


def test_failed_entry_cleans_up_temporary_directory(tmp_path, monkeypatch):
    (tmp_path / "file.txt").write_text("original", encoding="utf-8")
    created = []
    original_factory = tempfile.TemporaryDirectory

    def tracked_temp(*args, **kwargs):
        temporary = original_factory(*args, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    def fail_git(self, *args):
        raise OSError("git unavailable")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", tracked_temp)
    monkeypatch.setattr(Workspace, "_git", fail_git)
    with pytest.raises(OSError, match="git unavailable"):
        with Workspace(tmp_path):
            pass
    assert created and all(not path.exists() for path in created)


def test_rejects_temporary_directory_inside_source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    temp_root = source / "temp"
    temp_root.mkdir(parents=True)
    (source / "file.txt").write_text("original", encoding="utf-8")
    before = snapshot_files(source)
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    with pytest.raises(ValueError, match="临时目录不能位于源目录内部"):
        with Workspace(source):
            pass
    assert snapshot_files(source) == before
    assert not list(temp_root.iterdir())


def test_rejects_source_directory_links_and_links_created_in_workspace(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = source / "linked"
    make_directory_link(link, outside)
    with pytest.raises(ValueError, match="符号链接或目录联接"):
        with Workspace(source):
            pass
    with pytest.raises(ValueError, match="符号链接或目录联接"):
        with Workspace(link):
            pass

    # Remove the link itself, without touching its target.
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)
    with Workspace(source, ["linked/secret.txt"]) as workspace:
        make_directory_link(workspace.path / "linked", outside)
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.read_file("linked/secret.txt")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.write_file("linked/secret.txt", "changed")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.export_patch()
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside"


def test_rejects_file_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = source / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"无法创建文件符号链接：{exc}")
    with pytest.raises(ValueError, match="符号链接或目录联接"):
        with Workspace(source):
            pass
    link.unlink()
    with Workspace(source, ["link.txt"]) as workspace:
        os.symlink(outside, workspace.path / "link.txt")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.read_file("link.txt")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.write_file("link.txt", "changed")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.export_patch()
    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.skipif(os.name != "nt", reason="目录联接只适用于 Windows")
def test_rejects_windows_directory_junctions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("outside", encoding="utf-8")

    def create_junction(link):
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("无法创建 Windows 目录联接")
        assert os.path.isjunction(link)

    link = source / "junction"
    create_junction(link)
    with pytest.raises(ValueError, match="符号链接或目录联接"):
        with Workspace(source):
            pass
    os.rmdir(link)
    with Workspace(source, ["junction/file.txt"]) as workspace:
        create_junction(workspace.path / "junction")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.read_file("junction/file.txt")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.write_file("junction/file.txt", "changed")
        with pytest.raises(ValueError, match="符号链接或目录联接"):
            workspace.export_patch()
    assert (outside / "file.txt").read_text(encoding="utf-8") == "outside"


def test_atomic_edit_does_not_modify_an_existing_hard_link_target(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "file.txt"
    original.write_text("source", encoding="utf-8")
    with Workspace(source, ["file.txt"]) as workspace:
        copied = workspace.path / "file.txt"
        copied.unlink()
        try:
            os.link(original, copied)
        except OSError as exc:
            pytest.skip(f"无法创建硬链接：{exc}")
        workspace.write_file("file.txt", "workspace")
        assert workspace.read_file("file.txt") == "workspace"
        assert original.read_text(encoding="utf-8") == "source"


@pytest.mark.skipif(os.name != "nt", reason="Windows 只读文件的替换语义")
def test_failed_write_to_read_only_file_leaves_no_temporary_file_or_patch(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"original\n")
    with Workspace(tmp_path, ["file.txt"]) as workspace:
        target = workspace.path / "file.txt"
        target.chmod(stat.S_IREAD)
        try:
            with pytest.raises(PermissionError):
                workspace.write_file("file.txt", "changed\n")
            assert workspace.export_patch() == ""
            assert not list(workspace.path.rglob(".evocode-edit-*"))
            assert not list(workspace.path.parent.glob(".evocode-edit-*"))
            assert target.read_bytes() == b"original\n"
        finally:
            target.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_failed_replace_removes_temporary_file(tmp_path, monkeypatch):
    (tmp_path / "file.txt").write_bytes(b"original\n")
    with Workspace(tmp_path, ["file.txt"]) as workspace:
        target = workspace.path / "file.txt"
        target.chmod(stat.S_IREAD)

        def fail_replace(*_args):
            raise PermissionError("simulated replacement failure")

        try:
            with monkeypatch.context() as patcher:
                patcher.setattr(os, "replace", fail_replace)
                with pytest.raises(PermissionError, match="simulated replacement failure"):
                    workspace.write_file("file.txt", "changed\n")
            assert not list(workspace.path.rglob(".evocode-edit-*"))
            assert not list(workspace.path.parent.glob(".evocode-edit-*"))
            assert workspace.export_patch() == ""
            assert target.read_bytes() == b"original\n"
        finally:
            target.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_git_baseline_ignores_default_user_attributes(tmp_path, monkeypatch):
    config = tmp_path / "user-config" / "git"
    config.mkdir(parents=True)
    (config / "attributes").write_bytes(b"*.txt text working-tree-encoding=UTF-16LE\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_bytes(b"abc")

    with Workspace(source, ["file.txt"]) as workspace:
        assert workspace.read_file("file.txt") == "abc"
        assert workspace.export_patch() == ""
        workspace.write_file("file.txt", "changed\n")
        patch = workspace.export_patch()
        assert "+changed" in patch
        assert (source / "file.txt").read_bytes() == b"abc"


def test_git_still_respects_project_attributes(tmp_path):
    (tmp_path / ".gitattributes").write_bytes(b"file.txt -diff\n")
    (tmp_path / "file.txt").write_bytes(b"original\n")
    with Workspace(tmp_path, ["file.txt"]) as workspace:
        workspace.write_file("file.txt", "changed\n")
        assert "GIT binary patch" in workspace.export_patch()


def test_git_baseline_ignores_inherited_identity_and_git_overrides(tmp_path, monkeypatch):
    (tmp_path / "file.txt").write_text("source", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repository"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "wrong author")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    with Workspace(tmp_path) as workspace:
        assert workspace.baseline_commit
        assert workspace._git("show", "-s", "--format=%an <%ae>", "HEAD").strip() == (
            "EvoCode <evocode@localhost>"
        )


@pytest.mark.parametrize(
    "editable_files",
    [None, "file.py", {}, [1], [""], ["."], ["../file.py"], [r"C:\file.py"],
     ["C:file.py"], [".git/config"], ["nested/.GIT/config"], ["bad?.py"], ["CON"],
     ["bad\nname"]],
)
def test_loader_rejects_invalid_editable_files(tmp_path, editable_files):
    task = {"task_id": "test", "issue": "issue", "repo": ".", "editable_files": editable_files}
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    with pytest.raises(ValueError, match="editable_files"):
        load_task(path)


def test_loader_accepts_missing_or_valid_editable_files(tmp_path):
    task = {"task_id": "test", "issue": "issue", "repo": "."}
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    assert "editable_files" not in load_task(path)
    task["editable_files"] = ["calculator.py", "src/module.py"]
    path.write_text(json.dumps(task), encoding="utf-8")
    assert load_task(path)["editable_files"] == task["editable_files"]


def test_cli_runs_smoke_in_copy_and_keeps_source_unchanged(tmp_path):
    before = snapshot_files(SAMPLE)
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "evocode.cli", "--task", str(PROJECT / "examples/tasks/smoke-001.json")],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 1
    assert "任务编号：smoke-001" in result.stdout
    assert "2 failed, 1 passed" in result.stdout
    assert "测试退出码：1" in result.stdout
    assert "return a - b" in (SAMPLE / "calculator.py").read_text(encoding="utf-8")
    assert snapshot_files(SAMPLE) == before
