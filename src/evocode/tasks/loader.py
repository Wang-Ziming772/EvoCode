import json
from pathlib import Path


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

    return data
