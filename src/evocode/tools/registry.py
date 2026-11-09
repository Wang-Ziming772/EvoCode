"""Registration, JSON Schema subset validation, and bounded observations."""

import copy
import json
import math
import re
import subprocess
from typing import Any

from evocode.tools.contracts import Observation, Tool, ToolCall, ToolContext, ToolError, ToolOutput


DEFAULT_OUTPUT_LIMIT = 16_000
TRUNCATION_MARKER = "\n...[输出已截断]..."
_METADATA_LIMIT = 2_048
_MAX_CALL_ID = 256


class _InvalidArguments(ValueError):
    pass


def _is_number(value: Any) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _check_json_value(value: Any) -> None:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON 对象的字段名必须是字符串")
        for item in value.values():
            _check_json_value(item)
    elif isinstance(value, list):
        for item in value:
            _check_json_value(item)
    elif value is not None and not isinstance(value, (str, bool)) and not _is_number(value):
        raise ValueError("值必须是有效的 JSON 数据")


def _json_equal(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def _check_schema(schema: Any) -> None:
    """Reject unsupported schema features instead of silently ignoring them."""
    if not isinstance(schema, dict) or schema.get("type") not in {
        "object", "array", "string", "integer", "number", "boolean", "null",
    }:
        raise ValueError("工具参数 schema 必须指定支持的 type")
    kind = schema["type"]
    common = {"type", "description", "enum"}
    specific = {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems"},
        "string": {"minLength", "maxLength"},
        "integer": {"minimum", "maximum"},
        "number": {"minimum", "maximum"},
        "boolean": set(),
        "null": set(),
    }[kind]
    if set(schema) - common - specific:
        raise ValueError("工具参数 schema 包含不支持的关键字")
    if "description" in schema and not isinstance(schema["description"], str):
        raise ValueError("schema description 必须是字符串")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("schema enum 必须是非空列表")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not all(isinstance(k, str) for k in properties):
            raise ValueError("schema properties 必须是对象")
        if not isinstance(required, list) or not all(isinstance(k, str) for k in required):
            raise ValueError("schema required 必须是字符串列表")
        if len(set(required)) != len(required):
            raise ValueError("schema required 不能包含重复项")
        if not isinstance(schema.get("additionalProperties", True), bool):
            raise ValueError("schema additionalProperties 必须是布尔值")
        for child in properties.values():
            _check_schema(child)
    elif kind == "array":
        if "items" not in schema:
            raise ValueError("数组 schema 必须指定 items")
        _check_schema(schema["items"])
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"schema {key} 必须是非负整数")
    for key in ("minimum", "maximum"):
        if key in schema and not _is_number(schema[key]):
            raise ValueError(f"schema {key} 必须是有限数值")
    for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems"),
                         ("minimum", "maximum")):
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ValueError(f"schema {lower} 不能大于 {upper}")


