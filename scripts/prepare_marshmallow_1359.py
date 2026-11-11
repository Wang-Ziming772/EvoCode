"""Prepare one pinned, unmodified public issue without fetching benchmark patches."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / ".evocode-data" / "marshmallow-1359"
REPRO = PROJECT / "benchmarks" / "marshmallow_1359" / "test_issue_1357.py"
TEST_REL = Path("tests/test_evocode_issue_1357.py")
REPOSITORY = "https://github.com/marshmallow-code/marshmallow.git"
COMMIT = "b40a0f4e33823e6d0f341f7e8684e359a99060d1"
ISSUE_API = "https://api.github.com/repos/marshmallow-code/marshmallow/issues/1357"
TASK_ID = "marshmallow-code__marshmallow-1359"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo,
                            capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError(f"Git 操作失败：{args[0]}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_files(repo: Path) -> list[str]:
    source = repo / "src" / "marshmallow"
    if not source.is_dir():
        raise RuntimeError("基线缺少 src/marshmallow")
    files = sorted(path.relative_to(repo).as_posix() for path in source.rglob("*.py")
                   if path.is_file() and "__pycache__" not in path.parts)
    if not files:
        raise RuntimeError("没有找到源码 Python 文件")
    return files


def check_existing(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for repo in (root / "baseline", root / "repo"):
        if git(repo, "rev-parse", "HEAD") != COMMIT:
            raise RuntimeError("已有目录的提交不匹配，不会覆盖")
    if git(root / "baseline", "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("已有基线包含修改，不会覆盖")
    status = git(root / "repo", "status", "--porcelain", "--untracked-files=all")
    if status != f"?? {TEST_REL.as_posix()}":
        raise RuntimeError("已有任务副本包含修改，不会覆盖")
    task_bytes = (root / "task.json").read_bytes()
    test_bytes = (root / "repo" / TEST_REL).read_bytes()
    if (manifest.get("base_commit") != COMMIT or manifest.get("task_sha256") != digest(task_bytes)
            or manifest.get("repro_sha256") != digest(test_bytes) or test_bytes != REPRO.read_bytes()):
        raise RuntimeError("已有任务或复现测试已变化，不会覆盖")
    task = json.loads(task_bytes)
    if task.get("task_id") != TASK_ID or task.get("repo") != "repo" or task.get("editable_files") != source_files(root / "repo"):
        raise RuntimeError("已有任务配置不匹配，不会覆盖")


def prepare(root: Path = ROOT) -> Path:
    root = root.absolute()
    if root.exists():
        check_existing(root)
        return root / "task.json"
    request = urllib.request.Request(ISSUE_API, headers={"User-Agent": "EvoCode-task-prepare", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        issue = json.load(response)
    if issue.get("number") != 1357 or not isinstance(issue.get("body"), str) or not issue["body"].strip():
        raise RuntimeError("无法读取公开 Issue 正文")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="marshmallow-prepare-", dir=root.parent) as temp:
        stage = Path(temp) / "task"
        stage.mkdir()
        baseline = stage / "baseline"
        baseline.mkdir()
        git(baseline, "init", "--quiet")
        git(baseline, "remote", "add", "origin", REPOSITORY)
        git(baseline, "fetch", "--quiet", "--depth=1", "origin", COMMIT)
        git(baseline, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        if git(baseline, "rev-parse", "HEAD") != COMMIT:
            raise RuntimeError("获取的基线提交不匹配")
        repo = stage / "repo"
        repo.mkdir()
        git(repo, "init", "--quiet")
        git(repo, "fetch", "--quiet", "--depth=1", str(baseline), COMMIT)
        git(repo, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        target = repo / TEST_REL
        if target.exists() or not target.parent.is_dir():
            raise RuntimeError("复现测试目标路径不可用")
        test_bytes = REPRO.read_bytes()
        target.write_bytes(test_bytes)
        task = {"task_id": TASK_ID, "issue": f"{issue['title']}\n\n{issue['body']}",
                "repo": "repo", "editable_files": source_files(repo)}
        task_bytes = (json.dumps(task, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (stage / "task.json").write_bytes(task_bytes)
        manifest = {"instance_id": TASK_ID, "dataset": "SWE-bench Lite", "split": "dev",
                    "repository": REPOSITORY, "base_commit": COMMIT,
                    "issue": "https://github.com/marshmallow-code/marshmallow/issues/1357",
                    "repro_sha256": digest(test_bytes), "task_sha256": digest(task_bytes)}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        check_existing(stage)
        os.rename(stage, root)
    return root / "task.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="准备 marshmallow-1359 的固定基线与公开 Issue")
    parser.add_argument("--root", type=Path, default=ROOT, help="独立任务数据目录")
    args = parser.parse_args()
    try:
        print(f"任务文件：{prepare(args.root)}")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"准备失败：{exc}\n")


if __name__ == "__main__":
    main()
