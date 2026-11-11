"""Load only the selected gateway profile without exposing credentials."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values


Profile = Literal["gpt", "deepseek"]
LocalProfile = Literal["qwen4b", "qwen9b", "qwen2b_q8", "qwen2b_bf16", "qwen27b"]
LOCAL_PROFILES = ("qwen4b", "qwen9b", "qwen2b_q8", "qwen2b_bf16", "qwen27b")
_LOCAL_PROFILES = frozenset(LOCAL_PROFILES)
_EFFORTS = {
    "gpt": frozenset({"low", "medium", "high", "xhigh", "max"}),
    "deepseek": frozenset({"none", "low", "high", "max"}),
}


class ConfigError(ValueError):
    """A configuration error whose message contains no setting values."""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    profile: Profile
    base_url: str = field(repr=False)
    model_id: str = field(repr=False)
    api_key: str = field(repr=False)
    request_timeout_seconds: float
    max_output_tokens: int
    reasoning_effort: str
    max_retries: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or self.profile not in _EFFORTS:
            raise ConfigError("模型配置档案必须是 gpt 或 deepseek")
        _validate_base_url(self.base_url)
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ConfigError("模型 ID 不能为空")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ConfigError("模型密钥不能为空")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ConfigError("模型输出上限必须是正整数")
        if not isinstance(self.reasoning_effort, str) or self.reasoning_effort not in _EFFORTS[self.profile]:
            raise ConfigError("所选档案的思考强度无效")
        try:
            valid_timeout = (type(self.request_timeout_seconds) in (int, float)
                             and math.isfinite(self.request_timeout_seconds)
                             and self.request_timeout_seconds > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ConfigError("模型请求超时必须是有限正数")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ConfigError("模型重试次数必须是非负整数")


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    profile: LocalProfile
    base_url: str = field(repr=False)
    model_id: str
    think: bool
    num_ctx: int
    max_output_tokens: int
    request_timeout_seconds: float
    max_steps: int
    max_tool_calls: int
    max_retries: int
    keep_alive: str
    unload_base_urls: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.profile not in _LOCAL_PROFILES:
            raise ConfigError("本地模型配置档案无效")
        _validate_base_url(self.base_url, f"EVOCODE_{self.profile.upper()}_BASE_URL 或 EVOCODE_OLLAMA_BASE_URL")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ConfigError("本地模型 ID 不能为空")
        if type(self.think) is not bool:
            raise ConfigError("本地模型 THINK 必须是布尔值")
        for name in ("num_ctx", "max_output_tokens", "max_steps", "max_tool_calls"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ConfigError(f"{name} 必须是正整数")
        if self.max_output_tokens >= self.num_ctx:
            raise ConfigError("本地模型输出上限必须小于上下文容量")
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ConfigError("本地模型重试次数必须是非负整数")
        if (type(self.request_timeout_seconds) not in (int, float)
                or not math.isfinite(self.request_timeout_seconds) or self.request_timeout_seconds <= 0):
            raise ConfigError("本地模型请求超时必须是有限正数")
        if not isinstance(self.keep_alive, str) or not re.fullmatch(r"(?:-1|0|[0-9]+(?:ms|s|m|h)(?:[0-9]+(?:ms|s|m|h))*)", self.keep_alive):
            raise ConfigError("EVOCODE_OLLAMA_KEEP_ALIVE 格式无效")

    @property
    def sampling(self) -> dict[str, int | float]:
        return {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 0.0, "repeat_penalty": 1.0}


def _validate_base_url(base_url: str, name: str = "EVOCODE_BASE_URL") -> None:
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.query or parsed.fragment:
            raise ValueError
        _ = parsed.port
    except (TypeError, ValueError, AttributeError):
        raise ConfigError(f"{name} 必须是不含凭据或查询参数的 HTTP(S) 地址") from None


def _ollama_unload_base_urls(values: dict[str, str | None]) -> tuple[str, ...]:
    extra_key = "EVOCODE_OLLAMA_UNLOAD_BASE_URLS"
    extra = [url.strip() for url in (values.get(extra_key) or "").split(",") if url.strip()]
    for url in extra:
        _validate_base_url(url, extra_key)
    candidates = [values.get("EVOCODE_OLLAMA_BASE_URL", "http://127.0.0.1:11434")]
    candidates.extend(values.get(f"EVOCODE_{profile.upper()}_BASE_URL") for profile in LOCAL_PROFILES)
    candidates.extend(extra)
    addresses = {}
    for url in candidates:
        if url is None:
            continue
        try:
            _validate_base_url(url)
        except ConfigError:
            # An invalid address for a different profile cannot block this one.
            continue
        addresses[url.rstrip("/")] = None
    return tuple(addresses)


def _required(values: dict[str, str | None], name: str) -> str:
    value = values.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"缺少必需配置：{name}")
    return value


def _integer(values: dict[str, str | None], name: str, *, minimum: int) -> int:
    value = _required(values, name)
    if not re.fullmatch(r"[0-9]+", value):
        raise ConfigError(f"{name} 必须是大于等于 {minimum} 的整数")
    try:
        result = int(value)
    except ValueError:
        raise ConfigError(f"{name} 必须是大于等于 {minimum} 的整数") from None
    if result < minimum:
        raise ConfigError(f"{name} 必须是大于等于 {minimum} 的整数")
    return result


def load_model_config(
    profile: str | None = None, env_file: str | os.PathLike[str] | None = None,
) -> ModelConfig | OllamaConfig:
    """Load .env without modifying os.environ; process variables take precedence."""
    path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
    try:
        from_file = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError("无法读取模型配置文件") from None
    values: dict[str, str | None] = {**from_file, **os.environ}
    selected = profile if profile is not None else _required(values, "EVOCODE_DEFAULT_PROFILE")
    if not isinstance(selected, str) or selected not in _EFFORTS.keys() | _LOCAL_PROFILES:
        raise ConfigError("模型配置档案必须是 gpt 或 deepseek，或受支持的本地档案")
    prefix = f"EVOCODE_{selected.upper()}_"
    if selected in _LOCAL_PROFILES:
        think_text = _required(values, prefix + "THINK").strip().lower()
        if think_text not in {"true", "false"}:
            raise ConfigError(f"{prefix}THINK 必须是 true 或 false")
        timeout_name = prefix + "REQUEST_TIMEOUT_SECONDS"
        timeout_text = _required(values, timeout_name)
        try:
            timeout = float(timeout_text)
        except ValueError:
            raise ConfigError(f"{timeout_name} 必须是有限正数") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(f"{timeout_name} 必须是有限正数")
        return OllamaConfig(
            profile=selected, base_url=values.get(prefix + "BASE_URL", values.get("EVOCODE_OLLAMA_BASE_URL", "http://127.0.0.1:11434")),
            model_id=_required(values, prefix + "MODEL_ID"), think=think_text == "true",
            num_ctx=_integer(values, prefix + "NUM_CTX", minimum=1),
            max_output_tokens=_integer(values, prefix + "MAX_OUTPUT_TOKENS", minimum=1),
            request_timeout_seconds=timeout,
            max_steps=_integer(values, prefix + "MAX_STEPS", minimum=1) if prefix + "MAX_STEPS" in values else 10,
            max_tool_calls=_integer(values, prefix + "MAX_TOOL_CALLS", minimum=1) if prefix + "MAX_TOOL_CALLS" in values else 30,
            max_retries=_integer(values, prefix + "MAX_RETRIES", minimum=0),
            keep_alive=values.get("EVOCODE_OLLAMA_KEEP_ALIVE", "5m"),
            unload_base_urls=_ollama_unload_base_urls(values),
        )
    base_url = _required(values, "EVOCODE_BASE_URL")
    _validate_base_url(base_url)
    model_id = _required(values, prefix + "MODEL_ID")
    api_key = _required(values, prefix + "API_KEY")
    maximum = _integer(values, prefix + "MAX_OUTPUT_TOKENS", minimum=1)
    effort = _required(values, prefix + "REASONING_EFFORT")
    if effort not in _EFFORTS[selected]:
        allowed = ", ".join(sorted(_EFFORTS[selected]))
        raise ConfigError(f"{prefix}REASONING_EFFORT 必须是：{allowed}")
    timeout_text = _required(values, "EVOCODE_REQUEST_TIMEOUT_SECONDS")
    try:
        timeout = float(timeout_text)
    except ValueError:
        raise ConfigError("EVOCODE_REQUEST_TIMEOUT_SECONDS 必须是正数") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("EVOCODE_REQUEST_TIMEOUT_SECONDS 必须是有限正数")
    retries = _integer(values, "EVOCODE_MAX_RETRIES", minimum=0)
    return ModelConfig(
        profile=selected, base_url=base_url, model_id=model_id, api_key=api_key,
        request_timeout_seconds=timeout, max_output_tokens=maximum,
        reasoning_effort=effort, max_retries=retries,
    )
