import json
from pathlib import Path

from evocode.tasks.paths import validate_relative_path


def load_task(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError("最外层必须是字典")

    for field in ("task_id", "issue", "repo"):
        if field not in data:
            raise ValueError(f"缺少必填字段 {field}")
        value = data[field]
        if not isinstance(value, str):
            raise ValueError(f"{field} 必须是字符串")
        if not value.strip():
            raise ValueError(f"{field} 去掉首尾空白后不能为空")

    if "editable_files" in data:
        editable_files = data["editable_files"]
        if not isinstance(editable_files, list):
            raise ValueError("editable_files 必须是路径字符串列表")
        for path in editable_files:
            if not isinstance(path, str):
                raise ValueError("editable_files 必须是路径字符串列表")
            try:
                validate_relative_path(path)
            except ValueError as exc:
                raise ValueError(f"editable_files 中的路径无效：{exc}") from exc

    return data
