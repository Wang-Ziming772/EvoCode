"""Two native Ollama requests with one read_file round trip; no file edits."""

import argparse
import json
import re
import time
import uuid
from pathlib import Path

from evocode.artifacts import Redactor
from evocode.models import OllamaConfig, OllamaProvider, load_model_config
from evocode.models.config import LOCAL_PROFILES
from evocode.tasks.workspace import Workspace
from evocode.tools import ToolContext, create_default_registry


PROJECT = Path(__file__).resolve().parents[1]


def run(profile: str, env_file: Path, output_root: Path, *, provider_factory=OllamaProvider) -> tuple[int, Path]:
    config = load_model_config(profile, env_file)
    if not isinstance(config, OllamaConfig):
        raise ValueError("必须选择 Ollama 本地档案")
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / uuid.uuid4().hex
    output.mkdir()
    provider = provider_factory(config)
    redactor = Redactor(env_file)
    record = {"profile": config.profile, "model_id": redactor.summary(config.model_id, 1000)[0],
              "base_url": redactor.summary(config.base_url, 1000)[0], "think": config.think,
              "num_ctx": config.num_ctx, "num_predict": config.max_output_tokens,
              "request_timeout_seconds": config.request_timeout_seconds, "sampling": config.sampling,
              "keep_alive": config.keep_alive, "status": "FAILED", "requests": [],
              "offered_tools": ["read_file"], "expected_path": "calculator.py"}
    start = time.monotonic()
    code = 1
    try:
        record["unloaded_models"] = redactor.json_value(provider.prepare_for_run())
        registry = create_default_registry()
        specs = [spec for spec in registry.get_specs() if spec["name"] == "read_file"]
        messages = [{"role": "user", "content": "请先调用 read_file 读取 calculator.py，暂时不要回答。"}]
        with Workspace(PROJECT / "examples" / "repos" / "add_bug") as workspace:
            context = ToolContext(workspace)
            def generate(stage):
                item = {"stage": stage, "status": "PENDING"}
                record["requests"].append(item)
                try:
                    response = provider.generate(messages, specs)
                except Exception:
                    item.update(status="error", thinking_present=getattr(provider, "last_thinking_present", None),
                                usage=getattr(provider, "last_usage", None),
                                response_metadata=getattr(provider, "last_response_metadata", None))
                    raise
                item.update(status="ok", thinking_present=response.thinking_present, usage=response.usage,
                            tool_count=len(response.tool_calls), content_chars=len(response.content),
                            tool_names=[call.name if call.name == "read_file" else "<other>" for call in response.tool_calls],
                            tool_markup_in_content=bool(re.search(r"<\|?/?tool_call(?:\|?>|\b)", response.content, re.IGNORECASE)),
                            response_metadata=getattr(provider, "last_response_metadata", None))
                return response

            first = generate("read_file")
            if not first.tool_calls or any(call.name != "read_file" or call.arguments.get("path") != "calculator.py"
                                           for call in first.tool_calls):
                raise ValueError("第一次请求没有按要求调用 read_file")
            observations = [registry.execute(call, context) for call in first.tool_calls]
            if any(item.status != "ok" for item in observations):
                raise ValueError("read_file 执行失败")
            record["read_file_status"] = [item.status for item in observations]
            messages.extend(provider.tool_results(first, observations))
            messages.append({"role": "user", "content": "请简短说明文件中的问题并结束，不要再调用工具。"})
            second = generate("final")
            if second.tool_calls or not second.content.strip():
                raise ValueError("第二次请求没有返回有效的最终文本")
            if config.think and not any(item["thinking_present"] for item in record["requests"]):
                raise ValueError("已请求 thinking，但两个响应均未观察到 thinking 字段")
        record["status"] = "SUCCEEDED"
        code = 0
    except KeyboardInterrupt:
        record["reason"] = "用户中断"
        code = 130
    except Exception as exc:
        # Provider messages are sanitized; never save the raw HTTP response.
        from evocode.models import LLMLocalError, LLMTimeoutError
        reason = str(exc) if isinstance(exc, (LLMLocalError, LLMTimeoutError, ValueError)) else "工具往返发生异常"
        record["reason"] = redactor.summary(reason, 1000)[0]
    record.update(runtime=provider.runtime, usage_totals=provider.usage_totals or None,
                  usage_observed_calls=provider.usage_observed_calls,
                  elapsed_seconds=round(time.monotonic() - start, 6))
    (output / "result.json").write_text(json.dumps(record, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return code, output


def main() -> None:
    parser = argparse.ArgumentParser(description="验证一次 Ollama 原生工具往返")
    parser.add_argument("--profile", required=True, choices=LOCAL_PROFILES)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, default=Path("runs/ollama/roundtrip"))
    args = parser.parse_args()
    try:
        code, path = run(args.profile, args.env_file, args.output_dir)
    except KeyboardInterrupt:
        parser.exit(130, "工具往返已中断\n")
    except Exception:
        parser.exit(2, "无法准备本地模型或输出目录\n")
    print("工具往返结果目录：{}".format(path.resolve()))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
