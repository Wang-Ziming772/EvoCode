"""LiteLLM adapters for native Responses and Chat Completions protocols."""

import copy
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from evocode.models.config import ConfigError, ModelConfig
from evocode.tools import Observation, ToolCall


class LLMError(RuntimeError):
    """A sanitized model request failure."""


class LLMTimeoutError(LLMError):
    pass


class LLMProtocolError(LLMError):
    pass


class _SecretFilter(logging.Filter):
    def __init__(self, *secrets: str) -> None:
        super().__init__()
        variants = set()
        for secret in secrets:
            variants.update((secret, quote(secret, safe=""), json.dumps(secret)[1:-1]))
        self._variants = sorted(variants, key=len, reverse=True)

    def _redact(self, value: str) -> str:
        for secret in self._variants:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def _redact_extra(self, value: Any, depth: int = 0) -> Any:
        if depth > 10:
            return "[日志字段已屏蔽]"
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {self._redact_extra(k, depth + 1): self._redact_extra(v, depth + 1)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_extra(item, depth + 1) for item in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = self._redact(record.exc_text)
            if record.stack_info:
                record.stack_info = self._redact(record.stack_info)
            for key in set(record.__dict__) - _STANDARD_LOG_FIELDS:
                record.__dict__[key] = self._redact_extra(record.__dict__[key])
        except Exception:
            record.msg, record.args, record.exc_info, record.exc_text, record.stack_info = (
                "[日志已屏蔽]", (), None, None, None
            )
        return True


_STANDARD_LOG_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


@contextmanager
def _redact_library_logs(*secrets: str):
    """Mask the selected key even when SDK debug logging is enabled."""
    prefixes = ("LiteLLM", "openai", "httpx", "httpcore")
    names = {"LiteLLM", "openai._base_client", "httpx", "httpcore.http11", "httpcore.connection"}
    names.update(name for name in logging.Logger.manager.loggerDict if name.startswith(prefixes))
    loggers = [logging.getLogger(name) for name in names]
    secret_filter = _SecretFilter(*secrets)
    for logger in loggers:
        logger.addFilter(secret_filter)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(secret_filter)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: dict[str, Any] | None = None
    # Plain protocol data, never an SDK object. Responses reasoning items and
    # DeepSeek reasoning_content must survive the tool-result round trip.
    assistant_items: list[dict[str, Any]] = field(default_factory=list, repr=False)
    protocol: str = ""


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise LLMProtocolError("模型返回了无法转换的协议数据")


def _arguments(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise LLMProtocolError("工具调用参数必须是 JSON 字符串")
    invalid = False
    try:
        def reject_constant(_value: str) -> None:
            raise ValueError("non-finite JSON number")

        parsed = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        invalid = True
        parsed = None
    if invalid:
        # JSONDecodeError retains the original argument string; do not attach
        # it as exception context where it could expose sensitive content.
        raise LLMProtocolError("工具调用参数不是有效的 JSON")
    if not isinstance(parsed, dict):
        raise LLMProtocolError("工具调用参数必须是 JSON 对象")
    return parsed


def _tool_call(call_id: Any, name: Any, arguments: Any) -> ToolCall:
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        raise LLMProtocolError("工具调用缺少有效的调用 ID 或工具名称")
    return ToolCall(call_id=call_id, name=name, arguments=_arguments(arguments))


def _usage(data: dict[str, Any]) -> dict[str, Any] | None:
    usage = data.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise LLMProtocolError("模型返回的 usage 格式无效")
    return usage


def _check_ids(calls: list[ToolCall]) -> None:
    ids = [call.call_id for call in calls]
    if len(ids) != len(set(ids)):
        raise LLMProtocolError("模型返回了重复的工具调用 ID")


def _parse_responses(raw: Any) -> LLMResponse:
    data = _plain(raw)
    if not isinstance(data, dict) or not isinstance(data.get("output"), list):
        raise LLMProtocolError("Responses 响应缺少 output 列表")
    if data.get("status") not in (None, "completed"):
        raise LLMProtocolError("Responses 响应未完成")
    text: list[str] = []
    calls: list[ToolCall] = []
    items = data["output"]
    for item in items:
        if not isinstance(item, dict):
            raise LLMProtocolError("Responses 输出项格式无效")
        if item.get("type") == "function_call":
            calls.append(_tool_call(item.get("call_id"), item.get("name"), item.get("arguments")))
        elif item.get("type") == "message":
            parts = item.get("content", [])
            if not isinstance(parts, list):
                raise LLMProtocolError("Responses 文本内容格式无效")
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise LLMProtocolError("Responses 文本内容格式无效")
                    text.append(part["text"])
    _check_ids(calls)
    return LLMResponse("".join(text), calls, _usage(data), copy.deepcopy(items), "responses")


def _parse_chat(raw: Any) -> LLMResponse:
    data = _plain(raw)
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise LLMProtocolError("Chat Completions 响应缺少 choices")
    choice = data["choices"][0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise LLMProtocolError("Chat Completions 响应缺少 assistant 消息")
    if choice.get("finish_reason") not in (None, "stop", "tool_calls"):
        raise LLMProtocolError("Chat Completions 响应未正常完成")
    message = choice["message"]
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise LLMProtocolError("Chat Completions 文本内容格式无效")
    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise LLMProtocolError("Chat Completions 工具调用格式无效")
    calls: list[ToolCall] = []
    for item in tool_calls:
        if not isinstance(item, dict) or item.get("type") != "function" or not isinstance(item.get("function"), dict):
            raise LLMProtocolError("不支持的 Chat Completions 工具调用")
        function = item["function"]
        calls.append(_tool_call(item.get("id"), function.get("name"), function.get("arguments")))
    _check_ids(calls)
    message = copy.deepcopy(message)
    message["role"] = "assistant"
    if calls and "content" not in message:
        message["content"] = None
    return LLMResponse(content or "", calls, _usage(data), [message], "chat_completions")


def _specs(tools: list[dict[str, Any]], *, responses: bool) -> list[dict[str, Any]]:
    converted = []
    for spec in tools:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str) or not isinstance(spec.get("description"), str) or not isinstance(spec.get("parameters"), dict):
            raise LLMProtocolError("工具定义格式无效")
        function = {"name": spec["name"], "description": spec["description"],
                    "parameters": copy.deepcopy(spec["parameters"])}
        if responses:
            converted.append({"type": "function", **function, "strict": False})
        else:
            converted.append({"type": "function", "function": function})
    return converted


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if type(status) is int and 100 <= status <= 599 else None


def _timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def _retryable(exc: Exception) -> bool:
    status = _status_code(exc)
    return _timeout(exc) or isinstance(exc, ConnectionError) or "connection" in type(exc).__name__.lower() or status in {408, 429, 500, 502, 503, 504}


def _failure(exc: Exception, config: ModelConfig) -> LLMError:
    status = _status_code(exc)
    if _timeout(exc) or status == 408:
        return LLMTimeoutError("模型请求超时；请检查网关和超时配置")
    explanations = {
        400: "网关拒绝请求；请检查协议、模型及参数是否受支持",
        401: "网关认证失败",
        403: "网关拒绝访问",
        404: "网关接口或模型不存在",
        408: "网关请求超时",
        429: "网关限流或额度不足",
    }
    if status is not None:
        detail = explanations.get(status, "网关返回错误")
        # Prefer the structured gateway message. Some LiteLLM errors lose the
        # original response object, so their text is the fallback; redact it
        # before surfacing it and never expose an unbounded server response.
        message = None
        try:
            payload = getattr(exc, "response").json()
            error = payload.get("error") if isinstance(payload, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
        except Exception:
            pass
        if not isinstance(message, str) or not message.strip():
            try:
                message = str(exc)
            except Exception:
                message = ""
        if message.strip():
            message = _SecretFilter(config.api_key, config.model_id, config.base_url)._redact(message)
            detail += f"：{' '.join(message.split())[:500]}"
        return LLMError(f"{detail}（HTTP {status}）")
    return LLMError("模型请求失败；请检查网关连接及配置")


class LLMProvider:
    """Provider-independent entry point; protocol selection follows the profile."""

    def __init__(self, config: ModelConfig, *, client: Any = None) -> None:
        if config.profile not in {"gpt", "deepseek"}:
            raise ConfigError("模型配置档案必须是 gpt 或 deepseek")
        self.config = config
        self._client = client
        self._injected_client = client is not None

    def _get_client(self) -> Any:
        if self._client is None:
            # Avoid an unrelated network fetch of LiteLLM's model-cost map.
            os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
            import litellm

            self._client = litellm
        return self._client

    def _chat_request(self, invoke: Any, request: dict[str, Any]) -> Any:
        if self._injected_client:
            return invoke(**request)

        # LiteLLM fills missing Chat Completions usage with zeroes. Capture
        # the actual response instead, which also preserves provider-specific
        # assistant fields such as reasoning_content during tool calls.
        import httpx
        from openai import OpenAI

        captured: list[dict[str, Any]] = []

        def capture(response: httpx.Response) -> None:
            if response.is_success:
                try:
                    payload = response.json() if response.is_closed else json.loads(response.read())
                except (ValueError, UnicodeError):
                    return
                if isinstance(payload, dict):
                    captured.append(payload)

        with httpx.Client(event_hooks={"response": [capture]}, follow_redirects=True) as transport:
            with OpenAI(
                api_key=self.config.api_key, base_url=self.config.base_url,
                timeout=self.config.request_timeout_seconds, max_retries=0, http_client=transport,
            ) as sdk_client:
                invoke(**request, client=sdk_client)
        if not captured:
            raise LLMProtocolError("无法读取网关的原始 Chat Completions 响应")
        return captured[-1]

    def generate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
            raise LLMProtocolError("模型输入必须是非空消息列表")
        specs = _specs(tools or [], responses=self.config.profile == "gpt")
        common: dict[str, Any] = {
            # Only the first prefix is LiteLLM routing; the gateway receives
            # the original model ID, including any slash it may contain.
            "model": f"openai/{self.config.model_id}",
            "api_base": self.config.base_url,
            "api_key": self.config.api_key,
            "timeout": self.config.request_timeout_seconds,
            "num_retries": 0,
            "max_retries": 0,
            "drop_params": False,
            "stream": False,
        }
        client = self._get_client()
        if self.config.profile == "gpt":
            request = {**common, "input": copy.deepcopy(messages),
                       "max_output_tokens": self.config.max_output_tokens,
                       "reasoning": {"effort": self.config.reasoning_effort},
                       "store": False, "include": ["reasoning.encrypted_content"]}
            invoke = client.responses
        else:
            request = {**common, "messages": copy.deepcopy(messages),
                       "max_tokens": self.config.max_output_tokens,
                       "reasoning_effort": self.config.reasoning_effort}
            invoke = client.completion
        if specs:
            request["tools"] = specs
        # LiteLLM's internal retries stay disabled; only this bounded loop uses
        # the configured count, so total request attempts are explicit.
        failure: LLMError | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with _redact_library_logs(self.config.api_key, self.config.model_id, self.config.base_url):
                    result = invoke(**request) if self.config.profile == "gpt" else self._chat_request(invoke, request)
                break
            except LLMProtocolError:
                raise
            except Exception as exc:
                if attempt < self.config.max_retries and _retryable(exc):
                    continue
                failure = _failure(exc, self.config)
                break
        if failure is not None:
            # Raise outside the handler so the original SDK exception (which
            # can contain secrets) is not retained in __context__ either.
            raise failure
        try:
            return _parse_responses(result) if self.config.profile == "gpt" else _parse_chat(result)
        except LLMProtocolError:
            raise
        except Exception:
            pass
        raise LLMProtocolError("模型响应格式无效")

    def tool_results(self, response: LLMResponse, observations: list[Observation]) -> list[dict[str, Any]]:
        """Return the original assistant items and matching tool-result items."""
        expected_protocol = "responses" if self.config.profile == "gpt" else "chat_completions"
        if response.protocol != expected_protocol or not response.tool_calls:
            raise LLMProtocolError("响应不包含当前协议的工具调用")
        expected = [call.call_id for call in response.tool_calls]
        received = [item.call_id for item in observations]
        if len(received) != len(expected) or len(set(received)) != len(received) or set(received) != set(expected):
            raise LLMProtocolError("工具结果与调用 ID 不匹配")
        by_id = {item.call_id: item for item in observations}
        items = copy.deepcopy(response.assistant_items)
        for call_id in expected:
            observation = by_id[call_id]
            try:
                output = json.dumps({"status": observation.status, "content": observation.content,
                                     "metadata": observation.metadata}, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, OverflowError, RecursionError):
                raise LLMProtocolError("工具结果无法序列化为 JSON") from None
            if expected_protocol == "responses":
                items.append({"type": "function_call_output", "call_id": call_id, "output": output})
            else:
                items.append({"role": "tool", "tool_call_id": call_id, "content": output})
        return items
