# mcp-harries-resourcer

[![Tests](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml/badge.svg)](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**让查到的资料，变成下次找得到的笔记。**

Resourcer 是一个轻量 MCP 资料工具，把网页搜索、正文提取和本地 Markdown 收藏连接起来：**搜索网页 → 提取正文 → 由调用方整理 → 保存笔记 → 本地检索**。

不需要搜索 API Key。服务通过 stdio 运行；网页搜索和抓取仍会向搜索引擎及目标网站发送网络请求。支持 Claude Code，以及能启动 stdio MCP 服务的其他客户端。

[快速开始](#快速开始) · [使用演示](docs/usage-example.md) · [分页与 JSON](docs/reading.md) · [工具列表](#工具) · [配置](#限速与正文缓存) · [优化路线](docs/roadmap.md)

## 适合做什么

- **查资料**：围绕一个问题搜索多个关键词，中文查询优先 Bing，其他查询优先 DuckDuckGo；支持六个中文站点的快捷定向搜索。
- **读来源**：批量提取网页正文，保留链接和失败诊断，让调用方根据原文整理，而不是只读搜索摘要。
- **积累笔记**：把整理后的内容写进本地 Markdown 文件，再按关键词找回文件和具体行号。

内置跨调用限速、最多 5 路正文抓取和有容量上限的短时缓存。重复读取可以复用完整正文，保存同名资料不会覆盖旧文件。

长文支持分页续读和正文版本校验；需要新内容时可单次刷新。单页、批量和调研工具支持 JSON 输出，调研资料包可限制正文总长度，按需读取剩余页面。

```mermaid
flowchart LR
    Q[问题] --> S[网页搜索 / 中文站点搜索]
    S --> F[正文提取与来源索引]
    F --> A[调用方阅读、核对与总结]
    A --> M[save_finding 保存 Markdown]
    M --> L[search_local 检索已有资料]
```

## 快速开始

需要 Python 3.10+、Git，以及一个支持 stdio MCP 的客户端。以下命令在准备放置项目的目录运行；不需要激活虚拟环境。

### Windows PowerShell

```powershell
git clone https://github.com/fisHarly0/mcp-harries-resourcer.git
cd mcp-harries-resourcer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Claude Code：使用绝对路径，避免客户端切换目录后找不到文件
$repoPath = (Get-Location).Path
claude mcp add --scope user resourcer -- "$repoPath\.venv\Scripts\python.exe" "$repoPath\server.py"
claude mcp list
```

### macOS / Linux

```bash
git clone https://github.com/fisHarly0/mcp-harries-resourcer.git
cd mcp-harries-resourcer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
claude mcp add --scope user resourcer -- "$PWD/.venv/bin/python" "$PWD/server.py"
claude mcp list
```

已经安装过时，进入原仓库更新代码，再用虚拟环境中的 Python 更新依赖。若已有同名 `resourcer` 配置，先检查原路径；无需再次克隆或重复添加。代码更新后重新连接 MCP 服务，已运行的进程不会自动重载。

其他客户端：将服务的 `command` 设为虚拟环境 Python 的**绝对路径**，`args` 设为 `["server.py 的绝对路径"]`，传输方式选 stdio。不要只填写系统的 `python`，否则可能使用未安装依赖的解释器。

## 第一次使用

连接后发送：

> 搜索 Python asyncio 的官方文档，抓取前两条结果，列出来源和抓取失败项。根据正文整理简短笔记，然后调用 save_finding 保存到 python-demo，最后用 search_local 查找刚保存的笔记。

调用流程：

1. `deep_research(query="Python asyncio official documentation", num_results=5, fetch_top_n=2)` 返回来源索引和正文。
2. 客户端模型核对来源、整理笔记。工具本身不调用模型，也不会自动生成结论。
3. `save_finding(collection="python-demo", title="asyncio 入门", content="整理后的 Markdown", source_url="https://docs.python.org/3/library/asyncio.html", tags="python,asyncio")` 写入文件。
4. `search_local(query="asyncio", root="保存结果中显示的 collection 绝对路径", include_ext="md")` 返回命中文件和行号。

完整参数示例、预期输出和保存文件示例见 [使用演示](docs/usage-example.md)。

## 工具

| 工具 | 用途 |
|---|---|
| `web_search` | 中文优先 Bing，其他查询优先 DuckDuckGo；无结果或失败时切换引擎，失败附诊断 |
| `web_search_multi` | 并发搜索多个关键词，按查询分组，并跨查询去重 |
| `search_chinese` | 知乎、B站、微信公众号、简书、CSDN、雪球的 `site:` 定向搜索 |
| `fetch_page` | 单页正文、分页续读、版本校验、单次刷新；支持 JSON 元数据 |
| `fetch_pages` | 批量正文与刷新；共享最多 5 个网络抓取名额和限速，返回每页状态与续读位置 |
| `deep_research` | 搜索并抓取前 N 条正文，按总正文预算返回资料包；支持 JSON，不自动总结或保存 |
| `search_local` | 在指定本地目录搜索文本，返回文件路径和行号 |
| `save_finding` | 保存带元数据的 Markdown；同秒同标题自动添加序号，保留已有文件 |

## 保存目录

默认写入 `~/research/<collection>/`。可在启动 MCP 进程时传入 `RESOURCER_RESEARCH_ROOT` 更改根目录。

Windows 示例：把下面的目录改成你的资料目录，再重新添加或更新客户端中的服务配置：

```powershell
$repoPath = (Get-Location).Path
claude mcp add --scope user resourcer -e RESOURCER_RESEARCH_ROOT=D:\research -- "$repoPath\.venv\Scripts\python.exe" "$repoPath\server.py"
```

环境变量需要传给 MCP **子进程**。在另一个终端临时设置环境变量，不会改变已经运行的客户端或服务。

## 限速与正文缓存

限速和缓存由同一个 MCP 服务进程内的所有调用共享，包括单页抓取、批量抓取和 `deep_research`。搜索引擎各自排队，正文抓取使用独立队列；多个客户端各自启动的进程不会共享这些状态。

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `RESOURCER_SEARCH_RPM` | `30` | 每个搜索引擎主动请求的速率，默认相邻请求至少间隔 2 秒 |
| `RESOURCER_FETCH_RPM` | `60` | 所有正文主动抓取的总速率，默认相邻请求至少间隔 1 秒 |
| `RESOURCER_CACHE_TTL_SECONDS` | `300` | 成功正文缓存 5 分钟；命中不延长有效期 |
| `RESOURCER_CACHE_MAX_ENTRIES` | `64` | 最多缓存 64 个 URL，容量不足时淘汰最久未使用项 |
| `RESOURCER_CACHE_MAX_BYTES` | `8388608` | 缓存 URL、标题、正文的 UTF-8 内容预算，默认 8 MiB；不含 Python 对象开销 |

速率值设为 `0` 可关闭对应限速；任一缓存值设为 `0` 可关闭跨调用缓存。非法值会在 stderr 记录警告并使用默认值。设置修改后需要重新连接服务。

- 搜索结果不缓存，正文按完整 URL 匹配；不同查询参数视为不同页面。
- 缓存保存完整提取正文；每次调用再按 `max_chars` 截取，因此先读短摘要不会影响后续读全文。
- 失败页面和超过单个缓存总容量的正文不缓存；页面过期后重新抓取。
- 同时抓取相同 URL 时共享进行中的下载，即使关闭缓存也能减少同一时刻的重复请求。
- 命中缓存时输出标明“正文来自进程内缓存”和年龄；单次刷新可用 `refresh=true`，需要每次重新抓取时设置 `RESOURCER_CACHE_TTL_SECONDS=0`。
- 缓存只存在内存，服务退出即清空。限速针对主动发起的请求，HTTP 自动跳转属于同一次请求链；不会保证目标站点不再限流。

## 如何理解搜索结果

| 返回信息 | 含义与下一步 |
|---|---|
| 搜索结果 + 搜索诊断 | 前面的引擎失败，后备引擎找到了结果；可以继续使用返回的来源 |
| 未找到相关结果 | 所有尝试的引擎均返回了可识别的无结果页面；可换关键词 |
| 搜索未完成 | 存在限流、网络故障、验证码或未知页面；不能据此判断没有资料 |
| 限流（HTTP 429） | 降低频率，稍后重试；服务会先尝试后备引擎 |
| 解析失败 | 未识别结果页结构，可能是引擎改版或拦截；保留诊断以便排查 |
| 抓取失败 / 无法提取正文 | 页面可能要求登录、依赖 JavaScript、限制访问，或不是可提取的 HTML |

若客户端无法连接，先用配置中的 Python 执行 `-m pip show mcp httpx trafilatura beautifulsoup4`，核对解释器和依赖；再检查 `server.py` 的绝对路径。手动运行 `server.py` 后等待输入是正常现象，它不是交互式终端程序。

如果出现 `No module named mcp.server.fastmcp`，请用服务对应的 Python 重新安装 `requirements.txt`。当前实现使用 SDK v1 的 `FastMCP`，依赖已限制为 `mcp<2`，避免安装不兼容的 SDK 2.x。

## 验证与开发

```powershell
# 使用安装依赖的同一个 Python
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS / Linux 使用 `.venv/bin/python -m unittest discover -s tests -v`。

测试使用固定 HTML 样本、模拟 HTTP 响应和可控时钟，覆盖 URL 解码、去重、无结果、验证码、限流、超时、引擎切换、正文失败、共享限速、缓存过期与淘汰、请求取消、保存和本地检索；MCP 测试还会启动真实 stdio 子进程。测试不会向真实搜索引擎发请求，固定样本不保证引擎未来页面保持不变。GitHub Actions 配置了 Windows / Linux 与 Python 3.10 / 3.12 的检查。

分页、刷新、版本变化和 JSON 参数还会通过本地 HTTP 页面与真实 MCP 子进程验证。当前持续优化方向与各批次验收条件见 [优化路线](docs/roadmap.md)。

### 项目结构

```text
mcp-harries-resourcer/
├── server.py              # 8 个 MCP 工具、搜索解析与资料保存
├── request_policy.py      # 请求限速、缓存和重复下载复用
├── requirements.txt       # Python 依赖
├── docs/                  # 使用示例、项目对比与验证记录
├── tests/                 # 回归测试与固定 HTML 样本
└── .github/workflows/     # Windows / Linux 自动检查
```

## 反馈与贡献

遇到引擎页面改版、安装问题或抓取失败，可提交 [Issue](https://github.com/fisHarly0/mcp-harries-resourcer/issues)。请附操作系统、Python 版本、复现步骤和错误诊断；示例中去掉个人笔记、凭据与私有地址。

欢迎提交修复或测试样本。修改搜索解析器时，请同步补充最小 HTML 样本，并运行测试。

## 边界与取舍

- 依赖公开搜索页面，免搜索 API Key，但可用性会受网络、引擎限流和页面改版影响。
- Bing 若把搜索请求重定向到首页，会补试一次 `www.bing.com/search`；仍失败时切换后备引擎并报告诊断。
- `site:` 的最终行为由搜索引擎决定，不能保证每条结果都符合预期。
- 正文提取不运行浏览器，不支持登录后的内容、验证码或复杂 JavaScript 页面。
- `deep_research` 是资料收集工具；模型总结、事实核对和保存需要调用方继续执行。
- 服务使用 stdio，不监听 HTTP 端口；这不等于离线运行。只在受信任的本地客户端中使用，因为工具能读取指定目录并保存文件。
- 请遵守目标站点的访问规则与服务条款，控制频率，尊重原文版权。

## 类似项目

见 [开源项目对比](docs/alternatives.md)：比较 DuckDuckGo MCP Server、SearXNG MCP、Tavily MCP 和 Firecrawl MCP 的依赖与适用场景。

## License

[MIT](LICENSE)
