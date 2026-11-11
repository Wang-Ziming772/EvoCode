"""Native Ollama chat adapter; no cloud SDK or proxy environment is used."""

import copy
import hashlib
import json
import math
import re
import time
import uuid
from typing import Any

import httpx

from evocode.models.config import OllamaConfig
from evocode.models.provider import (LLMContextError, LLMEmptyResponseError, LLMIncompleteError, LLMLocalError,
                                     OllamaProtocolError as LLMProtocolError, LLMResponse, OllamaTimeoutError, _specs)
from evocode.tools import Observation, ToolCall


_METRICS = ("prompt_eval_count", "eval_count", "total_duration", "load_duration",
            "prompt_eval_duration", "eval_duration", "prompt_eval_cached_count")
_TURN_MARKER = "_evocode_ollama_turn_id"
_UNLOAD_WAIT_SECONDS = 10.0


def _local_server_key(url: str) -> httpx.URL | None:
    address = httpx.URL(url.rstrip("/"))
    if address.host == "localhost":
        return address.copy_with(host="127.0.0.1")
    return address if address.host in {"127.0.0.1", "::1"} else None


def _input_overflow(response: httpx.Response) -> dict[str, Any] | None:
    if response.status_code != 400:
        return None
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, str):
            # Ollama may put the runner's JSON error inside the error string.
            wrapper = json.loads(error)
            error = wrapper.get("error") if isinstance(wrapper, dict) else None
    except (ValueError, RecursionError):
        return None
    return error if isinstance(error, dict) and error.get("type") == "exceed_context_size_error" else None


