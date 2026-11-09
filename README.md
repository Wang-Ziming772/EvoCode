# EvoCode

面向代码修复的软件工程 Agent 实验项目。

## 当前进度

已支持从任务文件加载本地代码目录，在临时文件工作区中执行 pytest、
按白名单编辑 UTF-8 文件并导出 Git 补丁。

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

## 验证

在仓库根目录执行：

```powershell
uv run --locked --no-python-downloads pytest -q
```

测试覆盖 CLI、独立副本、样例修复、补丁应用检查、路径与编辑限制、缓存排除、
链接拒绝以及正常和异常退出时的清理。
