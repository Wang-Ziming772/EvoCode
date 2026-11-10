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


def _validate_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.query or parsed.fragment:
            raise ValueError
        _ = parsed.port
    except (TypeError, ValueError, AttributeError):
        raise ConfigError("EVOCODE_BASE_URL 必须是不含凭据或查询参数的 HTTP(S) 地址") from None


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
) -> ModelConfig:
    """Load .env without modifying os.environ; process variables take precedence."""
    path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
    try:
        from_file = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigError("无法读取模型配置文件") from None
    values: dict[str, str | None] = {**from_file, **os.environ}
    selected = profile if profile is not None else _required(values, "EVOCODE_DEFAULT_PROFILE")
    if not isinstance(selected, str) or selected not in _EFFORTS:
        raise ConfigError("模型配置档案必须是 gpt 或 deepseek")
    prefix = f"EVOCODE_{selected.upper()}_"
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
