# 安装、启动和升级

项目提供标准 Python 包、wheel 和源码包，Python 最低版本为 3.10；CI 验证 Windows／Linux 上的 3.10 和 3.12。命令为 `mcp-harries-resourcer`，Python 包名为 `mcp_harries_resourcer`。本批提供 Git 来源安装，没有执行 PyPI 发布。

## 使用 uvx 启动

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Git，然后运行：

```text
uvx --from git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main mcp-harries-resourcer
```

这会由 uv 准备隔离环境并启动 stdio MCP 服务；正常启动会等待客户端消息，没有交互式终端界面。检查安装可在末尾加 `--version` 或 `--help`。初次下载、构建和依赖安装需要网络与额外时间；建议先运行一次 `--version`，再接入 MCP 客户端，避免首次准备超过客户端的启动时限。

其他客户端的配置示例：

```json
{
  "mcpServers": {
    "resourcer": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main",
        "mcp-harries-resourcer"
      ]
    }
  }
}
```

如果图形客户端找不到 `uvx`，使用其可执行文件的绝对路径。`uvx` 等价于 `uv tool run`，具体来源、Python 选择和隔离规则见 [uv 官方工具文档](https://docs.astral.sh/uv/guides/tools/)。需要指定现有解释器时，在 `--from` 前增加 `--python <解释器绝对路径>`。

Claude Code 可用：

```text
claude mcp add --scope user resourcer -- uvx --from git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main mcp-harries-resourcer
```

已有同名服务时先检查其配置，不要重复添加。原来的环境变量仍然有效，包括 `RESOURCER_RESEARCH_ROOT`；切换启动方式时保留现有设置，避免笔记改存到默认目录。

## 固定版本与更新

`@main` 会随仓库推进。需要固定代码时，把 `main` 替换为经过验证的完整提交 SHA。包的 `--version` 显示版本号，不能代替具体 Git 提交；相同版本号可能出现在尚未升版的不同开发提交中。版本变化见 [CHANGELOG](../CHANGELOG.md)。

需要重新检查 Git 来源和依赖缓存时：

```text
uvx --refresh --from git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main mcp-harries-resourcer --version
```

随后重新连接 MCP。固定 SHA 的配置只有更换 SHA 才会切换代码；已运行的服务不会自行重载。依赖声明采用兼容范围，没有锁定全部传递依赖，所以固定源码 SHA 不等于整个环境逐字节可复现。

## 使用已有 Python 虚拟环境

也可以用虚拟环境中的 Python 安装 Git 来源：

```text
python -m pip install "git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main"
python -m mcp_harries_resourcer --version
```

MCP 配置使用该虚拟环境 Python 的绝对路径，`args` 为 `["-m", "mcp_harries_resourcer"]`；也可以直接用虚拟环境内 `mcp-harries-resourcer` 命令的绝对路径。Windows 命令位于 `Scripts`，macOS／Linux 位于 `bin`。更新使用同一解释器执行 `pip install --upgrade` 和相同 Git URL，再重新连接。

## 从源码开发与旧配置兼容

原先克隆仓库、安装 `requirements.txt`、执行根目录 `server.py` 的配置继续可用，不需要迁移已有笔记。开发安装可用 `python -m pip install -e .`；随后 `python -m mcp_harries_resourcer` 或命令入口均可启动。

生产模块移入 `mcp_harries_resourcer/`。直接用 Python 导入内部模块的脚本需要改成例如 `from mcp_harries_resourcer import server`；这些内部模块不承诺稳定的库 API。MCP 的 8 个工具名称和参数保持不变。

## 验证安装产物

```text
python -m pip install build
python scripts/verify_distribution.py
```

脚本在临时副本构建源码包，再从源码包构建 wheel，创建不继承系统包的虚拟环境并安装 wheel。它检查依赖、导入位置和版本一致性，然后从与仓库无关的目录，通过命令入口和 `python -m` 启动真实 MCP：列出 8 个工具、读取本地 HTTP 页面、运行解析子进程、验证 Markdown 代码与表格、保存笔记并按标签搜索。还会在工作目录放置同名干扰模块，检查运行时没有误导入它们。

已有 uv 时，可加 `--uv <uv可执行文件绝对路径>`，再验证从临时源码目录执行 `uv tool run --from`。这项本地验证不代表远程 Git 已推送；远程来源需在对应提交推送后单独验证。

Windows 可通过 `TEMP`、`TMP` 指定临时盘，`PIP_CACHE_DIR`、`UV_CACHE_DIR` 指定下载与工具缓存；uv 需要下载 Python 时，可用 `UV_PYTHON_INSTALL_DIR` 指定存放位置。验证脚本不清理全局缓存，也不触碰已有用户笔记。

包配置遵循 [Python 打包指南](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)，依赖声明从 `requirements.txt` 读取，源码与安装包共用一份范围。命令入口形式参考 [DuckDuckGo MCP Server](https://github.com/nickclyde/duckduckgo-mcp-server/blob/main/pyproject.toml) 的工具分发方式；本项目独立实现安装与验证流程。

本批本地验证：Windows / Python 3.12.12，原有 193 项测试中 192 项通过、1 项因符号链接权限不足跳过。源码包 → wheel → 全新虚拟环境、命令入口、模块入口、uv 临时来源启动和旧版脚本入口均已完成上述真实 MCP 流程。缓存与验证环境位于 F 盘。四组跨平台结果以 [Actions](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml) 中对应提交为准。
