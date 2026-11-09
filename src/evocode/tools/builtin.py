"""The fixed file, testing, and Git operations available to the agent."""

from typing import Any

from evocode.tools.contracts import Tool, ToolContext, ToolError, ToolOutput
from evocode.tools.registry import DEFAULT_OUTPUT_LIMIT, TRUNCATION_MARKER, ToolRegistry


_COLLECTION_LIMIT = 32_000
_SNIPPET_LIMIT = 500


class _Lines:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.chars = 0
        self.truncated = False

    def add(self, line: str) -> bool:
        separator = 1 if self.lines else 0
        remaining = _COLLECTION_LIMIT - self.chars - separator
        if len(line) > remaining:
            if remaining > 0:
                self.lines.append(line[:remaining])
            self.truncated = True
            return False
        self.lines.append(line)
        self.chars += len(line) + separator
        return True

    def output(self) -> str:
        return "\n".join(self.lines) + (TRUNCATION_MARKER if self.truncated else "")


def _list_files(context: ToolContext, _arguments: dict[str, Any]) -> ToolOutput:
    output = _Lines()
    for path in context.workspace.iter_files():
        if not output.add(path):
            break
    return ToolOutput(output.output(), {
        "entries_collected": len(output.lines), "truncated": output.truncated,
    })


def _read_file(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    return ToolOutput(context.workspace.read_file(arguments["path"]))


def _snippet(text: str, position: int) -> tuple[str, bool]:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end < 0:
        end = len(text)
    line = text[start:end].rstrip("\r")
    match_offset = position - start
    if len(line) <= _SNIPPET_LIMIT:
        return line, False
    left = max(0, min(match_offset - 120, len(line) - _SNIPPET_LIMIT))
    right = left + _SNIPPET_LIMIT
    marker = "...[片段已截断]..."
    return (marker if left else "") + line[left:right] + (marker if right < len(line) else ""), True


def _search_code(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    query = arguments["query"]
    output = _Lines()
    skipped = 0
    matches = 0
    clipped_snippets = 0
    for path in context.workspace.iter_files():
        try:
            text = context.workspace.read_file(path)
        except UnicodeDecodeError:
            skipped += 1
            continue
        if "\0" in text:
            skipped += 1
            continue
        cursor = previous = 0
        line_number = 1
        while (position := text.find(query, cursor)) != -1:
            line_number += text.count("\n", previous, position)
            previous = position
            snippet, clipped = _snippet(text, position)
            line = f"{path}:{line_number}:{snippet}"
            if not output.add(line):
                break
            matches += 1
            clipped_snippets += int(clipped)
            cursor = position + len(query)
        if output.truncated:
            break
    return ToolOutput(output.output(), {
        "matches_collected": matches, "skipped_non_text_files": skipped,
        "snippets_truncated": clipped_snippets, "truncated": output.truncated or bool(clipped_snippets),
    })


def _write_file(context: ToolContext, arguments: dict[str, Any]) -> ToolOutput:
    context.workspace.write_file(arguments["path"], arguments["content"])
    return ToolOutput(f"已更新文件：{arguments['path']}")


def _run_tests(context: ToolContext, _arguments: dict[str, Any]) -> ToolOutput:
    result = context.test_executor(context.workspace.active_path)
    code = getattr(result, "returncode", None)
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    if type(code) is not int or (stdout is not None and not isinstance(stdout, str)) or (
        stderr is not None and not isinstance(stderr, str)
    ):
        raise ToolError("invalid_test_result", "测试执行器必须返回退出码和文本输出")
    content = f"标准输出：\n{stdout or ''}\n标准错误：\n{stderr or ''}"
    outcome = {0: "passed", 1: "failed", 2: "interrupted", 3: "internal_error",
               4: "usage_error", 5: "no_tests"}.get(code, "other")
    return ToolOutput(content, {"exit_code": code, "test_outcome": outcome})


def _git_diff(context: ToolContext, _arguments: dict[str, Any]) -> ToolOutput:
    patch = context.workspace.export_patch()
    return ToolOutput(patch, {"has_changes": bool(patch)})


def _git_status(context: ToolContext, _arguments: dict[str, Any]) -> ToolOutput:
    return ToolOutput(context.workspace.git_status())


def _schema(properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


def create_default_registry(*, max_output_chars: int = DEFAULT_OUTPUT_LIMIT) -> ToolRegistry:
    """Create a registry containing exactly the seven built-in tools."""
    registry = ToolRegistry(max_output_chars=max_output_chars)
    path = {"type": "string", "description": "工作区内的相对文件路径", "minLength": 1, "maxLength": 4096}
    tools = [
        Tool("list_files", "列出可访问的工作区文件，不包括 Git 内部和生成目录。",
             _schema(), _list_files),
        Tool("read_file", "读取工作区中的 UTF-8 文本文件。",
             _schema({"path": path}, ("path",)), _read_file),
        Tool("search_code", "区分大小写地搜索字面量，返回路径、行号和片段。",
             _schema({"query": {"type": "string", "minLength": 1, "maxLength": 256}}, ("query",)),
             _search_code),
        Tool("write_file", "替换白名单中已有 UTF-8 文本文件的内容。",
             _schema({"path": path, "content": {"type": "string", "maxLength": 1_000_000}},
                     ("path", "content")), _write_file),
        Tool("run_tests", "使用上下文中的测试执行器运行测试。", _schema(), _run_tests),
        Tool("git_diff", "查看相对于工作区基线的 Git 补丁。", _schema(), _git_diff),
        Tool("git_status", "查看工作区的 Git 文件状态。", _schema(), _git_status),
    ]
    for tool in tools:
        registry.register(tool)
    return registry
