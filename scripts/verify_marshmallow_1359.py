"""Verify the pinned task before or after applying a saved patch in a fresh Workspace."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_marshmallow_1359 import COMMIT, ROOT, TEST_REL, check_existing  # noqa: E402

from evocode.sandbox import DockerTestExecutor  # noqa: E402
from evocode.tasks.workspace import Workspace  # noqa: E402


EXPECTED_IMPORT = "EVOCODE_MARSHMALLOW_FILE=/workspace/src/marshmallow/__init__.py"


def _git(repo: Path, *args: str) -> bytes:
    process = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=repo,
                             capture_output=True, timeout=30, check=False)
    if process.returncode:
        raise RuntimeError(f"补丁 Git 检查失败：{args[0]}")
    return process.stdout


def _stats(result) -> dict:
    counts = {name: int(value) for value, name in re.findall(
        r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b", result.stdout[-6000:]
    )}
    return {"exit_code": result.returncode, "counts": counts,
            "workspace_import": EXPECTED_IMPORT in result.stdout,
            "python_3_7_17": "EVOCODE_PYTHON_VERSION=3.7.17" in result.stdout,
            "pytest_7_4_4": "EVOCODE_PYTEST_VERSION=7.4.4" in result.stdout,
            "output_truncated": result.output_truncated}


def _protected(repo: Path) -> dict[str, str]:
    paths = [path for path in (repo / "tests").rglob("*") if path.is_file()]
    paths += [repo / name for name in ("setup.cfg", "tox.ini", "pyproject.toml") if (repo / name).is_file()]
    return {path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _run(repo: Path, image: str, targets: tuple[str, ...], label: str, output: Path) -> dict:
    result = DockerTestExecutor(image=image, timeout_seconds=120, max_output_bytes=262144,
                                pytest_targets=targets)(repo)
    (output / f"{label}.txt").write_bytes((result.stdout + "\n[stderr]\n" + result.stderr).encode("utf-8"))
    stats = _stats(result)
    if not stats["workspace_import"] or not stats["python_3_7_17"] or not stats["pytest_7_4_4"]:
        raise RuntimeError(f"{label} 的容器环境或导入路径不符合要求")
    if stats["output_truncated"]:
        raise RuntimeError(f"{label} 输出被截断，无法完成审计")
    return stats


def verify(root: Path, image: str, patch: Path | None, output_root: Path) -> tuple[Path, dict]:
    root = root.resolve()
    check_existing(root)
    task = json.loads((root / "task.json").read_text(encoding="utf-8"))
    allowed = set(task["editable_files"])
    baseline = root / "baseline"
    original_targets = tuple(sorted(path.relative_to(baseline).as_posix() for path in (baseline / "tests").rglob("test_*.py")))
    if not original_targets or len(original_targets) > 50:
        raise RuntimeError("原有测试文件集合无效")
    image_id = DockerTestExecutor(image=image, timeout_seconds=120).check_available()
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / uuid.uuid4().hex
    output.mkdir()
    report = {"base_commit": COMMIT, "image_id": image_id, "patch": str(patch.resolve()) if patch else None}
    try:
        with Workspace(baseline) as original:
            report["original_before"] = _run(original.path, image_id, (), "original-before", output)
        with Workspace(root / "repo") as workspace:
            protected_before = _protected(workspace.path)
            report["repro_before"] = _run(workspace.path, image_id, (TEST_REL.as_posix(),), "repro-before", output)
            before_output = (output / "repro-before.txt").read_text(encoding="utf-8")
            if (report["original_before"]["exit_code"] != 0
                    or report["original_before"]["counts"].get("passed", 0) == 0
                    or report["repro_before"]["exit_code"] != 1
                    or report["repro_before"]["counts"].get("failed") != 2
                    or "AttributeError: 'List' object has no attribute 'opts'" not in before_output
                    or "AttributeError: 'Tuple' object has no attribute 'opts'" not in before_output):
                raise RuntimeError("修复前结果未能确认公开 Issue 的两个失败场景")
            if patch is not None:
                patch = patch.resolve(strict=True)
                patch_bytes = patch.read_bytes()
                if not patch_bytes:
                    raise RuntimeError("补丁为空")
                report["patch_sha256"] = hashlib.sha256(patch_bytes).hexdigest()
                raw_stats = _git(workspace.path, "apply", "--numstat", "-z", str(patch))
                modified = []
                for entry in raw_stats.split(b"\0"):
                    if not entry:
                        continue
                    parts = entry.decode("utf-8", errors="strict").split("\t")
                    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() or parts[2] not in allowed:
                        raise RuntimeError("补丁包含白名单外、重命名或二进制文件")
                    modified.append(parts[2])
                if not modified:
                    raise RuntimeError("补丁没有可检查的源码修改")
                _git(workspace.path, "apply", "--check", str(patch))
                _git(workspace.path, "apply", str(patch))
                name_status = _git(workspace.path, "diff", "--name-status", "--no-renames").decode("utf-8").splitlines()
                if any(not row.startswith("M\t") or row[2:] not in allowed for row in name_status):
                    raise RuntimeError("补丁只能修改白名单中的已有源码文件")
                if set(row[2:] for row in name_status) != set(modified) or _protected(workspace.path) != protected_before:
                    raise RuntimeError("补丁修改了不允许的文件或测试材料")
                report["modified_files"] = sorted(modified)
                report["original_after"] = _run(workspace.path, image_id, original_targets, "original-after", output)
                report["repro_after"] = _run(workspace.path, image_id, (TEST_REL.as_posix(),), "repro-after", output)
                report["combined_after"] = _run(workspace.path, image_id, (), "combined-after", output)
                if (_protected(workspace.path) != protected_before
                        or report["original_after"]["exit_code"] != 0
                        or report["original_after"]["counts"] != report["original_before"]["counts"]
                        or report["repro_after"]["exit_code"] != 0
                        or report["repro_after"]["counts"].get("passed") != 2
                        or report["combined_after"]["exit_code"] != 0
                        or report["combined_after"]["counts"].get("passed") != report["original_before"]["counts"]["passed"] + 2):
                    raise RuntimeError("补丁后的原有测试、复现测试或测试材料校验失败")
        check_existing(root)
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", reason=str(exc))
    (output / "verification.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return output, report


def main() -> None:
    parser = argparse.ArgumentParser(description="在干净工作区验证 marshmallow-1359 测试和补丁")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--image", default="evocode-marshmallow-1359:py37")
    parser.add_argument("--patch", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("runs/marshmallow-1359/verification"))
    args = parser.parse_args()
    try:
        output, report = verify(args.root, args.image, args.patch, args.output_dir)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"验证准备失败：{exc}\n")
    print(json.dumps({"output_dir": str(output.resolve()), **report}, ensure_ascii=True))
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
