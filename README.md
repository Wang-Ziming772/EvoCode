# EvoCode

面向代码修复的软件工程 Agent 实验项目。

## 当前进度

已支持从任务文件加载本地代码目录，在临时文件工作区中执行 pytest、
按白名单编辑 UTF-8 文件、导出 Git 补丁，并通过统一工具接口调用这些能力。
模型接入层支持两个网关配置档案及一次手动验证的工具往返；最小控制器已支持
带预算限制的工具调用和独立最终复验，并可显式注入 Docker 测试执行器。
`repair` 命令将这些能力连接起来，并保存结果、补丁和运行轨迹。

## 第一个功能目标

输入一个简单的 Python 修复任务，在隔离工作区中读取代码、
生成补丁、执行测试，并保存运行结果和轨迹。

## 环境要求

- Python 3.12：当前开发与运行版本，使用电脑上已安装的解释器。
- uv：管理项目虚拟环境、依赖和运行命令。
- Git：获取代码和管理版本。

项目通过 `.python-version` 指定 Python 3.12，通过 `uv.lock` 记录解析后的依赖版本。

## 安装与运行

以下命令在仓库根目录执行，适用于 Windows PowerShell。

### 同步项目环境

确认已安装 Python 3.12 和 uv，然后执行：

```powershell
uv sync --locked --python 3.12 --no-python-downloads
```

该命令根据锁文件创建或同步 `.venv` 虚拟环境，并安装当前项目。`--locked` 要求锁文件与项目配置一致；`--no-python-downloads` 禁止自动下载 Python，项目依赖仍会按需下载并安装到虚拟环境中。

### 检查 Python 版本与路径

```powershell
uv run --locked --no-python-downloads python --version
uv run --locked --no-python-downloads python -c "import sys; print(sys.executable)"
```

版本应为 `Python 3.12.x`，解释器路径应位于项目的 `.venv\Scripts` 目录。

### 运行命令行入口

```powershell
uv run --locked --no-python-downloads evocode --help
uv run --locked --no-python-downloads evocode --task examples/tasks/smoke-001.json
```

任务文件中的 `repo` 相对于任务 JSON 所在目录解析。CLI 在临时副本中调用当前
Python 解释器和 pytest；输出与退出码来自测试结果。当前 smoke 样例有意保留加法错误，
因此会有 2 项失败、1 项通过，命令返回退出码 `1`。`uv run` 会在项目虚拟环境中执行
命令，无需手动激活环境。

`.venv` 是本地生成的环境目录，不提交到 Git；`.python-version` 和 `uv.lock` 应随项目提交，便于在其他电脑上重建环境。

### 完整修复命令

先启动 Docker Desktop 并切换到 Linux 容器模式，在仓库根目录构建测试镜像：

```powershell
docker build --pull=false -t evocode-pytest:py312 -f docker/pytest/Dockerfile docker/pytest
```

按下文的模型配置说明准备 `.env`，然后分别运行：

```powershell
uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile gpt --env-file .env
uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile deepseek --env-file .env
```

`repair` 使用所选档案原有的模型 ID、思考强度和输出上限；省略 `--profile` 时使用
`EVOCODE_DEFAULT_PROFILE`。`--env-file` 默认是当前目录的 `.env`。任务路径相对当前
目录，任务中的 `repo` 相对任务文件所在目录。调用模型前会检查运行目录、Docker Linux
引擎和本地镜像，并用受限容器确认镜像中的 pytest 与执行器可用。运行时不会自动拉取、
构建镜像或安装依赖，也不会在 Docker 失败时回退到宿主机测试。

其他选项：`--output-dir` 默认为 `runs`；`--max-steps`、`--max-tool-calls` 由所选档案
或命令行决定（云端回退为 10/30）；`--image` 默认为 `evocode-pytest:py312`；
`--test-timeout` 默认为 30 秒。两项次数上限和超时必须为正数。
退出码为 `0`（控制器最终复验通过且产物已保存）、`1`（控制器失败或额度耗尽）、
`2`（参数、配置、启动检查或产物保存错误）或 `130`（用户中断）。

每次运行在结果根目录下创建独立的 UUID 目录：

- `result.json`：状态、原因、模型配置白名单、调用次数、耗时和最终复验摘要。
- `patch.diff`：工作区相对基线的原始 Git 补丁；失败或额度耗尽时也保留已有修改。
- `trajectory.jsonl`：逐事件刷新的状态、模型可见文本与 usage 摘要、工具调用及复验结果。

