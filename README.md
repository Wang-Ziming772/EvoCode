# EvoCode

面向代码修复的软件工程 Agent 实验项目。

## 当前进度

已支持从任务文件加载本地代码目录，在临时文件工作区中执行 pytest、
按白名单编辑 UTF-8 文件、导出 Git 补丁，并通过统一工具接口调用这些能力。
模型接入层支持两个网关配置档案及一次手动验证的工具往返；最小控制器已支持
带预算限制的工具调用和独立最终复验。

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

默认注册的七个工具如下。所有参数都在调用前按其 JSON Schema 检查，
不接受表格之外的参数。

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `list_files` | `{}` | 列出可访问的工作区文件 |
| `read_file` | `{"path": "..."}` | 读取 UTF-8 文本 |
| `search_code` | `{"query": "..."}` | 搜索字面量，返回路径、行号和片段 |
| `write_file` | `{"path": "...", "content": "..."}` | 修改白名单内的已有文本文件 |
| `run_tests` | `{}` | 使用上下文中的测试执行器运行测试 |
| `git_diff` | `{}` | 查看相对基线的补丁 |
| `git_status` | `{}` | 查看固定格式的 Git 工作区状态 |

`list_files` 和 `search_code` 不遍历 `.git` 及上文列出的生成目录，也不返回文件接口
无法访问的路径；搜索区分大小写，跳过非 UTF-8 文件及包含 NUL 的文件。搜索支持跨行
字面量，行号和片段对应匹配的起始行。文件接口的路径、链接和编辑白名单限制仍然有效。
Git 工具只执行固定操作；`git_diff` 与 `Workspace.export_patch()` 一样会刷新副本暂存区。

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
不会运行模型修改后的代码，也不会调用真实模型。项目尚未提供 Docker 或其他进程隔离；
实际执行不可信修改时，调用方必须提供适当隔离的测试执行器。

## 验证

在仓库根目录执行：

```powershell
uv run --locked --no-python-downloads python -m pytest -q
```

测试覆盖 CLI、独立副本、样例修复、补丁应用检查、路径与编辑限制、缓存排除、
链接拒绝及清理，也覆盖工具注册、七工具调用、模型配置、两种协议的模拟请求、
工具往返、错误和截断，以及控制器的离线轨迹、预算与复验。自动测试不访问真实模型 API。