class OllamaProvider:
    def __init__(self, config: OllamaConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport
        # Keep only the latest valid tool turn. Markers survive the controller's
        # deep copies and are stripped before the request leaves this provider.
        self._turn_thinking: dict[str, tuple[str, str]] = {}
        self._last_tool_ids: tuple[str, ...] | None = None
        self._last_thinking = ""
        self.last_thinking_present: bool | None = None
        self.last_usage: dict[str, Any] | None = None
        self.last_response_metadata: dict[str, Any] | None = None
        self.last_input_estimate_bytes: int | None = None
        self.usage_totals: dict[str, int] = {}
        self.usage_observed_calls: dict[str, int] = {}
        self.runtime: dict[str, Any] = {"ollama_version": None, "model_digest": None, "actual_num_ctx": None}

    def _client(self, timeout: float, *, base_url: str | None = None) -> httpx.Client:
        return httpx.Client(base_url=(base_url or self.config.base_url).rstrip("/"), trust_env=False,
                            timeout=httpx.Timeout(timeout), transport=self._transport)

    def check_available(self) -> dict[str, Any]:
        try:
            with self._client(min(self.config.request_timeout_seconds, 10)) as client:
                version = client.get("/api/version")
                tags = client.get("/api/tags")
                version.raise_for_status()
                tags.raise_for_status()
                version_data, tags_data = version.json(), tags.json()
        except httpx.TimeoutException:
            raise OllamaTimeoutError("Ollama 服务检查超时") from None
        except (httpx.HTTPError, ValueError):
            raise LLMLocalError("无法连接 Ollama 服务或读取本地模型列表") from None
        if not isinstance(version_data, dict) or not isinstance(tags_data, dict) or not isinstance(tags_data.get("models"), list):
            raise LLMLocalError("Ollama 服务返回的模型列表无效")
        model = next((item for item in tags_data["models"] if isinstance(item, dict)
                      and item.get("name") == self.config.model_id), None)
        if model is None:
            raise LLMLocalError("所选 Ollama 本地模型不存在；请检查模型 ID")
        self.runtime["ollama_version"] = version_data.get("version") if isinstance(version_data.get("version"), str) else None
        self.runtime["model_digest"] = model.get("digest") if isinstance(model.get("digest"), str) else None
        return dict(self.runtime)

    @staticmethod
    def _running_models(client: httpx.Client) -> list[dict[str, Any]]:
        response = client.get("/api/ps")
        response.raise_for_status()
        data = response.json()
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list) or any(
            not isinstance(model, dict) or not isinstance(model.get("name"), str) or not model["name"]
            for model in models
        ):
            raise ValueError("invalid Ollama running-model list")
        return models

    def prepare_for_run(self) -> list[dict[str, str]]:
        """Validate the selected model and release other local runners before generation."""
        self.check_available()
        selected = _local_server_key(self.config.base_url)
        if selected is None:
            return []
        digest = self.runtime.get("model_digest")
        released = []
        seen = set()
        for url in (self.config.base_url, *self.config.unload_base_urls):
            server = _local_server_key(url)
            if server is None or server in seen:
                continue
            seen.add(server)
            try:
                with self._client(5, base_url=url) as client:
                    try:
                        running = self._running_models(client)
                    except httpx.ConnectError:
                        if server != selected:
                            continue  # A stopped local service holds no loaded models.
                        raise
                    names = sorted({model["name"] for model in running if not (
                        server == selected and (model["name"] == self.config.model_id
                                                or (digest and model.get("digest") == digest))
                    )})
                    if not names:
                        continue
                    for name in names:
                        response = client.post("/api/generate", json={"model": name, "keep_alive": 0, "stream": False})
                        response.raise_for_status()
                    deadline = time.monotonic() + _UNLOAD_WAIT_SECONDS
                    pending = set(names)
                    while pending:
                        pending.intersection_update(model["name"] for model in self._running_models(client))
                        if not pending:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise LLMLocalError(f"Ollama {url} 的模型尚未卸载：{', '.join(sorted(pending))}")
                        time.sleep(min(0.25, remaining))
                    released.extend({"base_url": url, "model_id": name} for name in names)
            except (httpx.HTTPError, ValueError):
                raise LLMLocalError(f"无法确认 Ollama {url} 上的其他模型已卸载") from None
        return released

    @staticmethod
    def _input_estimate_bytes(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        try:
            serialized = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise LLMProtocolError("Ollama 输入无法序列化") from None
        # Keep the byte estimate for diagnostics. Ollama tokenizes the rendered
        # prompt and enforces its actual context limit.
        return len(serialized) + 128 * (len(messages) + len(tools)) + 2048

    @staticmethod
    def _message_fingerprint(message: dict[str, Any]) -> str:
        try:
            encoded = json.dumps(message, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise LLMProtocolError("Ollama 工具往返消息无法序列化") from None
        return hashlib.sha256(encoded).hexdigest()

    def _native_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        native = copy.deepcopy(messages)
        last_user = max((index for index, message in enumerate(native)
                         if message.get("role") == "user"), default=-1)
        last_tool = max((index for index, message in enumerate(native)
                         if index > last_user and message.get("role") == "assistant"
                         and message.get("tool_calls")), default=-1)
        active: dict[str, tuple[str, str]] = {}
        seen: set[str] = set()
        for index, message in enumerate(native):
            marker = message.pop(_TURN_MARKER, None)
            # Only thinking returned by this provider is eligible for replay.
            message.pop("thinking", None)
            if marker is None:
                continue
            if not isinstance(marker, str) or marker in seen:
                raise LLMProtocolError("Ollama 工具往返消息标识无效")
            seen.add(marker)
            if index <= last_user or index != last_tool:
                continue
            entry = self._turn_thinking.get(marker)
            if entry is None:
                # Older or reset histories may still carry a marker after its
                # thinking was discarded; the messages remain valid history.
                continue
            if (last_user < 0 or message.get("role") != "assistant" or not message.get("tool_calls")
                    or self._message_fingerprint(message) != entry[0]):
                raise LLMProtocolError("Ollama 工具往返消息与内存记录不匹配")
            active[marker] = entry
            if entry[1]:
                message["thinking"] = entry[1]
        # Forget old user turns and histories that were replaced or reset.
        self._turn_thinking = active
        return native

    def _observe_usage(self, data: dict[str, Any]) -> None:
        usage = {key: data[key] for key in _METRICS if type(data.get(key)) is int and data[key] >= 0}
        self.last_usage = usage or None
        for key, value in usage.items():
            self.usage_totals[key] = self.usage_totals.get(key, 0) + value
            self.usage_observed_calls[key] = self.usage_observed_calls.get(key, 0) + 1

    def _loaded_context(self) -> None:
        try:
            with self._client(min(self.config.request_timeout_seconds, 5)) as client:
                response = client.get("/api/ps")
                response.raise_for_status()
                data = response.json()
            models = data.get("models", []) if isinstance(data, dict) else []
            model = next((m for m in models if isinstance(m, dict) and m.get("name") == self.config.model_id), None)
            actual = model.get("context_length") if model else None
            if type(actual) is int and actual > 0:
                self.runtime["actual_num_ctx"] = actual
                if actual < self.config.num_ctx:
                    raise LLMContextError("Ollama 实际加载的上下文小于配置值，已停止")
        except LLMContextError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            # Some versions or keep_alive=0 do not expose a loaded model.
            return

    def generate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> LLMResponse:
        self.last_thinking_present = None
        self.last_usage = None
        self.last_response_metadata = None
        self.last_input_estimate_bytes = None
        self._last_thinking = ""
        self._last_tool_ids = None
        if not isinstance(messages, list) or not messages or not all(isinstance(item, dict) for item in messages):
            raise LLMProtocolError("Ollama 输入消息格式无效")
        native_tools = _specs(tools or [], responses=False)
        native_messages = self._native_messages(messages)
        self.last_input_estimate_bytes = self._input_estimate_bytes(native_messages, native_tools)
        request = {"model": self.config.model_id, "messages": native_messages, "tools": native_tools,
                   "stream": False, "think": self.config.think,
                   "keep_alive": int(self.config.keep_alive) if self.config.keep_alive in {"0", "-1"} else self.config.keep_alive,
                   "truncate": False, "shift": False,
                   "options": {"num_ctx": self.config.num_ctx, "num_predict": self.config.max_output_tokens,
                               **self.config.sampling}}
        data = None
        for attempt in range(self.config.max_retries + 1):
            try:
                with self._client(self.config.request_timeout_seconds) as client:
                    response = client.post("/api/chat", json=request)
                    response.raise_for_status()
                    data = response.json()
                break
            except httpx.TimeoutException:
                if attempt == self.config.max_retries:
                    raise OllamaTimeoutError("Ollama 请求超时") from None
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                overflow = _input_overflow(exc.response)
                if overflow is not None:
                    count, limit = overflow.get("n_prompt_tokens"), overflow.get("n_ctx")
                    detail = ""
                    if type(count) is int and count > 0 and type(limit) is int and limit > 0:
                        detail = f"（实际输入 {count} token，可用上下文 {limit} token）"
                    raise LLMContextError(f"Ollama 拒绝超出上下文容量的输入{detail}") from None
                if attempt < self.config.max_retries and code in {408, 429, 500, 502, 503, 504}:
                    continue
                if code == 404:
                    raise LLMLocalError("所选 Ollama 本地模型不存在") from None
                raise LLMLocalError(f"Ollama 请求失败（HTTP {code}）") from None
            except httpx.HTTPError:
                if attempt == self.config.max_retries:
                    raise LLMLocalError("无法连接 Ollama 服务") from None
            except ValueError:
                raise LLMProtocolError("Ollama 响应不是有效 JSON") from None
        if not isinstance(data, dict):
            raise LLMProtocolError("Ollama 响应格式无效")
        self._observe_usage(data)
        message = data.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise LLMProtocolError("Ollama 响应缺少 assistant 消息")
        thinking = message.get("thinking")
        content = message.get("content")
        thinking = "" if thinking is None else thinking
        content = "" if content is None else content
        if not isinstance(thinking, str) or not isinstance(content, str):
            raise LLMProtocolError("Ollama 响应文本格式无效")
        self.last_thinking_present = bool(thinking)
        reason = data.get("done_reason")
        raw_calls = message.get("tool_calls") or []
        self.last_response_metadata = {
            "done": data.get("done") if type(data.get("done")) is bool else None,
            "done_reason": reason if reason in ("stop", "length") else "other",
            "thinking_chars": len(thinking), "content_chars": len(content),
            "tool_call_count": len(raw_calls) if isinstance(raw_calls, list) else None,
        }
        self._loaded_context()
        if re.search(r"</?think>", content, re.IGNORECASE):
            raise LLMProtocolError("Ollama 响应未分离思考内容")
        if data.get("done") is not True or data.get("done_reason") != "stop":
            suffix = "，仅收到思考内容" if thinking and not content and not message.get("tool_calls") else ""
            raise LLMIncompleteError(f"Ollama 响应未完整结束或达到输出上限{suffix}")
        if not isinstance(raw_calls, list):
            raise LLMProtocolError("Ollama 工具调用格式无效")
        calls = []
        native_calls = []
        for raw in raw_calls:
            if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
                raise LLMProtocolError("Ollama 工具调用格式无效")
            function = raw["function"]
            name, arguments = function.get("name"), function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments, parse_constant=lambda _x: (_ for _ in ()).throw(ValueError()))
                except (ValueError, RecursionError):
                    raise LLMProtocolError("Ollama 工具参数不是有效 JSON 对象") from None
            try:
                if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                    raise ValueError
                json.dumps(arguments, allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                raise LLMProtocolError("Ollama 工具参数不是有效 JSON 对象") from None
            calls.append(ToolCall(uuid.uuid4().hex, name, arguments))
            native = copy.deepcopy(raw)
            native["function"]["arguments"] = copy.deepcopy(arguments)
            native_calls.append(native)
        if not calls and not content.strip():
            raise LLMEmptyResponseError("Ollama 仅返回思考内容或空响应，没有可执行调用或最终文本")
        assistant = {"role": "assistant", "content": content}
        if native_calls:
            assistant["tool_calls"] = native_calls
            self._last_thinking = thinking
            self._last_tool_ids = tuple(call.call_id for call in calls)
        else:
            self._turn_thinking.clear()
        return LLMResponse(content, calls, self.last_usage, [assistant], "ollama", bool(thinking))

    def tool_results(self, response: LLMResponse, observations: list[Observation]) -> list[dict[str, Any]]:
        if response.protocol != "ollama" or not response.tool_calls or len(response.assistant_items) != 1:
            raise LLMProtocolError("Ollama 响应不包含有效的工具调用")
        expected = [call.call_id for call in response.tool_calls]
        received = [item.call_id for item in observations]
        if (tuple(expected) != self._last_tool_ids or len(set(expected)) != len(expected)
                or len(received) != len(expected) or set(received) != set(expected)):
            raise LLMProtocolError("Ollama 工具结果与调用 ID 不匹配")
        assistant = copy.deepcopy(response.assistant_items[0])
        native_calls = assistant.get("tool_calls")
        if not isinstance(native_calls, list) or len(native_calls) != len(expected):
            raise LLMProtocolError("Ollama 原生工具调用消息不匹配")
        by_id = {item.call_id: item for item in observations}
        result = [assistant]
        for call in response.tool_calls:
            item = by_id[call.call_id]
            try:
                content = json.dumps({"status": item.status, "content": item.content, "metadata": item.metadata},
                                     ensure_ascii=False, allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                raise LLMProtocolError("Ollama 工具结果无法序列化") from None
            result.append({"role": "tool", "tool_name": call.name, "content": content})
        marker = uuid.uuid4().hex
        fingerprint = self._message_fingerprint(assistant)
        self._turn_thinking = {marker: (fingerprint, self._last_thinking)}
        assistant[_TURN_MARKER] = marker
        self._last_thinking = ""
        self._last_tool_ids = None
        return result
