"""Bounded, credential-aware run artifacts and an append-only event stream."""

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import dotenv_values


_SECRET_NAMES = ("EVOCODE_GPT_API_KEY", "EVOCODE_DEEPSEEK_API_KEY")
_FORBIDDEN_FIELDS = frozenset({"reasoning_content", "encrypted_content", "authorization", "api_key",
                               "access_token", "refresh_token", "password", "secret"})
_AUTH = re.compile(r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*[\"']?(?:(?:bearer|basic|digest|token)\s+)?)[^\s\"',}]+")
# A log can be conservatively masked, but a patch must remain byte-for-byte
# intact. Only treat explicit Authorization values as credentials here; a
# variable, formatted string, or concatenated scheme is not a literal token.
_PATCH_AUTH_LITERAL = re.compile(
    r"(?i)\bauthorization\b[\"']?\s*[:=]\s*(?P<prefix>[rubf]{0,3})"
    r"(?P<quote>[\"'])(?P<value>[^\"'\r\n]*)(?P=quote)"
)
_PATCH_AUTH_HEADER = re.compile(
    r"(?im)^\+\s*authorization\s*:\s*(?:bearer|basic|digest|token)\s+(?P<value>[^\s\"',}]+)"
)
_PATCH_STRING = re.compile(
    r"(?i)^(?P<prefix>[rubf]{0,3})(?P<value>\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*')"
)
_MARKER = "...[已截断]..."
_TEXT_LIMIT = 4000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactError(OSError):
    """A run artifact could not be saved safely."""


def _patch_string_suffix(tail: str) -> tuple[bool, str]:
    """Distinguish literal concatenation from a runtime expression."""
    pieces = []
    for _ in range(40):
        tail = tail.lstrip()
        if tail.startswith("%") or re.match(r"^\.format\s*\(", tail):
            return True, ""
        if not tail.startswith("+"):
            return False, "".join(pieces)
        tail = tail[1:].lstrip()
        match = _PATCH_STRING.match(tail)
        if not match:
            return True, ""
        piece = match.group("value")[1:-1]
        if "f" in match.group("prefix").casefold() and "{" in piece and "}" in piece:
            return True, ""
        pieces.append(piece)
        tail = tail[match.end():]
    # Do not classify arbitrarily long expressions as literal credentials.
    return True, ""


class Redactor:
    def __init__(self, env_file: Path) -> None:
        try:
            file_values = dotenv_values(env_file, encoding="utf-8-sig", interpolate=False)
        except (OSError, UnicodeError):
            file_values = {}
        values = [source.get(name) for source in (file_values, os.environ) for name in _SECRET_NAMES]
        variants = set()
        for value in values:
            if isinstance(value, str) and value:
                variants.update((value, quote(value, safe=""), json.dumps(value, ensure_ascii=True)[1:-1]))
        self._secrets = sorted((value for value in variants if value), key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        return _AUTH.sub(r"\1[REDACTED]", value)

    def contains_credential(self, value: str) -> bool:
        if any(secret in value for secret in self._secrets):
            return True
        for match in _PATCH_AUTH_LITERAL.finditer(value):
            initial = match.group("value")
            tail = value[match.end():].split("\n", 1)[0]
            # A string prefix followed by an expression is not itself a
            # credential. Keep rejecting a complete literal Bearer value.
            is_expression, suffix = _patch_string_suffix(tail)
            complete_scheme = re.match(r"(?i)^(?:bearer|basic|digest|token)\s+\S+", initial.strip())
            if is_expression and not complete_scheme:
                continue
            literal = (initial + suffix).strip()
            # Formatting placeholders and an incomplete scheme are code or
            # template fragments, rather than a hard-coded credential value.
            if (not literal or literal.casefold() in {"bearer", "basic", "digest", "token"}
                    or ("{" in literal and "}" in literal) or "%s" in literal):
                continue
            return True
        for match in _PATCH_AUTH_HEADER.finditer(value):
            literal = match.group("value")
            if not literal.startswith(("{", "$", "<", "%")):
                return True
        return False

    def summary(self, value: str, limit: int = _TEXT_LIMIT) -> tuple[str, bool]:
        value = self.text(value)
        if len(value) <= limit:
            return value, False
        available = max(0, limit - len(_MARKER))
        head = available * 3 // 4
        tail = available - head
        return value[:head] + _MARKER + (value[-tail:] if tail else ""), True

    def json_value(self, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[层级已截断]"
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float:
            return value if math.isfinite(value) else None
        if isinstance(value, str):
            return self.summary(value, 1000)[0]
        if isinstance(value, list):
            return [self.json_value(item, depth + 1) for item in value[:50]] + (
                ["[列表已截断]"] if len(value) > 50 else []
            )
        if isinstance(value, dict):
            safe = {}
            for key, item in islice(value.items(), 50):
                if not isinstance(key, str) or key.casefold() in _FORBIDDEN_FIELDS:
                    continue
                safe[self.summary(key, 128)[0]] = self.json_value(item, depth + 1)
            if len(value) > 50:
                safe["_truncated"] = True
            return safe
        return "[不支持的值]"


def _usage(value: Any, redactor: Redactor, depth: int = 0, budget: list[int] | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if depth > 4:
        return {"_truncated": True}
    if budget is None:
        budget = [200]
    result = {}
    truncated = len(value) > 50
    for name, item in islice(value.items(), 50):
        if not isinstance(name, str) or name.casefold() in _FORBIDDEN_FIELDS:
            continue
        if budget[0] <= 0:
            truncated = True
            break
        budget[0] -= 1
        key = redactor.summary(name, 128)[0]
        if item is None or type(item) is int or (type(item) is float and math.isfinite(item)):
            result[key] = item
        elif isinstance(item, dict):
            result[key] = _usage(item, redactor, depth + 1, budget)
        else:
            truncated = True
    if truncated:
        result["_truncated"] = True
    return result


def _arguments(name: str, arguments: Any, redactor: Redactor) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {"invalid_type": type(arguments).__name__}
    result: dict[str, Any] = {"argument_count": len(arguments)}
    for key in ("path", "query"):
        value = arguments.get(key)
        if isinstance(value, str):
            result[key], result[f"{key}_truncated"] = redactor.summary(value, 256)
    if "content" in arguments:
        value = arguments["content"]
        result["content_length"] = len(value) if isinstance(value, str) else None
    other_keys = list(islice((key for key in arguments if isinstance(key, str)
                              and key not in {"path", "query", "content"}), 21))
    if other_keys:
        result["other_keys"] = [redactor.summary(key, 128)[0] for key in other_keys[:20]
                                if key.casefold() not in _FORBIDDEN_FIELDS]
        if len(other_keys) > 20:
            result["other_keys_truncated"] = True
    return result


def observation_summary(observation: Any, redactor: Redactor) -> dict[str, Any] | None:
    if observation is None:
        return None
    content, clipped = redactor.summary(observation.content)
    return {"call_id": redactor.summary(observation.call_id, 256)[0], "status": observation.status,
            "content": content, "content_truncated": clipped,
            "metadata": redactor.json_value(observation.metadata)}


class RunArtifacts:
    def __init__(self, root: Path, redactor: Redactor) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.path = root / self.run_id
        self.path.mkdir()
        self.redactor = redactor
        self.trajectory_path = self.path / "trajectory.jsonl"
        self.patch_path = self.path / "patch.diff"
        self.result_path = self.path / "result.json"
        self.model_requests = 0
        self.tool_calls = 0
        self._trajectory = self.trajectory_path.open("x", encoding="utf-8", newline="\n")
        try:
            with self.patch_path.open("xb"):
                pass
        except BaseException:
            self._trajectory.close()
            raise

    def write_result(self, value: dict[str, Any]) -> None:
        temp = self.path / ".result.tmp"
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as file:
                file.write(encoded)
                file.flush()
            os.replace(temp, self.result_path)
        finally:
            temp.unlink(missing_ok=True)

    def write_patch(self, patch: str) -> None:
        if self.redactor.contains_credential(patch):
            raise ArtifactError("补丁包含已知凭据，拒绝保存")
        data = patch.encode("utf-8", errors="surrogateescape")
        temp = self.path / ".patch.tmp"
        try:
            with temp.open("wb") as file:
                file.write(data)
                file.flush()
            os.replace(temp, self.patch_path)
        finally:
            temp.unlink(missing_ok=True)

    def event(self, value: dict[str, Any]) -> None:
        kind = value.get("type")
        event: dict[str, Any] = {"type": kind, "time": utc_now()}
        if kind == "model_response":
            event.update(request=value["request"], tool_count=value["tool_count"])
            event["content"], event["content_truncated"] = self.redactor.summary(value["content"])
            event["usage"] = _usage(value.get("usage"), self.redactor)
            if type(value.get("thinking_present")) is bool:
                event["thinking_present"] = value["thinking_present"]
            if type(value.get("input_estimate_bytes")) is int:
                event["input_estimate_bytes"] = value["input_estimate_bytes"]
        elif kind == "tool_call":
            event.update(origin=value["origin"], number=value["number"],
                         call_id=self.redactor.summary(value["call_id"], 256)[0],
                         name=self.redactor.summary(value["name"], 128)[0],
                         arguments=_arguments(value["name"], value["arguments"], self.redactor))
        elif kind == "observation":
            event.update(origin=value["origin"], number=value["number"],
                         name=self.redactor.summary(value["name"], 128)[0],
                         call_id=self.redactor.summary(value["call_id"], 256)[0], status=value["status"],
                         metadata=self.redactor.json_value(value["metadata"]))
            event["content"], event["content_truncated"] = self.redactor.summary(value["content"])
        else:
            for key, item in value.items():
                if key != "type":
                    event[key] = self.redactor.json_value(item)
        self._trajectory.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
        self._trajectory.flush()
        if kind == "model_request":
            self.model_requests = value["request"]
        elif kind == "tool_call":
            self.tool_calls = value["number"]

    def close(self) -> None:
        self._trajectory.close()
