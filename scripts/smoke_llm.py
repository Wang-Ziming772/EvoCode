"""Manual two-request smoke check; only calculator.py can be read."""

import argparse
import sys
from pathlib import Path

from evocode.models import ConfigError, LLMError, LLMProvider, load_model_config
from evocode.tasks.workspace import Workspace
from evocode.tools import ToolContext, create_default_registry


PROJECT = Path(__file__).resolve().parents[1]


class SmokeError(RuntimeError):
    pass


def run_smoke(profile: str, *, provider_factory=LLMProvider) -> str:
    config = load_model_config(profile, PROJECT / ".env")
    # A retry could turn two logical requests into more than two HTTP requests.
    if config.max_retries != 0:
        raise SmokeError("为保证最多两次请求，请将 EVOCODE_MAX_RETRIES 设置为 0")
    provider = provider_factory(config)
    registry = create_default_registry()
    tools = [spec for spec in registry.get_specs() if spec["name"] == "read_file"]
    messages = [{"role": "user", "content": (
        "请先调用 read_file 读取 calculator.py，暂时不要回答。"
    )}]
    with Workspace(PROJECT / "examples" / "repos" / "add_bug") as workspace:
        context = ToolContext(workspace)
        first = provider.generate(messages, tools)
        if not first.tool_calls:
            raise SmokeError("第一次响应没有按要求调用 read_file")
        for call in first.tool_calls:
            if call.name != "read_file" or call.arguments != {"path": "calculator.py"}:
                raise SmokeError("第一次响应包含不允许的工具或参数")
        observations = [registry.execute(call, context) for call in first.tool_calls]
        if any(item.status != "ok" for item in observations):
            raise SmokeError("read_file 调用失败或参数无效")
        print("第一次请求通过：已通过 read_file 读取 calculator.py。")
        messages.extend(provider.tool_results(first, observations))
        messages.append({"role": "user", "content": "请简短说明代码问题并结束，不要再调用工具。"})
        second = provider.generate(messages, tools)
        if second.tool_calls:
            raise SmokeError("第二次响应仍要求调用工具")
        if not second.content.strip():
            raise SmokeError("第二次响应没有提供文本说明")
        # Never print a secret even if a gateway or model echoes it.
        explanation = second.content
        for value in (config.api_key, config.model_id, config.base_url):
            explanation = explanation.replace(value, "[REDACTED]")
        return explanation


def main() -> int:
    parser = argparse.ArgumentParser(description="手动验证一次模型工具往返")
    parser.add_argument("--profile", required=True, choices=("gpt", "deepseek"))
    args = parser.parse_args()
    try:
        explanation = run_smoke(args.profile)
    except (ConfigError, LLMError, SmokeError, OSError, ValueError):
        # Config/model errors already use safe messages; never print arbitrary
        # OS or third-party exception strings that might contain credentials.
        exc = sys.exception()
        if isinstance(exc, (ConfigError, LLMError, SmokeError)):
            print(f"验证失败：{exc}", file=sys.stderr)
        else:
            print("验证失败：无法准备或清理工作区", file=sys.stderr)
        return 1
    except Exception:
        print("验证失败：发生未预期的模型或工具错误", file=sys.stderr)
        return 1
    print("验证成功：已读取 calculator.py，并完成两次模型请求。")
    print(explanation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
