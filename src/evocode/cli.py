import argparse
from pathlib import Path

from evocode.tasks.loader import load_task

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
        task = load_task(args.task)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"任务编号：{task['task_id']}")
    print(f"问题描述：{task['issue']}")


if __name__ == "__main__":
    main()
