# mcp-harries-resourcer

[![Tests](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml/badge.svg)](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**让查到的资料，变成下次找得到的笔记。**

Resourcer 是一个轻量 MCP 资料工具，把网页搜索、正文提取和本地 Markdown 收藏连接起来：**搜索网页 → 提取正文 → 由调用方整理 → 保存笔记 → 本地检索**。

不需要搜索 API Key。服务通过 stdio 运行；网页搜索和抓取仍会向搜索引擎及目标网站发送网络请求。支持 Claude Code，以及能启动 stdio MCP 服务的其他客户端。

[快速开始](#快速开始) · [使用演示](docs/usage-example.md) · [搜索与来源筛选](docs/search.md) · [分页与 JSON](docs/reading.md) · [整批预算](docs/batches.md) · [网络恢复与限制](docs/network.md) · [工具列表](#工具) · [配置](#限速与正文缓存) · [优化路线](docs/roadmap.md)

## 适合做什么

- **查资料**：围绕一个问题搜索多个关键词，中文查询优先 Bing，其他查询优先 DuckDuckGo；支持六个中文站点的快捷定向搜索。
- **读来源**：批量提取网页正文，保留链接和失败诊断，让调用方根据原文整理，而不是只读搜索摘要。
- **积累笔记**：把整理后的内容写进本地 Markdown 文件，再按关键词找回文件和具体行号。

内置跨调用限速、最多 5 路正文抓取和有容量上限的短时缓存。重复读取可以复用完整正文，保存同名资料不会覆盖旧文件。

网络请求支持有限重试、`Retry-After` 等待、总时间预算和响应大小限制。大页面、解压后超限的响应或持续缓慢的下载会明确报错，不会把部分正文当作完整资料保存到缓存。

多查询搜索、批量阅读和调研默认还有 **120 秒整批预算**。到时保留已完成的结果，标记未完成项，并取消本批剩余工作；双引擎搜索已拿到的来源也会保留。可通过 `time_budget_seconds` 调整，详见 [整批预算与部分结果](docs/batches.md)。

长文支持分页续读和正文版本校验；需要新内容时可单次刷新。单页、批量和调研工具支持 JSON 输出，调研资料包可限制正文总长度，按需读取剩余页面。

阅读技术文档时可用 `content_format="markdown"`，保留提取到的标题、代码块、表格和链接。JSON 还提供正文引用索引、结构统计和简易异常提示；两种正文格式共享同一次下载，使用各自的版本标识。示例与限制见 [Markdown 与引用](docs/content.md)。

正文在独立 Python 进程中解析，默认最多同时解析 2 页、每页解析预算 20 秒（含排队与启动）。超时或取消会终止并回收对应解析进程，复杂正文不会一直占住主事件循环。进程启动和内存开销、时间边界见 [解析隔离](docs/parsing.md)。

支持进度的 MCP 客户端可以接收搜索与读取通知：批量调用按已结束的查询／不同 URL 计数，调研先报告搜索阶段，再报告网页读取进度。成功与失败分别说明，整批到期不会把剩余项补成完成。启用方式与计数规则见 [进度反馈](docs/progress.md)。

同一服务进程会复用空闲 HTTP 客户端，减少重复初始化，并在条件允许时复用连接。客户端在一次搜索／读取作用域内独占，归还时清空 Cookie；最多保留 5 个空闲客户端，60 秒未再次借出即关闭。短页测量和资源边界见 [HTTP 客户端复用](docs/clients.md)。

搜索支持包含／排除域名，并逐条校验返回链接。需要扩大来源覆盖时，可选择 `strategy="merge"` 同时查询两个引擎；去重后保留各引擎的原始链接和排名。搜索工具也支持 JSON 输出。

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
| `web_search` | 默认依次切换引擎，可选双引擎合并；包含／排除域名、来源排名和 JSON |
| `web_search_multi` | 并发搜索多个关键词、跨查询去重；支持域名筛选、整批时间预算及部分结果，JSON 保留关联查询 |
| `search_chinese` | 知乎、B站、微信公众号、简书、CSDN、雪球定向搜索，校验实际返回域名 |
| `fetch_page` | 纯文本或 Markdown 正文、引用索引、分页续读、版本校验、单次刷新；支持 JSON 元数据 |
| `fetch_pages` | 批量正文与刷新；共享最多 5 个抓取名额和限速，到整批时限后返回已完成正文与未完成 URL |
| `deep_research` | 搜索和前 N 条正文抓取共用整批时限，支持双引擎合并和总正文预算；支持 JSON，不自动总结或保存 |
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
| `RESOURCER_CACHE_MAX_BYTES` | `8388608` | 缓存 URL、标题、两种格式正文及引用索引的 UTF-8 内容预算，默认 8 MiB；不含 Python 对象开销 |
| `RESOURCER_HTTP_RETRIES` | `1` | 可重试 HTTP 状态或网络故障最多追加尝试次数，范围 0–3 |
| `RESOURCER_REQUEST_TIMEOUT_SECONDS` | `60` | 每个引擎尝试／单页抓取的异步时间预算，含重试等待；范围 1–300 秒 |
| `RESOURCER_RESPONSE_MAX_BYTES` | `8388608` | 单个响应的传输数据及解压后数据上限，默认各 8 MiB；范围 1024–67108864 字节 |
| `RESOURCER_PARSE_CONCURRENCY` | `2` | 同时运行的正文解析进程数，范围 1–5 |
| `RESOURCER_PARSE_TIMEOUT_SECONDS` | `20` | 每页解析预算，包含解析排队、进程启动与结果传输，范围 1–120 秒 |

速率值设为 `0` 可关闭对应限速；任一缓存值设为 `0` 可关闭跨调用缓存。非法值会在 stderr 记录警告并使用默认值。设置修改后需要重新连接服务。

- 搜索结果不缓存，正文按完整 URL 匹配；不同查询参数视为不同页面。
- 缓存保存完整的纯文本、Markdown 正文和引用索引；每次调用再按 `max_chars` 截取，因此先读短摘要不会影响后续读全文。一次下载会提取两种格式，增加解析工作和单页缓存占用，但切换格式无需重复下载。
- 失败页面和超过单个缓存总容量的正文不缓存；页面过期后重新抓取。
- 同时抓取相同 URL 时共享进行中的下载，即使关闭缓存也能减少同一时刻的重复请求。
- 命中缓存时输出标明“正文来自进程内缓存”和年龄；单次刷新可用 `refresh=true`，需要每次重新抓取时设置 `RESOURCER_CACHE_TTL_SECONDS=0`。
- 缓存只存在内存，服务退出即清空。初次请求、重试和每次 HTTP 跳转都会经过限速；每次尝试最多跟随 5 次跳转。限速不会保证目标站点不再限流。
- `429/502/503/504` 或部分连接、读取故障默认最多重试一次。遵守 `Retry-After` 的秒数或日期；等待超过剩余预算时不提前重试，同一来源的后续调用也会受等待期约束。
- 时间和响应大小限制不能设为 0。网络预算与错误码见 [网络恢复与限制](docs/network.md)；三个批量工具还支持默认 120 秒的 [整批预算](docs/batches.md)，包含分批排队。

## 如何理解搜索结果

| 返回信息 | 含义与下一步 |
|---|---|
| 搜索结果 + 搜索诊断 | 前面的引擎失败，后备引擎找到了结果；可以继续使用返回的来源 |
| 未找到相关结果 | 所有尝试的引擎均返回了可识别的无结果页面；可换关键词 |
| 本次候选中没有符合筛选条件的结果 | 有候选链接，但全部被域名或 URL 校验排除；不表示目标站点没有相关资料 |
| 搜索未完成 | 存在限流、网络故障、验证码或未知页面；不能据此判断没有资料 |
| 限流（HTTP 429） | 降低频率，稍后重试；服务会先尝试后备引擎 |
| 解析失败 | 未识别结果页结构，可能是引擎改版或拦截；保留诊断以便排查 |
| 抓取失败 / 无法提取正文 | 页面可能要求登录、依赖 JavaScript、限制访问，或不是可提取的 HTML |
| 响应体超过上限 | 下载或解压后的数据太大；不会返回、解析或缓存部分正文 |
| 请求总时间预算已用尽 / 站点仍在限流等待期 | 本次等待已达到预算，或网站要求更长等待；稍后再试 |
| 整批时间预算已用尽 | 已完成资料仍会返回；JSON 的 `unfinished_urls` 可用于稍后重新抓取，查询状态中会说明搜索是否完整 |

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

可选的真实搜索检查：`python scripts/search_smoke.py --output <结果文件.json>`。它通过真实 MCP 子进程比较固定中英文查询的两种策略，会访问搜索引擎；输出链接、耗时、来源、筛选数量和失败原因。没有结果时域名检查标记为 `null`，不能据此声称搜索质量达标。实测记录见 [搜索说明](docs/search.md#本批验证)。

### 项目结构

```text
mcp-harries-resourcer/
├── server.py              # 8 个 MCP 工具、搜索解析与资料保存
├── request_policy.py      # 请求限速、缓存和重复下载复用
├── client_pool.py         # 独占 HTTP 客户端复用、Cookie 清理和空闲回收
├── http_policy.py         # 有界下载、重试、跳转和站点等待期
├── batch_budget.py        # 整批截止时间、有限工作任务和部分结果
├── task_cleanup.py        # 超时与重复取消后的子任务回收
├── progress.py            # 请求内进度、通知合并与发送限时
├── search_results.py      # 域名筛选、URL 去重与来源合并
├── page_content.py        # Markdown、代码保留、引用索引和内容提示
├── parse_policy.py        # 解析并发、超时、取消与子进程回收
├── parse_worker.py        # 单页正文提取的独立进程入口
├── requirements.txt       # Python 依赖
├── docs/                  # 使用示例、项目对比与验证记录
├── tests/                 # 回归测试与固定 HTML 样本
├── scripts/               # 可选的真实搜索验证
└── .github/workflows/     # Windows / Linux 自动检查
```

## 反馈与贡献

遇到引擎页面改版、安装问题或抓取失败，可提交 [Issue](https://github.com/fisHarly0/mcp-harries-resourcer/issues)。请附操作系统、Python 版本、复现步骤和错误诊断；示例中去掉个人笔记、凭据与私有地址。

欢迎提交修复或测试样本。修改搜索解析器时，请同步补充最小 HTML 样本，并运行测试。

## 边界与取舍

- 依赖公开搜索页面，免搜索 API Key，但可用性会受网络、引擎限流和页面改版影响。
- Bing 若把搜索请求重定向到首页，会补试一次 `www.bing.com/search`；仍失败时切换后备引擎并报告诊断。
- 手写 `site:` 只作为引擎提示；需要严格筛选时使用 `include_domains` / `exclude_domains` 或 `search_chinese`。筛选只检查搜索结果链接，不约束后续正文抓取的 HTTP 跳转。
- 合并模式交替取两个引擎的结果，会增加请求量；没有语义重排，不保证相关性高于默认模式。筛选后的结果可能少于请求数量。
- 正文提取不运行浏览器，不支持登录后的内容、验证码或复杂 JavaScript 页面。
- `deep_research` 是资料收集工具；模型总结、事实核对和保存需要调用方继续执行。
- 服务使用 stdio，不监听 HTTP 端口；这不等于离线运行。只在受信任的本地客户端中使用，因为工具能读取指定目录并保存文件。
- 请遵守目标站点的访问规则与服务条款，控制频率，尊重原文版权。

## 类似项目

见 [开源项目对比](docs/alternatives.md)：比较 DuckDuckGo MCP Server、SearXNG MCP、Tavily MCP 和 Firecrawl MCP 的依赖与适用场景。

## License

[MIT](LICENSE)
