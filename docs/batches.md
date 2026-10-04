# 整批预算与部分结果

大量 URL 或多个关键词不必一直等到最慢一项结束。`web_search_multi`、`fetch_pages`、`deep_research` 支持 `time_budget_seconds`，默认 **120 秒**，可设为大于 0、不超过 600 的有限秒数。

这三个工具到截止时间后停止领取新任务、取消仍在进行的工作、等待其清理，再返回已完成结果。没有后台续跑或任务轮询服务；调用方可按返回的未完成项再次请求。

## 使用示例

```python
# 最多用 15 秒抓取这一批，保留先完成的正文。
fetch_pages(
    urls=["https://docs.python.org/3/library/asyncio.html",
          "https://docs.python.org/3/library/asyncio-task.html"],
    time_budget_seconds=15,
    response_format="json",
)

# 搜索、排队、正文下载共用 30 秒，而不是每个阶段重新计时。
deep_research(
    query="Python asyncio official documentation",
    include_domains=["docs.python.org"],
    strategy="merge",
    fetch_top_n=4,
    time_budget_seconds=30,
    max_total_chars=12000,
    response_format="json",
)
```

`web_search_multi(queries=["asyncio tasks", "asyncio cancellation"], time_budget_seconds=20, response_format="json")` 同样适用。直接 `web_search` / `search_chinese` 仍使用每个引擎的网络预算；单页阅读仍使用单页预算。

## 会保留哪些内容

- **批量正文**：每条输入仍有对应结果，并保持顺序；重复 URL 只工作一次，输出仍保留重复位置。成功正文和原有分页元数据保持不变。
- **多查询**：每个查询仍有独立状态、诊断和关联 URL。已返回的引擎结果先保存，因此合并模式中一边完成、另一边超时时也能返回已有链接。
- **调研**：搜索和抓取共用一个截止时间。若搜索已耗尽整批预算，已有来源仍会列出，计划抓取的页面标记为尚未开始；不会再给正文阶段追加一份时间。
- **正文字符预算**：`max_chars_each` 和 `max_total_chars` 继续约束已完成页面的返回内容。因字符预算而省略的正文可以通过 `next_index` 续读；因时间预算未获取的页面需重新抓取。

正常 HTTP 失败与超时不会覆盖其他已经完成的资料。意外的单项抓取异常也会转为该页错误，其他页面可继续。

## JSON 字段

三个工具新增 `batch`：

| 字段 | 含义 |
|---|---|
| `time_budget_seconds` | 本次整批预算 |
| `elapsed_seconds` | 从预算建立到生成结果元数据的耗时，含取消清理 |
| `deadline_exceeded` | 生成元数据时是否已经到达截止时间 |
| `complete` | 是否没有因整批预算遗留的工作；不代表所有请求成功 |
| `completed` | 已结束的工作项数，包括正常返回的失败与捕获的单项异常 |
| `timed_out` | 开始执行后被整批截止时间中止的工作项数 |
| `not_started` | 因时间耗尽而未开始执行的工作项数 |

计数单位：多查询按查询数；批量正文按去重后的 URL 数；调研按 **1 个搜索工作项 + 计划抓取的页面数**。它们不是底层 HTTP 请求次数；重试和跳转不增加这些计数。

正文超时项的示意结构：

```json
{
  "url": "https://example.org/slow",
  "ok": false,
  "title": "",
  "text": "",
  "error_code": "batch_deadline",
  "started": true,
  "error": "整批时间预算已用尽，抓取已中止"
}
```

`started=false` 表示该页尚未进入抓取工作。`started=true` 也可能正在等待共享并发名额或同 URL 的下载，不保证已经向网站发送请求。

`fetch_pages` 和 `deep_research` 的 `unfinished_urls` 列出因整批预算未完成的 URL，可直接用于后续 `fetch_pages`。普通 403、内容超限等已结束的失败不会加入该列表，应检查 `pages` 中的错误。

多查询中各条 `queries` 的 `deadline_exceeded` 和 `diagnostics` 标明哪些查询未完成。即使返回了部分链接、`ok=true`，搜索的 `complete` 仍可能为 false。调研顶层的 `complete` 继续表示搜索阶段完整性，`ok` 继续表示正文成功情况；检查 **`batch.complete` 和每页 `ok`** 才能判断整批工作是否完成及成功。

## 取消和资源边界

每批最多有 5 个工作任务领取查询或 URL，避免为大列表的每一项都创建独立任务。正文仍受进程内全局 5 路网络抓取上限约束。每个搜索工作可再调用自己的引擎，因此 5 个搜索工作不等于最多 5 个搜索 HTTP 请求；引擎请求仍共享限速。

超时返回前会等待本批任务完成取消清理，再关闭 HTTP 客户端。已完成正文可留在缓存，未完成正文不会缓存。同 URL 下载若由别的调用拥有，本批超时只取消自己的等待，不影响那个独立调用；如果本批拥有下载，其他等待者可用自己的客户端重试。

客户端主动取消会继续传播取消信号，不会伪装成一个正常的部分结果响应。整批截止时间属于协作式异步限制，取消清理、同步 HTML 提取和结果序列化可能使实际返回时间略超预算。同步解析目前不能强制中断；这不是 CPU 进程隔离或严格墙钟保证。

## 验证

本批本地 122 项测试通过。新增测试覆盖工作任务上限、结果顺序、重复 URL、单项异常、跨阶段截止时间、保留已完成引擎、取消清理、共享下载和正文字符预算。真实 MCP + 本地 HTTP 测试用 1 秒预算返回缓存中的已完成页面及慢页面错误，再单独抓取慢页面，确认可以恢复。真实 MCP 搜索测试用固定 HTTP 样本模拟一个正常引擎和一个停滞引擎，验证部分来源能穿过协议返回。

2026-10-04 的真实联网调研设置 20 秒整批预算和 1000 字符正文预算，搜索返回 3 条 Python 官方来源，成功抓取 2 页，约 3.8 秒完成，返回正文恰好 1000 字符。来源包括 [asyncio 文档](https://docs.python.org/3/library/asyncio.html)和[概念概述](https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html)。单次实测不代表所有网络环境的速度。

参考 [Firecrawl MCP](https://github.com/firecrawl/firecrawl-mcp-server) 对批量工作状态的区分；本项目采用当前调用内返回部分结果的方式，未接入后台任务服务。取消与任务回收依据 [Python asyncio 文档](https://docs.python.org/3.10/library/asyncio-task.html)，实现独立编写。
