# mcp-harries-resourcer

[![Tests](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml/badge.svg)](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**让查到的资料，变成下次找得到的笔记。**

Resourcer 是面向中文开发者的轻量 MCP 资料助手，把网上技术资料整理成带来源、可再次检索的本地 Markdown 笔记：**搜索网页 → 提取正文 → 由调用方整理 → 保存笔记 → 本地检索**。

不需要搜索 API Key。服务通过 stdio 运行；网页搜索和抓取仍会向搜索引擎及目标网站发送网络请求。支持 Claude Code，以及能启动 stdio MCP 服务的其他客户端。

[为什么选择它](#为什么选择它) · [快速开始](#快速开始) · [常用调用](#常用调用) · [工具列表](#工具) · [本地检索](docs/local-search.md) · [配置](#限速与正文缓存) · [版本记录](CHANGELOG.md)

当前版本 **0.4.0**：新增本地多关键词查询 `all`／`any`，可以跨同一文件的标题与正文找回资料，并查看各词的命中位置。默认保持整段文字匹配，已有调用无需修改。

## 为什么选择它

适合经常查技术文档、希望保留整理结果，并在后续开发中再次使用这些资料的人。

- **一次查阅，留下可复用的笔记。** 同一个 MCP 服务连接搜索、正文读取、笔记保存和本地检索；笔记保留来源、集合与标签，找回时可查看关键词的原文位置。
- **资料使用普通 Markdown 文件。** 可以直接打开、编辑、迁移或纳入自己的 Git 仓库。默认运行无需数据库、向量服务或搜索 API Key；已有资料目录也可按支持的文本格式搜索。
- **照顾中文技术资料的细节。** 提供中文站点搜索入口，处理页面声明的 GBK 等编码，保留提取正文中的代码、列表和表格；中文词与代码标点可按字面找回。

项目的价值集中在这套工作流程的组合。搜索、正文提取、缓存和分页在同类工具中已很常见；当前没有证据证明 Resourcer 的搜索相关性或速度领先。选择工具时可结合 [同类项目对比](docs/alternatives.md) 与下方的 [效果与限制](#效果与限制)。

## 适合做什么

| 需求 | 已有能力 | 详细说明 |
|---|---|---|
| 搜索网页资料 | Bing／DuckDuckGo 自动切换或合并，多查询去重、中文站点搜索、包含／排除域名、来源排名 | [搜索与筛选](docs/search.md) |
| 阅读技术文档 | 纯文本或 Markdown，代码、嵌套列表、表格、链接与引用索引；处理页面声明的中文编码 | [正文结构](docs/content.md) · [编码](docs/encoding.md) |
| 续读长文、批量调研 | 分页与版本校验、单次刷新、批量抓取、正文总量限制、JSON 输出 | [分页与 JSON](docs/reading.md) |
| 保存和找回笔记 | Markdown 收藏、元数据筛选、逐行或逐文件结果、字段优先排序、全部／任一关键词及逐词上下文 | [本地检索](docs/local-search.md) |
| 处理慢请求和部分失败 | 限速、有限重试、缓存与连接复用；整批到期保留已完成结果，支持取消和进度通知 | [网络限制](docs/network.md) · [整批预算](docs/batches.md) · [进度](docs/progress.md) |
| 接入自己的搜索服务 | 可选 SearXNG JSON 后端，保留上游引擎与部分失败诊断 | [SearXNG 接入](docs/searxng.md) |

内置搜索不需要额外配置：中文查询优先 Bing，其他查询优先 DuckDuckGo。SearXNG 需要自行提供实例地址并启用 JSON 接口；当前已验证接入协议与工具流程，尚未验证真实实例的检索效果。

默认最多并发抓取 5 个网页，多查询／批量阅读／调研的整批预算为 120 秒。本地搜索默认单文件上限 2 MiB、扫描预算 10 秒，提前排除依赖与构建目录。预算耗尽或出现读取错误会明确标记不完整，空结果不能一概理解为资料不存在。

正文与内置搜索 HTML 在可终止子进程中解析；同一服务进程复用短时正文缓存和空闲 HTTP 客户端。资源开销与限制见 [解析隔离](docs/parsing.md) 和 [客户端复用](docs/clients.md)。

```mermaid
flowchart LR
    Q[问题] --> S[网页搜索 / 中文站点搜索]
    S --> F[正文提取与来源索引]
    F --> A[调用方阅读、核对与总结]
    A --> M[save_finding 保存 Markdown]
    M --> L[search_local 检索已有资料]
```

## 快速开始

需要 Python 3.10+、Git，以及一个支持 stdio MCP 的客户端。已经安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 时，可以直接从 Git 来源启动：

```text
uvx --from git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main mcp-harries-resourcer
```

初次使用先在末尾加 `--version` 完成下载与安装，再配置客户端。MCP 的 `command` 为 `uvx`，`args` 为 `["--from", "git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main", "mcp-harries-resourcer"]`。固定提交、更新、缓存路径和完整配置见 [安装说明](docs/installation.md)，版本记录见 [CHANGELOG](CHANGELOG.md)。当前提供 Git 安装；没有执行 PyPI 发布。

通用 MCP 客户端配置：

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

使用 Git 来源安装后，更新并检查版本：

```text
uvx --refresh --from git+https://github.com/fisHarly0/mcp-harries-resourcer.git@main mcp-harries-resourcer --version
```

完成后重新连接 MCP。需要固定代码时，将 `main` 替换为完整提交 SHA。

需要修改源码或沿用原配置时，可按下面步骤克隆和启动；不需要激活虚拟环境。

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

## 常用调用

下面是交给 MCP 客户端的工具名与参数示例。

### 在指定来源内搜索

```python
web_search(
    query="Python asyncio cancellation",
    include_domains=["docs.python.org"],
    strategy="merge",
    response_format="json",
)
```

域名条件会逐条检查结果链接；合并模式保留两个引擎的来源信息，但不保证相关性更高。

### 按 Markdown 阅读网页

```python
fetch_page(
    url="https://docs.python.org/3/library/asyncio-task.html",
    content_format="markdown",
    max_chars=8000,
    response_format="json",
)
```

查看返回的完整性、`next_index` 和正文版本信息，再按需续读。代码、列表、表格来自提取后的正文，复杂页面仍需对照原网页。

### 用几个关键词找回笔记

```python
search_local(
    query="asyncio 取消",
    root="F:/research",
    result_mode="files",
    query_mode="all",
    max_results=5,
    response_format="json",
)
```

把 `root` 换成实际保存目录。例如“asyncio”在标题、“取消”在正文，也能匹配同一份笔记；每个词附命中上下文和位置。

| 参数 | 选择方式 |
|---|---|
| `query_mode="literal"`（默认） | 匹配整段连续文字，保留空格与代码标点 |
| `query_mode="all"` | 命中全部词，词序不限 |
| `query_mode="any"` | 命中任一词 |
| `result_mode="files"` | 每文件一条；允许跨字段、跨行匹配，并按字段层级排序 |
| `result_mode="lines"`（默认） | 按命中行返回；`all` 要求全部词出现在同一行 |

多词模式按空白拆分，最多 16 个不同词、4096 字符；不自动分词，也不把引号、减号或通配符解释为查询语法。`all` 的文件排序要求所有词共同满足对应层级，单个标题词不会把整个查询提升为标题匹配。结果的 `scan_complete` 与 `results_truncated` 分别表示扫描是否完整、展示是否截断。

只按笔记信息浏览时，可将 `query` 留空并设置集合、标签或来源筛选，例如 `search_local(query="", root="F:/research", tags="python,asyncio", response_format="json")`。

## 工具

| 工具 | 用途 |
|---|---|
| `web_search` | 默认依次切换引擎，可选双引擎合并；包含／排除域名、来源排名和 JSON |
| `web_search_multi` | 并发搜索多个关键词、跨查询去重；支持域名筛选、整批时间预算及部分结果，JSON 保留关联查询 |
| `search_chinese` | 知乎、B站、微信公众号、简书、CSDN、雪球定向搜索，校验实际返回域名 |
| `fetch_page` | 纯文本或 Markdown 正文、引用索引、分页续读、版本校验、单次刷新；支持 JSON 元数据 |
| `fetch_pages` | 批量正文与刷新；共享最多 5 个抓取名额和限速，到整批时限后返回已完成正文与未完成 URL |
| `deep_research` | 搜索和前 N 条正文抓取共用整批时限，支持双引擎合并和总正文预算；支持 JSON，不自动总结或保存 |
| `search_local` | 本地文本搜索，支持字面／全部词／任一词匹配、文件排序及元数据筛选；返回命中依据和完整性诊断 |
| `save_finding` | 保存带元数据的 Markdown；同秒同标题自动添加序号，保留已有文件 |

## 保存目录

默认写入 `~/research/<collection>/`。可在启动 MCP 进程时传入 `RESOURCER_RESEARCH_ROOT` 更改根目录。

Windows 示例：把下面的目录改成你的资料目录，再重新添加或更新客户端中的服务配置：

```powershell
$repoPath = (Get-Location).Path
claude mcp add --scope user resourcer -e RESOURCER_RESEARCH_ROOT=F:\research -- "$repoPath\.venv\Scripts\python.exe" "$repoPath\server.py"
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
| `RESOURCER_PARSE_CONCURRENCY` | `2` | 搜索 HTML 与正文共用的解析进程数，范围 1–5 |
| `RESOURCER_PARSE_TIMEOUT_SECONDS` | `20` | 每次 HTML 解析预算，包含解析排队、进程启动与结果传输，范围 1–120 秒 |

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
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS / Linux 先用 `.venv/bin/python -m pip install -r requirements-dev.txt` 安装开发依赖，再执行 `.venv/bin/python -m unittest discover -s tests -v`。其中独立 Markdown 渲染器只用于验证输出结构，不属于服务运行依赖。

测试使用固定 HTML 样本、模拟 HTTP 响应和可控时钟，覆盖 URL 解码、去重、无结果、验证码、限流、超时、引擎切换、正文失败、共享限速、缓存过期与淘汰、请求取消、保存和本地检索；MCP 测试还会启动真实 stdio 子进程。测试不会向真实搜索引擎发请求，固定样本不保证引擎未来页面保持不变。GitHub Actions 配置了 Windows / Linux 与 Python 3.10 / 3.12 / 3.14 的六组检查，每组均包含测试和独立安装验证；实际结果以对应提交的 Actions 为准。

分页、刷新、版本变化和 JSON 参数还会通过本地 HTTP 页面与真实 MCP 子进程验证。当前持续优化方向与各批次验收条件见 [优化路线](docs/roadmap.md)。

0.4.0 功能提交 [`851423d`](https://github.com/fisHarly0/mcp-harries-resourcer/commit/851423db611dc0272d684afc5a747d13a0619913) 的验证：本地 Windows／Python 3.12.12 共 308 项测试，307 项通过、1 项因符号链接权限跳过；[远端六组 CI](https://github.com/fisHarly0/mcp-harries-resourcer/actions/runs/37203732419) 全部通过。每组还独立构建源码包与 wheel，在全新虚拟环境中通过命令入口和模块入口验证真实 MCP，包括多关键词检索。这些结果不等于真实搜索引擎的可用性或相关性保证。

可选的真实搜索检查：`python scripts/search_smoke.py --output <结果文件.json>`。默认比较 6 个中英文案例的两种策略，分别检查域名和已知专题页命中，保留原始结果、耗时和失败原因。空结果的域名检查为 `null`，官方首页也不会算成专题页命中；支持选案例、重复运行和中断后保留记录。参数和实测见 [搜索质量验证](docs/search-quality.md)。

### 项目结构

```text
mcp-harries-resourcer/
├── pyproject.toml           # 包元数据、构建后端与命令入口
├── server.py                # 旧版绝对路径启动兼容入口
├── mcp_harries_resourcer/   # 安装后的 Python 包
│   ├── __main__.py          # CLI、--help、--version 与 stdio 启动
│   ├── server.py            # 8 个 MCP 工具、搜索调度与资料保存
│   ├── request_policy.py    # 请求限速、缓存和重复下载复用
│   ├── client_pool.py       # HTTP 客户端复用与 Cookie 清理
│   ├── http_policy.py       # 有界下载、重试和站点等待期
│   ├── batch_budget.py      # 整批时限、部分结果
│   ├── task_cleanup.py      # 超时与重复取消后的清理
│   ├── progress.py          # 请求内进度和通知合并
│   ├── search_content.py    # Bing／DuckDuckGo HTML 解析
│   ├── search_results.py    # 域名筛选、URL 去重与来源合并
│   ├── local_results.py     # 命中上下文与有界文件排序
│   ├── local_search.py      # 本地检索、筛选和扫描限制
│   ├── local_terms.py       # 有界多关键词匹配与逐词命中依据
│   ├── html_encoding.py     # 网页编码声明、解码与诊断
│   ├── page_content.py      # Markdown、代码保留与引用
│   ├── parse_policy.py      # 搜索与正文解析进程管理
│   └── parse_worker.py      # 单页解析进程入口
├── requirements.txt        # 源码与安装包共用的依赖范围
├── CHANGELOG.md            # 版本记录
├── docs/                   # 使用说明、项目对比与验证记录
├── tests/                  # 回归测试与固定 HTML 样本
├── scripts/                # 安装验证、真实搜索检查与性能样本
└── .github/workflows/      # Windows / Linux 回归及隔离安装验证
```

## 反馈与贡献

遇到引擎页面改版、安装问题或抓取失败，可提交 [Issue](https://github.com/fisHarly0/mcp-harries-resourcer/issues)。请附操作系统、Python 版本、复现步骤和错误诊断；示例中去掉个人笔记、凭据与私有地址。

欢迎提交修复或测试样本。修改搜索解析器时，请同步补充最小 HTML 样本，并运行测试。

## 效果与限制

**真实搜索质量仍需改进。** 2026-10-04 的早期基线实验使用 6 个中英文查询、两种策略，共 12 次调用，其中 2 次在前 5 条结果中命中预先指定的参考页面；受引擎限制等因素影响，部分查询未完成。这是特定环境下的小样本记录，不是整体准确率或当前 0.4.0 的重新评测。后续功能与协议测试尚未证明该指标改善，详见 [查询、环境和原始结果](docs/search-quality.md)。

SearXNG 已完成可选接入及协议验证，真实实例的可用性与检索效果尚未测量。本地资料库仍采用逐文件扫描，没有全文索引或语义检索；大量文件的搜索效率需要另行评估。

后续优先用真实调研任务验证两个问题：能否更快找到有用来源，以及保存后的笔记能否更容易被找回和复用。测试通过说明已覆盖的行为符合预期，不能代替这些使用效果的验证。

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