def _validate(value: Any, schema: dict[str, Any], location: str = "参数") -> None:
    kind = schema["type"]
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int or (
            type(value) is float and math.isfinite(value) and value.is_integer()
        ),
        "number": _is_number(value),
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]
    if not valid:
        raise _InvalidArguments(f"{location} 必须是 {kind}")
    if "enum" in schema and not any(_json_equal(value, candidate) for candidate in schema["enum"]):
        raise _InvalidArguments(f"{location} 不在允许的取值中")
    if kind == "object":
        if not all(isinstance(key, str) for key in value):
            raise _InvalidArguments(f"{location} 的字段名必须是字符串")
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise _InvalidArguments(f"{location} 缺少必需字段：{', '.join(missing)}")
        extra = [key for key in value if key not in properties]
        if extra and not schema.get("additionalProperties", True):
            raise _InvalidArguments(f"{location} 包含未知字段：{', '.join(extra)}")
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f"{location}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise _InvalidArguments(f"{location} 的元素过少")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise _InvalidArguments(f"{location} 的元素过多")
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{location}[{index}]")
    elif kind == "string":
        if len(value) < schema.get("minLength", 0):
            raise _InvalidArguments(f"{location} 的长度过短")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise _InvalidArguments(f"{location} 的长度过长")
    elif kind in {"number", "integer"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise _InvalidArguments(f"{location} 小于最小值")
        if "maximum" in schema and value > schema["maximum"]:
            raise _InvalidArguments(f"{location} 大于最大值")


def _partial_output(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


class ToolRegistry:
    """Validate and dispatch calls without depending on a model SDK."""

    def __init__(self, *, max_output_chars: int = DEFAULT_OUTPUT_LIMIT) -> None:
        if type(max_output_chars) is not int or max_output_chars < len(TRUNCATION_MARKER) + 1:
            raise ValueError("max_output_chars 过小或类型无效")
        self.max_output_chars = max_output_chars
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("注册项必须是 Tool")
        if not isinstance(tool.name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", tool.name):
            raise ValueError("工具名称格式无效")
        if tool.name in self._tools:
            raise ValueError(f"工具已注册：{tool.name}")
        if (
            not isinstance(tool.description, str)
            or not tool.description.strip()
            or not callable(tool.handler)
        ):
            raise ValueError("工具必须有描述和可调用的处理函数")
        try:
            json.dumps(tool.parameters, allow_nan=False)
            _check_json_value(tool.parameters)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("工具参数 schema 必须可序列化为 JSON") from exc
        _check_schema(tool.parameters)
        if tool.parameters["type"] != "object":
            raise ValueError("工具参数 schema 的顶层必须是 object")
        self._tools[tool.name] = Tool(
            tool.name, tool.description, copy.deepcopy(tool.parameters), tool.handler,
        )

    def get_specs(self) -> list[dict[str, Any]]:
        return [
            {"name": tool.name, "description": tool.description,
             "parameters": copy.deepcopy(tool.parameters)}
            for tool in self._tools.values()
        ]

    def _observation(
        self, call_id: str, status: str, content: str, metadata: dict[str, Any],
    ) -> Observation:
        try:
            _check_json_value(metadata)
            encoded_metadata = json.dumps(metadata, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return Observation(call_id, "error", "工具返回了无效的元数据", {
                "error_code": "invalid_tool_output", "truncated": False,
            })
        metadata = json.loads(encoded_metadata)
        if len(encoded_metadata) > _METADATA_LIMIT:
            metadata = {"metadata_truncated": True, "truncated": True}
        was_truncated = metadata.get("truncated") is True
        if len(content) > self.max_output_chars:
            original_length = len(content)
            available = self.max_output_chars - len(TRUNCATION_MARKER)
            head = available * 3 // 4
            tail = available - head
            content = content[:head] + TRUNCATION_MARKER + (content[-tail:] if tail else "")
            metadata["original_chars"] = original_length
            was_truncated = True
        metadata["truncated"] = was_truncated
        if len(json.dumps(metadata, ensure_ascii=True, allow_nan=False)) > _METADATA_LIMIT:
            metadata = {"metadata_truncated": True, "truncated": True}
        return Observation(call_id, status, content, metadata)

    def execute(self, call: ToolCall, context: ToolContext) -> Observation:
        if not isinstance(call, ToolCall):
            return self._observation("", "error", "无效的工具调用", {"error_code": "invalid_call"})
        call_id = call.call_id if isinstance(call.call_id, str) else ""
        if not call_id or len(call_id) > _MAX_CALL_ID or not isinstance(call.name, str):
            return self._observation(call_id[:_MAX_CALL_ID], "error", "调用 ID 或工具名称无效", {
                "error_code": "invalid_call", "call_id_truncated": len(call_id) > _MAX_CALL_ID,
            })
        tool = self._tools.get(call.name)
        if tool is None:
            return self._observation(call_id, "error", f"未知工具：{call.name}", {
                "error_code": "unknown_tool",
            })
        try:
            # Reject non-JSON values even under additionalProperties.
            json.dumps(call.arguments, allow_nan=False)
            _check_json_value(call.arguments)
            _validate(call.arguments, tool.parameters)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            return self._observation(call_id, "error", f"参数无效：{exc}", {
                "error_code": "invalid_arguments",
            })
        try:
            result = tool.handler(context, call.arguments)
            if (
                not isinstance(result, ToolOutput)
                or not isinstance(result.content, str)
                or not isinstance(result.metadata, dict)
            ):
                raise ToolError("invalid_tool_output", "工具必须返回 ToolOutput 和 JSON 元数据")
            return self._observation(call_id, "ok", result.content, result.metadata)
        except subprocess.TimeoutExpired as exc:
            partial = _partial_output(exc.stdout)
            stderr = _partial_output(exc.stderr)
            message = f"工具执行超时（{exc.timeout} 秒）"
            if partial:
                message += f"\n标准输出：\n{partial}"
            if stderr:
                message += f"\n标准错误：\n{stderr}"
            return self._observation(call_id, "timeout", message, {
                "error_code": "timeout", "timeout_seconds": exc.timeout,
            })
        except TimeoutError as exc:
            return self._observation(call_id, "timeout", f"工具执行超时：{exc}", {"error_code": "timeout"})
        except ToolError as exc:
            return self._observation(call_id, "error", str(exc), {"error_code": exc.code})
        except PermissionError as exc:
            return self._observation(call_id, "error", f"权限不足：{exc}", {"error_code": "permission_denied"})
        except FileNotFoundError as exc:
            return self._observation(call_id, "error", f"文件不存在：{exc}", {"error_code": "not_found"})
        except UnicodeError as exc:
            return self._observation(call_id, "error", f"文本编码错误：{exc}", {"error_code": "invalid_encoding"})
        except ValueError as exc:
            return self._observation(call_id, "error", f"操作无效：{exc}", {"error_code": "invalid_value"})
        except Exception as exc:
            return self._observation(call_id, "error", f"工具执行失败：{exc}", {"error_code": "execution_error"})
