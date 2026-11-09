import os
from pathlib import PurePosixPath, PureWindowsPath


def validate_relative_path(path: str | os.PathLike[str]) -> PurePosixPath:
    """Return a portable, unambiguous workspace-relative file path."""
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ValueError("路径必须是字符串或路径对象") from exc
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise ValueError("路径必须是非空字符串")

    windows_path = PureWindowsPath(raw)
    portable = raw.replace("\\", "/")
    if windows_path.drive or windows_path.root or portable.startswith("/"):
        raise ValueError(f"路径必须是相对路径：{raw}")

    raw_parts = portable.split("/")
    if ".." in raw_parts:
        raise ValueError(f"路径不能包含上级目录：{raw}")
    parts = PurePosixPath(portable).parts
    if not parts:
        raise ValueError("路径必须指向文件")
    for part in parts:
        if part.rstrip(" .").casefold() == ".git":
            raise ValueError("不能访问 .git 内部路径")
        # Avoid Windows aliases and drive/alternate-stream syntax on every OS.
        invalid_character = any(char in '<>:"|?*' or ord(char) < 32 for char in part)
        if invalid_character or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved():
            raise ValueError(f"路径包含不受支持的名称：{raw}")
    return PurePosixPath(*parts)