摘要会限制长度并标记截断，缺失的 usage 为 `null`；不会记录 SDK 原始响应或推理字段。
若终态写入发生异常并恢复，轨迹可能追加修正的 `run_finished`；以最后一条为准。
已知 API 密钥和 Authorization 凭据会脱敏。为保持补丁原样，如果补丁包含已知密钥或
明确硬编码的 Authorization 值，命令会拒绝保存该补丁并以错误结束；常见动态认证
表达式可以保存。输出目录不能位于任务源目录内部。
补丁不会自动应用到源目录。输出仍包含任务和模型产生的其他内容，应按其敏感程度管理。

目前完成的是 MVP 工程闭环和一题真实任务的开发验证；尚未接入正式 benchmark 评分、
批量评测或训练。

## 单题真实任务：marshmallow-1359

这是 SWE-bench Lite 的 `dev` 条目 `marshmallow-code__marshmallow-1359`，对应公开
[Issue #1357](https://github.com/marshmallow-code/marshmallow/issues/1357)，基线为
`b40a0f4e33823e6d0f341f7e8684e359a99060d1`。本节只做单题开发验证，不使用
SWE-bench 官方评测器，也不提供官方 resolved 判断或 benchmark 成功率。

在 Windows PowerShell 中，从仓库根目录依次准备任务并构建镜像：

```powershell
uv run --locked --no-python-downloads python scripts/prepare_marshmallow_1359.py
docker build --pull=false -t evocode-marshmallow-1359:py37 -f benchmarks/marshmallow_1359/docker/Dockerfile .
$ImageId = (docker image inspect evocode-marshmallow-1359:py37 --format '{{.Id}}').Trim()
uv run --locked --no-python-downloads python scripts/verify_marshmallow_1359.py --image $ImageId --output-dir runs/marshmallow-1359/preflight
```

准备时只获取指定提交和 Issue 正文，不获取数据集参考补丁、测试补丁或提示。
`.evocode-data/marshmallow-1359/baseline` 是干净历史源码，`repo` 是加入固定复现测试的
任务源，`task.json` 的编辑白名单由 `src/marshmallow` 下已有 Python 文件统一生成。
再次准备时会核验已有目录；发现修改时停止，不清空或覆盖。数据目录和 `runs` 均由 Git
忽略。复现测试来源于 Issue 正文，测试和配置不在 Agent 可编辑白名单中。

镜像固定 Python 3.7.17、pytest 7.4.4 及测试依赖；依赖只在构建阶段安装。
pytest 插件检查并输出 `marshmallow.__file__`，要求其位于容器的
`/workspace/src/marshmallow/__init__.py`。预检分别运行未修改的原有测试与复现测试，
并记录退出码、导入路径、版本及输出。

预检通过后，使用现有 `.env` 中的 GPT 配置运行：

```powershell
uv run --locked --no-python-downloads evocode repair --task .evocode-data/marshmallow-1359/task.json --profile gpt --env-file .env --max-steps 20 --max-tool-calls 60 --test-timeout 120 --image $ImageId --output-dir runs/marshmallow-1359/agent
```

独立验证最近一次保存的补丁：

```powershell
$LatestRun = Get-ChildItem -LiteralPath runs/marshmallow-1359/agent -Directory | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
$PatchPath = Join-Path $LatestRun.FullName 'patch.diff'
uv run --locked --no-python-downloads python scripts/verify_marshmallow_1359.py --patch $PatchPath --image $ImageId --output-dir runs/marshmallow-1359/independent
```

验证器在新工作区先确认原有测试及两个未修复复现，再检查补丁可应用且只修改已有白名单
源码，应用磁盘上的补丁，分别运行原有测试、复现测试和完整测试，并核对测试材料未变化。
每次验证使用独立输出目录保存 JSON 结果和原始测试输出；若任一步不符合条件，退出码非零。
镜像 ID、导入路径和测试数量可在输出中核对。测试均在禁网容器中运行，不安装宿主 Python
或在宿主机执行生成的代码。

## 文件工作区

任务 JSON 可添加可选的 `editable_files`，例如：

```json
{
  "task_id": "smoke-001",
  "issue": "检查计算器",
  "repo": "../repos/add_bug",
  "editable_files": ["calculator.py"]
}
```

该字段必须是相对于工作区的文件路径字符串列表。缺省或为空列表时，
`write_file` 不允许编辑任何文件。路径不得是绝对路径、包含 `..`、
指向 `.git`，或使用不受支持的 Windows 路径名称。只允许编辑白名单中
明确列出的已有 UTF-8 文本文件；`read_file` 可以读取其他合法路径。

可以在 Python 中直接使用工作区：

```python
from pathlib import Path

from evocode.tasks.workspace import Workspace

source = Path("examples/repos/add_bug")
with Workspace(source, editable_files=["calculator.py"]) as workspace:
    print(workspace.path)             # 临时副本路径
    print(workspace.baseline_commit)  # 副本的 Git 基线提交
    content = workspace.read_file("calculator.py")
    workspace.write_file("calculator.py", content)  # 此例不改变内容
    patch = workspace.export_patch()  # 无变化时为空字符串
```

工作区复制源码时排除 `.git`、`.venv`、`__pycache__`、`.pytest_cache`、
`.mypy_cache`、`.ruff_cache`、`.tox`、`.nox`、`build`、`dist` 和 `*.egg-info` 等目录；
Git worktree 中作为文件存在的 `.git` 也会被排除。要复制的路径中若存在符号链接、
Windows 目录联接或其他重解析点，则拒绝创建工作区；排除目录的内部不会被遍历。
文件接口会拒绝访问路径上后来出现的链接或联接，导出补丁时也会检查纳入的目录。
临时目录不能位于源目录内部。

副本使用独立 Git 仓库和基线提交，不依赖用户 Git 身份或用户级 attributes，也不修改
全局配置；源码自身的 `.gitattributes` 仍会生效。
`export_patch()` 将副本相对于基线的新增、删除和修改导出为 Git unified diff，
包括原项目 `.gitignore` 忽略的普通文件，但排除上述生成目录；每次导出都会刷新
副本的 Git 暂存区。工作区退出时，无论正常结束还是发生异常，都会清理临时目录。
需要把补丁写成文件时，可以用以下方式保留原始换行和非 UTF-8 字节：

```python
Path("change.patch").write_bytes(patch.encode("utf-8", errors="surrogateescape"))
```

然后可在对应的干净源码目录执行 `git apply --check /path/to/change.patch`，检查补丁
能否应用，而不实际修改文件。

**这是文件工作区，不提供进程隔离或网络隔离。** `write_file` 的白名单只约束该接口；
在工作区内运行的测试或其他进程仍拥有当前用户的文件系统和网络权限，也可以绕过该接口。
路径检查也不防护其他进程并发替换文件造成的竞态。因此不要将此模块当作运行不可信代码
的安全沙箱。

## 统一工具接口

工具层不依赖特定模型 SDK。`ToolCall` 包含 `call_id`、`name` 和 `arguments`；
`Observation` 返回对应的 `call_id`、`status`、文本 `content` 和 JSON 元数据
`metadata`。在工作区上下文内使用：

```python
from pathlib import Path

from evocode.tasks.workspace import Workspace
from evocode.tools import ToolCall, ToolContext, create_default_registry

registry = create_default_registry()
print(registry.get_specs())  # 名称、描述和参数 JSON Schema
with Workspace(Path("examples/repos/add_bug"), ["calculator.py"]) as workspace:
    context = ToolContext(workspace)
    observation = registry.execute(
        ToolCall("call-1", "read_file", {"path": "calculator.py"}), context
    )
    print(observation.status, observation.content, observation.metadata)
```

默认注册的八个工具如下。所有参数都在调用前按其 JSON Schema 检查，
不接受表格之外的参数。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `list_files` | `{}` | 列出可访问的工作区文件 |
| `read_file` | `{"path": "...", "start_line": 1, "end_line": 100}` | 读取 UTF-8 文本；可选行号从 1 开始且两端包含 |
| `search_code` | `{"query": "..."}` | 搜索字面量，返回路径、行号和片段 |
| `write_file` | `{"path": "...", "content": "..."}` | 修改白名单内的已有文本文件 |
| `replace_text` | `{"path": "...", "old_text": "...", "new_text": "..."}` | 精确替换白名单文件中的唯一匹配 |
| `run_tests` | `{}` | 使用上下文中的测试执行器运行测试 |
| `git_diff` | `{}` | 查看相对基线的补丁 |
| `git_status` | `{}` | 查看固定格式的 Git 工作区状态 |

`list_files` 和 `search_code` 不遍历 `.git` 及上文列出的生成目录，也不返回文件接口
无法访问的路径；搜索区分大小写，跳过非 UTF-8 文件及包含 NUL 的文件。搜索支持跨行
字面量，行号和片段对应匹配的起始行。文件接口的路径、链接和编辑白名单限制仍然有效。
Git 工具只执行固定操作；`git_diff` 与 `Workspace.export_patch()` 一样会刷新副本暂存区。
`read_file` 不指定行号时保留整文件读取行为；指定行号时每次最多读取 200 行。
`replace_text` 拒绝空旧文本、零次或多次匹配（包括重叠匹配）及替换整个文件；其余
内容与原始换行保持不变，且仍须通过已有路径和编辑白名单检查。

状态 `ok` 表示工具完成，`error` 表示调用或执行异常，`timeout` 表示执行超时。
异常的 `metadata.error_code` 用于区分未知工具、参数错误、权限错误等。测试进程正常
结束时，即使 pytest 断言失败，`run_tests` 仍返回 `ok`，并以 `metadata.exit_code` 和
`metadata.test_outcome` 报告结果；pytest 的内部错误、用法错误和未收集到测试也有各自的
`test_outcome`。标准输出和标准错误均包含在 `content` 中。

`ToolContext(workspace, test_executor=executor)` 可替换测试执行器。执行器接收当前
工作区路径，返回包含整数 `returncode`、文本或空值 `stdout` 和 `stderr` 的对象
（例如 `subprocess.CompletedProcess`）；超时时应抛出 `subprocess.TimeoutExpired` 或
`TimeoutError`。默认执行器仍使用当前 Python 和 pytest。工作区关闭后不再调用执行器。

每条观察的 `content` 默认最多 16,000 个字符，截断时包含标记并设置
`metadata.truncated`；元数据也有限额。可通过 `create_default_registry(max_output_chars=...)`
调整内容上限。文件列表和搜索收集结果最多约 32,000 字符，长搜索片段也会单独标记。
截断后的 `git_diff` 仅供查看，完整补丁应调用 `Workspace.export_patch()`。
这些限制约束返回内容，并不构成进程、网络或内存隔离。

需要扩展工具时，可创建空的 `ToolRegistry`，用 `register(Tool(...))` 注册名称、描述、
参数 schema 和返回 `ToolOutput` 的处理函数。注册表支持 object、array、string、integer、
number、boolean 和 null，以及 properties、required、additionalProperties、items、enum
和常用长度或数值上下限；不支持的 schema 关键字会在注册时拒绝，避免静默跳过校验。

## 模型配置与手动验证

在项目根目录的 `.env` 中配置以下字段。已有 `.env` 时保留其中的凭据；新建文件可参照
`.env.example`，填写对应密钥和网关提供的模型 ID。`.env` 被 Git 忽略，不应提交。

```dotenv
EVOCODE_DEFAULT_PROFILE=gpt
EVOCODE_BASE_URL=https://console.gahorouter.com/v1
EVOCODE_GPT_API_KEY=
EVOCODE_GPT_MODEL_ID=
EVOCODE_GPT_MAX_OUTPUT_TOKENS=128000
EVOCODE_GPT_REASONING_EFFORT=medium
EVOCODE_DEEPSEEK_API_KEY=
EVOCODE_DEEPSEEK_MODEL_ID=
EVOCODE_DEEPSEEK_MAX_OUTPUT_TOKENS=393216
EVOCODE_DEEPSEEK_REASONING_EFFORT=high
EVOCODE_REQUEST_TIMEOUT_SECONDS=60
EVOCODE_MAX_RETRIES=0
```

系统环境变量优先于 `.env`；调用方可显式选择 `gpt` 或 `deepseek`，只校验所选档案。
GPT 的思考强度支持 `low`、`medium`、`high`、`xhigh`、`max`；DeepSeek 支持 `none`、
`low`、`high`、`max`。输出上限和超时必须为正数，重试次数必须是非负整数。配置不会
猜测模型、切换凭据或自动修改思考强度和输出上限。

`evocode.models.load_model_config(profile, env_file)` 可指定档案和配置文件；省略文件路径
时读取当前目录的 `.env`。`LLMProvider.generate(messages, registry.get_specs())` 返回
`LLMResponse`，其中 `tool_calls` 使用现有 `evocode.tools.ToolCall`，缺失的 `usage` 为
`None`。调用方执行工具后，可使用 `provider.tool_results(response, observations)` 获取
可追加到下一次 `messages` 的原始 assistant 协议项和匹配的工具结果。GPT 使用 Responses
API；DeepSeek 使用 Chat Completions。模型 ID 和网关地址按配置使用，协议与 LiteLLM
路由前缀由适配器处理。

手动验证以下命令，不属于默认 pytest 流程：

```powershell
uv run --locked --no-python-downloads python scripts/smoke_llm.py --profile gpt
uv run --locked --no-python-downloads python scripts/smoke_llm.py --profile deepseek
```

每次验证只在临时工作区开放 `read_file`，最多请求模型两次；其他工具、错误参数、
缺少工具调用或第二次继续请求工具都会使验证失败。为保证请求次数上限，脚本要求
`EVOCODE_MAX_RETRIES=0`。常规 `LLMProvider` 按配置的次数重试暂时性失败；脚本不会
更改该配置或回退到其他模型。网关若不支持相应协议或参数，会返回错误而非自动降级。

## Ollama 本地档案

先启动已安装的 Ollama，并确保模型已在本机存在；程序不会下载模型。本地档案无需 API key，
请求不继承代理环境变量。`.env.example` 中三个新增档案的预算如下（K 按 1024 计算）：

| 档案 | 模型 ID | 上下文 | 单次输出上限 |
| --- | --- | ---: | ---: |
| `qwen2b_q8` | `qwen3.5:2b-q8_0` | 131072 | 8192 |
| `qwen2b_bf16` | `qwen3.5:2b-bf16` | 131072 | 8192 |
| `qwen27b` | `batiai/qwen3.8-27b:q2` | 24576 | 4096 |

保留的 `qwen4b`、`qwen9b` 分别使用 `qwen3.5:4b-q8_0`、`qwen3.5:9b-q4_K_M`，
其配置不随新增档案变化。输出上限是一次生成的总上限，包含思考；不是全任务累计 token 预算。

每个本地档案可设置自己的 `EVOCODE_<档案名大写>_BASE_URL`。地址优先级为档案专属地址、
`EVOCODE_OLLAMA_BASE_URL`、`http://127.0.0.1:11434`；同名系统环境变量优先于 `.env`。
显式错误地址不会触发端口回退。示例中三个新档案使用 `http://127.0.0.1:11436`。

本地 `repair` 和工具往返验证启动时，会检查各档案配置的本机 Ollama 服务，
卸载其他驻留模型，并等待 `/api/ps` 确认释放后再开始推理。
`EVOCODE_OLLAMA_UNLOAD_BASE_URLS` 可用逗号分隔填写额外的本机地址，
例如 `http://127.0.0.1:11435`。仍有模型未释放时，本次启动会报告对应服务。
修复任务的 `trajectory.jsonl` 用 `ollama_unload` 事件记录已卸载的模型。

如果需要为已有 G 盘模型单独启动该服务，可在 PowerShell 窗口中执行并保持窗口打开：

```powershell
$env:OLLAMA_HOST = '127.0.0.1:11436'; $env:OLLAMA_MODELS = 'G:\Ollama\models'; $env:OLLAMA_NOPRUNE = '1'; ollama serve
```

若端口已被占用，请确认服务实际使用的模型目录；程序不会自动下载或切换到其他服务。

分别验证一次只读工具往返，然后通过正式 CLI 运行 `smoke-001`：

```powershell
uv run --locked --no-python-downloads python scripts/smoke_ollama.py --profile qwen4b --env-file .env
uv run --locked --no-python-downloads python scripts/smoke_ollama.py --profile qwen9b --env-file .env
uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile qwen4b --env-file .env --output-dir runs/ollama/smoke-qwen4b
uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile qwen9b --env-file .env --output-dir runs/ollama/smoke-qwen9b
```

4B、9B 的示例配置为 `think=true`、`num_ctx=131072`、`num_predict=81920`、请求超时 3600 秒；
三个新档案的请求超时为 600 秒。实际运行以所选档案的 `.env` 和系统环境变量为准。
模型请求和工具调用预算分别为 200 和 600。`NUM_CTX` 控制上下文容量，
`MAX_OUTPUT_TOKENS` 控制一次生成的输出预留，`MAX_STEPS` 限制模型请求轮次，
`MAX_TOOL_CALLS` 限制工具调用次数（含最终复验），`MAX_RETRIES` 控制网络或暂时性 HTTP 错误的重试。
命令行显式 `--max-steps` / `--max-tool-calls` 优先于本地档案配置，例如 20/60；
若本地档案未配置相应预算，则回退到原有的 10/30。云端档案不使用本地预算，
其未指定时仍为 10/30。没有额外的全任务 token 硬上限。

本地响应正常结束（`done=true`、`done_reason=stop`）却只有思考内容或空正文，且没有
工具调用时，控制器会保留当前工作区与工具历史，最多连续补问两次；每次补问计入
`MAX_STEPS`。收到有效响应后连续补问计数归零。恢复不会更改 `.env` 中的思考开关、
采样参数或 token 预算，也不会把思考内容当作最终答案；成功仍须控制器重新执行测试。
输出截断、上下文不足、协议错误不适用这项恢复。`trajectory.jsonl` 中的 `model_error`
记录结束原因、思考/正文字数和工具调用数量，`model_retry` 记录补问次数，不记录思考正文。

同一用户轮次中，Provider 仅暂存最近一次有效 assistant 工具调用的 Ollama 原生 thinking，
并在后续请求中回传给对应的 assistant 消息；更早的工具调用与结果仍保留，但不携带
thinking。工具结果不会开启新用户轮次，空响应不会覆盖最近一次有效调用的缓存。
新的 `user` 消息（包括空响应补问和控制器复验失败反馈）会结束此前的用户轮次：旧工具调用
及结果保留，但不再回传旧轮次的 thinking。新任务、历史重置或完成后清除相应缓存。
这些思考内容不写入终端、运行产物或异常文本；内部关联标识不会发送给 Ollama。

可分别将 `EVOCODE_QWEN4B_NUM_CTX` 或 `EVOCODE_QWEN9B_NUM_CTX` 设为 `262144`
以请求 256K 上下文，输出上限仍须小于上下文。上下文增大会增加内存和显存占用，
可能导致更多计算落到 CPU。Ollama 按渲染后的输入计算实际容量，`MAX_OUTPUT_TOKENS`
作为单次生成上限发送。请求禁用服务端截断和上下文移位。收到明确的输入超限错误时，
运行会报告服务端给出的输入 token 数和可用上下文（如有）。服务端报告实际加载的窗口
小于配置值时，运行会结束。

轨迹中的 `input_estimate_bytes` 记录 UTF-8 序列化字节数及协议余量，仅用于诊断。
实际回传的最近一轮 thinking 也计入估算。`prompt_eval_count` 则是服务端报告的实际输入
token 数。

固定采样参数为 `temperature=0.6`、`top_p=0.95`、`top_k=20`、`min_p=0.0`、
`presence_penalty=0.0`、`repeat_penalty=1.0`。HTTP 超时分别约束连接、读写及连接池等待；
非流式响应在生成期间等待读取，3600 秒不是整个任务的总时限。Ctrl+C 可中断等待并尽力
保留当前产物。`result.json` 记录配置、服务版本、可获得的模型 digest、实际加载上下文及
各原生计数和耗时的累计值（`*_duration` 单位为纳秒）；缺失计数不补零，
`observed_calls` 标识每个字段实际出现的响应次数。轨迹仅记录是否存在 thinking，不保存其正文。
本地宽裕预算与此前云模型 20/60 设置不同，不能直接据此比较性能。

三个新增档案可分别运行只读工具往返、`smoke-001`，以及在准备好任务和镜像后手动运行
marshmallow 单题。以下命令从项目根目录执行，不会自动启动服务或下载模型：

```powershell
uv run --locked --no-python-downloads python scripts/smoke_ollama.py --profile qwen2b_q8 --env-file .env --output-dir runs/ollama/qwen2b_q8/roundtrip
uv run --locked --no-python-downloads python scripts/smoke_ollama.py --profile qwen2b_bf16 --env-file .env --output-dir runs/ollama/qwen2b_bf16/roundtrip
uv run --locked --no-python-downloads python scripts/smoke_ollama.py --profile qwen27b --env-file .env --output-dir runs/ollama/qwen27b/roundtrip

uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile qwen2b_q8 --env-file .env --image evocode-pytest:py312 --test-timeout 120 --output-dir runs/ollama/qwen2b_q8/smoke
uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile qwen2b_bf16 --env-file .env --image evocode-pytest:py312 --test-timeout 120 --output-dir runs/ollama/qwen2b_bf16/smoke
uv run --locked --no-python-downloads evocode repair --task examples/tasks/smoke-001.json --profile qwen27b --env-file .env --image evocode-pytest:py312 --test-timeout 120 --output-dir runs/ollama/qwen27b/smoke

uv run --locked --no-python-downloads evocode repair --task .evocode-data/marshmallow-1359/task.json --profile qwen2b_q8 --env-file .env --image evocode-marshmallow-1359:py37 --test-timeout 120 --output-dir runs/ollama/qwen2b_q8/marshmallow
uv run --locked --no-python-downloads evocode repair --task .evocode-data/marshmallow-1359/task.json --profile qwen2b_bf16 --env-file .env --image evocode-marshmallow-1359:py37 --test-timeout 120 --output-dir runs/ollama/qwen2b_bf16/marshmallow
uv run --locked --no-python-downloads evocode repair --task .evocode-data/marshmallow-1359/task.json --profile qwen27b --env-file .env --image evocode-marshmallow-1359:py37 --test-timeout 120 --output-dir runs/ollama/qwen27b/marshmallow
```

准备好上文的 marshmallow 任务和镜像后，可由用户手动运行：

```powershell
uv run --locked --no-python-downloads evocode repair --task .evocode-data/marshmallow-1359/task.json --profile qwen4b --env-file .env --image evocode-marshmallow-1359:py37 --test-timeout 120 --output-dir runs/ollama/marshmallow-qwen4b
uv run --locked --no-python-downloads evocode repair --task .evocode-data/marshmallow-1359/task.json --profile qwen9b --env-file .env --image evocode-marshmallow-1359:py37 --test-timeout 120 --output-dir runs/ollama/marshmallow-qwen9b
```

## 最小修复控制器

`AgentController` 把模型 Provider、工具注册表和已有工作区连接起来。调用方建立并关闭
工作区，同时必须显式提供测试执行器；控制器不会选择宿主机默认测试执行器。例如：

```python
from evocode.agent import AgentController
from evocode.tasks.workspace import Workspace
from evocode.tools import create_default_registry

# provider、source_dir、issue、editable_files 和 test_executor 由调用方提供。
controller = AgentController(
    provider=provider, registry=create_default_registry(), max_steps=10, max_tool_calls=30
)
with Workspace(source_dir, editable_files=editable_files) as workspace:
    result = controller.run(issue=issue, workspace=workspace, test_executor=test_executor)
    print(result.status, result.reason)
    print(result.model_requests, result.tool_calls, result.last_verification)
    print(result.final_explanation, result.patch, result.events)
```

测试执行器接收当前工作区路径，返回带 `returncode`、`stdout` 和 `stderr` 的对象，
与上文 `ToolContext` 的执行器约定相同。模型自行决定工具及顺序；每次调用通过注册表
执行，调用结果由 Provider 转换为相应协议的后续消息。模型以非空文本申请完成后，
控制器会再次调用 `run_tests`：只有观察状态为 `ok`、退出码为 `0` 且测试结果为
`passed` 才能成功。未通过的复验作为普通消息反馈，不伪造模型工具调用。

`max_steps` 限制模型请求次数，`max_tool_calls` 限制工具调用次数，包括错误调用和
控制器主动复验；多调用批次超额时整个批次不执行。结果状态包括 `SUCCEEDED`、
`FAILED` 和 `EXHAUSTED`，中间状态变化可在内存事件中查看。`RunResult` 包含结束原因、
最后一次完成说明、计数、最后一次复验、工作区补丁和事件。事件只记录状态、调用及
观察摘要，不记录文件内容、工具参数、凭据或模型原始推理内容。

新增控制器验收使用脚本式假模型与模拟测试执行器；执行器只读取当前副本的文件内容，
不会运行模型修改后的代码，也不会调用真实模型。实际测试时，调用方仍须显式传入
适当隔离的执行器，例如下面的 `DockerTestExecutor`。

## Docker 隔离测试执行器

启动 Docker Desktop 并切换到 Linux 容器模式，然后在仓库根目录构建本地测试镜像：

```powershell
docker build --pull=false -t evocode-pytest:py312 -f docker/pytest/Dockerfile docker/pytest
```

镜像基于固定的 Python 3.12 基础镜像 digest，并固定 pytest 及其依赖版本；构建可能需要
下载基础镜像和 Python 包。运行测试时不会自动拉取镜像或联网安装依赖。其他项目所需的
依赖需预先构建到可信的兼容镜像中；该镜像必须提供 `/usr/local/bin/python` 和 pytest，
以及镜像中的 `/opt/evocode/pytest_runner.py`，且不能声明额外卷。更新执行器后需重新
构建本地测试镜像。

执行器可以直接注入已有工具层或控制器，不改变 CLI 的默认行为：

```python
from evocode.sandbox import DockerTestExecutor
from evocode.tools import ToolCall, ToolContext, create_default_registry

executor = DockerTestExecutor(timeout_seconds=30)
# workspace 是调用方已进入的 Workspace。
context = ToolContext(workspace=workspace, test_executor=executor)
observation = create_default_registry().execute(ToolCall("test-1", "run_tests", {}), context)

# 对 AgentController 同样显式传入：
# result = controller.run(issue=issue, workspace=workspace, test_executor=executor)
```

每次调用都从当前工作区创建独立快照，并将快照以只读方式挂载进一次性容器；原工作区
不被挂载或修改。快照排除 `.git`、上述生成目录、`.env` 和 `.env.*`（保留
`.env.example`），拒绝符号链接、目录联接及特殊文件。其他文件会被复制，调用方应确认
工作区中没有不应交给测试代码的秘密。容器不继承宿主机环境变量或 Docker socket。

容器使用非 root 用户、禁用网络、只读根文件系统、删除 Linux capabilities、启用默认
seccomp 和 `no-new-privileges`，只提供受限的 `/tmp`。默认限制为 30 秒、512 MiB 内存、
1 CPU、64 个进程、64 MiB 临时空间；快照文件上限为 128 MiB，最多扫描 10,000 个条目
（包括排除的条目）。标准输出和
标准错误分别最多保留 65,536 字节，截断会明确标记，并通过 `Observation.metadata.truncated`
传递给工具调用方。运行命令固定为容器内 pytest，不开放任意命令。

执行器仅在 pytest 正常生成有效的 JUnit XML 报告、至少有一个测试通过且报告中无失败
或错误时，才接受退出码 0；报告最大为 4 MiB。缺失、无效或所有测试都跳过的报告不会
判为成功。报告校验防止意外提前退出，并不能防止测试代码故意伪造报告或篡改 pytest。

pytest 的非零退出码作为正常测试结果返回；超时抛出 `subprocess.TimeoutExpired`，
Docker 镜像、引擎、容器启动或资源错误抛出 `DockerExecutionError`。执行器会清理本次
创建的容器；若无法确认清理成功，则抛出 `DockerCleanupError` 而不是报告测试成功。
镜像、超时和资源上限可通过 `DockerTestExecutor` 的构造参数调整。超时涵盖快照和容器
执行，清理使用独立的时间额度，因此整体耗时可能超过测试超时。

如果 Docker 引擎在创建容器时失联，创建请求可能在清理检查之后才完成；引擎恢复后可用
`docker ps -a --filter label=io.evocode.test-run` 检查是否存在残留。`/workspace` 只读且
`/tmp` 不允许执行文件，依赖写入源码目录或执行临时程序的测试需要调整。

这是受限的 Docker 执行环境，不是抵御恶意代码的完整安全边界。Docker 共享内核，
也不能防护宿主机并发替换文件造成的竞态；处理不可信仓库时应使用独立虚拟机或更强隔离，
并使用可信 Docker 引擎和镜像。参见 [Docker Engine 安全说明](https://docs.docker.com/engine/security/)。

## 验证

在仓库根目录执行：

```powershell
uv run --locked --no-python-downloads python -m pytest -q
```

测试覆盖 CLI、独立副本、样例修复、补丁应用检查、路径与编辑限制、缓存排除、
链接拒绝及清理，也覆盖工具注册、七工具调用、模型配置、两种协议的模拟请求、
工具往返、错误和截断，以及控制器的离线轨迹、预算与复验。自动测试不访问真实模型 API。
Docker 集成测试会在本地 Linux 引擎与 `evocode-pytest:py312` 镜像可用时运行，否则跳过；
它们不拉取镜像或访问真实模型 API。
