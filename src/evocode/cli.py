import argparse
import math
import subprocess
import sys
from pathlib import Path

from evocode.models.config import LOCAL_PROFILES
from evocode.tasks.loader import load_task
from evocode.tasks.workspace import Workspace
from evocode.tools.testing import run_tests


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是正整数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是有限正数") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是有限正数")
    return number


def _print_repair_line(value: str, *, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    encoding = getattr(stream, "encoding", None)
    if encoding:
        value = value.encode(encoding, errors="backslashreplace").decode(encoding)
    print(value, file=stream)


def _repair(args: argparse.Namespace) -> None:
    from evocode.runner import RepairOptions, run_repair

    options = RepairOptions(
        task=args.task, profile=args.profile, env_file=args.env_file, output_dir=args.output_dir,
        max_steps=args.max_steps, max_tool_calls=args.max_tool_calls, image=args.image,
        test_timeout=args.test_timeout,
    )
    try:
        execution = run_repair(options)
    except KeyboardInterrupt:
        _print_repair_line("修复已中断", stream=sys.stderr)
        raise SystemExit(130) from None
    except Exception:
        _print_repair_line("启动失败：请检查任务、配置、输出目录和 Docker 设置", stream=sys.stderr)
        raise SystemExit(2) from None

    def safe(value: object) -> str:
        return execution.redactor.summary(str(value), 1000)[0]

    _print_repair_line(f"任务编号：{safe(execution.task['task_id']) if execution.task else '未知'}")
    if execution.config:
        _print_repair_line(f"模型：{safe(execution.config.profile)} / {safe(execution.config.model_id)}")
    else:
        _print_repair_line(f"模型：{safe(args.profile) if args.profile else '未确定'}")
    _print_repair_line(f"最终状态：{safe(execution.status)}")
    _print_repair_line(f"结束原因：{safe(execution.reason)}")
    _print_repair_line(f"请求次数：{execution.model_requests}；工具调用次数：{execution.tool_calls}")
    _print_repair_line(f"耗时：{execution.elapsed_seconds:.2f} 秒")
    if execution.run_dir:
        _print_repair_line(f"产物目录：{safe(execution.run_dir)}")
    if execution.result and execution.result.final_explanation:
        _print_repair_line(f"结果说明：{safe(execution.result.final_explanation)}")
    raise SystemExit(execution.exit_code)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="evocode",
        description="EvoCode: a code repair agent.",
    )
    parser.add_argument(
        "--task",
        type=Path,
        help="Path to a task JSON file",
    )
    subparsers = parser.add_subparsers(dest="command")
    repair = subparsers.add_parser("repair", help="Run a repair with Docker-isolated tests")
    repair.add_argument("--task", type=Path, required=True, help="Path to a task JSON file")
    repair.add_argument("--profile", choices=("gpt", "deepseek", *LOCAL_PROFILES), help="Model configuration profile")
    repair.add_argument("--env-file", type=Path, default=Path(".env"), help="Environment configuration file")
    repair.add_argument("--output-dir", type=Path, default=Path("runs"), help="Root for run artifacts")
    repair.add_argument("--max-steps", type=_positive_int, default=None, help="Maximum model requests (profile default)")
    repair.add_argument("--max-tool-calls", type=_positive_int, default=None, help="Maximum tool calls (profile default)")
    repair.add_argument("--image", default="evocode-pytest:py312", help="Local Docker test image")
    repair.add_argument("--test-timeout", type=_positive_float, default=30, help="Test timeout in seconds")
    args = parser.parse_args()

    if args.command == "repair":
        _repair(args)
        return

    if args.task is None:
        parser.print_help()
        return

    try:
        task_path = args.task.resolve()
        task = load_task(task_path)
        repo_dir = (task_path.parent / task["repo"]).absolute()
        if not repo_dir.is_dir():
            raise ValueError(f"代码目录不存在：{repo_dir}")

        with Workspace(repo_dir, editable_files=task.get("editable_files", [])) as workspace:
            result = run_tests(workspace.path)
    except subprocess.TimeoutExpired as exc:
        parser.exit(2, f"测试执行超时（{exc.timeout} 秒）\n")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"任务编号：{task['task_id']}")
    print(f"问题描述：{task['issue']}")
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    print(f"测试退出码：{result.returncode}")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
