import argparse
import subprocess
import sys
from pathlib import Path

from evocode.tasks.loader import load_task
from evocode.tools.testing import run_tests


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
    args = parser.parse_args()

    if args.task is None:
        parser.print_help()
        return

    try:
        task_path = args.task.resolve()
        task = load_task(task_path)
        repo_dir = (task_path.parent / task["repo"]).resolve()
        if not repo_dir.is_dir():
            raise ValueError(f"代码目录不存在：{repo_dir}")

        result = run_tests(repo_dir)
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
