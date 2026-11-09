# EvoCode

面向代码修复的软件工程 Agent 实验项目。

## 当前进度

正在进行工程初始化。

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
```

当前入口支持显示帮助信息。`uv run` 会在项目虚拟环境中执行命令，无需手动激活环境。

`.venv` 是本地生成的环境目录，不提交到 Git；`.python-version` 和 `uv.lock` 应随项目提交，便于在其他电脑上重建环境。
