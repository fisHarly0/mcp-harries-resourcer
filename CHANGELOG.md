# 版本记录

## 0.1.1 — 2026-10-04

- 显式声明 `lxml_html_clean` 运行依赖，修复 Python 3.10 的全新虚拟环境中，旧版 pip 未安装正文提取器所需清理模块、导致 MCP 初始化失败的问题。`pip check` 无法发现这类缺失，因此保留实际启动和正文读取检查。

## 0.1.0 — 2026-10-04

首次提供标准 Python 包和命令入口。本版本包含此前逐批完成的 8 个 MCP 工具：网页／多查询／中文搜索、单页／批量读取、调研资料收集、本地检索和 Markdown 保存。

- 增加 `mcp-harries-resourcer` 和 `python -m mcp_harries_resourcer` 启动方式，支持 `--help`、`--version`。
- 保留仓库根目录的 `python server.py` 启动兼容；MCP 工具名称、参数和笔记格式不变。
- 模块移入 `mcp_harries_resourcer` 包，网页解析子进程支持从安装位置启动，不依赖当前工作目录。
- 提供 wheel／源码包构建、隔离安装和真实 MCP 验证；支持通过 Git 来源使用 `uvx --from`。

版本号用于标识代码包，不表示已发布到 PyPI。此前实现过程和后续方向见 [优化路线](docs/roadmap.md)。
