# 搜索与来源筛选

搜索工具默认保留原来的文本输出和引擎切换方式。新增参数不需要额外依赖或搜索 API Key。更新代码后重新连接 MCP 服务，客户端才能发现新参数。

## 指定来源

只搜 Python 官方文档：

```python
web_search(
    query="Python asyncio official documentation",
    include_domains=["docs.python.org"],
    max_results=5,
    response_format="json",
)
```

从两个站点查资料，排除其中一个子域名：

```python
web_search(
    query="asyncio task cancellation",
    include_domains=["python.org", "github.com"],
    exclude_domains=["discuss.python.org"],
    strategy="merge",
)
```

- 填裸域名，不填协议、路径、端口或 `*`。允许大小写、末尾根域点和国际化域名，内部统一规范化。DNS 域名与 IPv4 地址可用于筛选；IPv6 字面量目前不能作为筛选参数。
- 包含条件匹配域名本身及其子域名；多个包含域名之间为“或”，排除优先。空列表表示没有对应限制。
- `example.org.evil.org`、`notexample.org` 不会匹配 `example.org`，包含用户信息或格式有歧义的链接也会被丢弃。
- 工具向引擎发送 `site:` / `-site:` 提示，再检查每条候选链接的真实主机名。手写在 query 中的 `site:` 不会自动变成服务端筛选条件。
- 有筛选时，每次尝试最多检查 `min(50, max(10, max_results * 3))` 条候选；引擎可能返回更少。筛选后数量可能不足，不自动翻页，也不会放宽条件填满结果。
- 这限制的是**搜索结果链接**。后续 `fetch_page` / `deep_research` 的 HTTP 跳转可能到其他域名，抓取结果中的 `final_url` 可用于核对；筛选不是网络访问隔离机制。

`search_chinese(query="异步编程", site="zhihu")` 自动应用 `zhihu.com` 筛选。其余站点映射保持不变，微信公众号匹配 `mp.weixin.qq.com`，CSDN 匹配 `blog.csdn.net`。

## 选择搜索策略

| 参数 | 行为 |
|---|---|
| `strategy="fallback"`（默认） | 中文先 Bing/zh-CN，再 DuckDuckGo；其他查询先 DuckDuckGo，再 Bing/en-US、Bing/zh-CN。遇到第一组通过筛选的结果就返回 |
| `strategy="merge"` | 并发查询 DuckDuckGo 和一个 Bing 语言市场，中文用 zh-CN、其他用 en-US。按语言优先顺序交替取结果并去重，一个引擎失败时仍保留另一个的结果 |

合并按引擎顺序交替取结果，保留每个引擎内部排名。没有语义评分，也不代表多引擎共同返回的内容已经被事实核实。合并模式会增加请求量，并等待两个引擎结束，因此保留为显式选项。

URL 去重统一主机名大小写、国际化域名、默认端口、空路径，并忽略 `#fragment`。保留 HTTP/HTTPS 差异、路径大小写、末尾斜杠和查询参数；不会随意删掉可能改变内容的参数。返回的 `url` 仍是首次保留的原始链接，`canonical_url` 用于识别重复页面。

## JSON 与诊断

`web_search`、`web_search_multi`、`search_chinese` 支持 `response_format="json"`。返回内容是 MCP 文本中的 JSON 字符串，需要调用方解析。

单查询结果包含：

| 字段 | 含义 |
|---|---|
| `results` | 标题、链接、摘要，以及 `canonical_url` 和 `provenance` |
| `provenance`（每条结果内） | 每个命中引擎的名称、原始候选序号 `rank`（从 1 开始）、原始 URL；不是合并后的排名 |
| `attempts` | 实际尝试的引擎；成功时有候选数、通过数、过滤数，失败时有原因 |
| `filtered_count` | 各引擎候选中因域名条件或无效 URL 被排除的条数；同一 URL 在两个引擎出现时可计两次 |
| `diagnostics` | 引擎错误；有结果时也可能非空 |
| `complete` | 本次实际尝试的搜索请求是否均无错误；默认策略未尝试的后备引擎不计入，也不表示搜遍全网 |
| `ok` | 有可用结果，或正常完成但无结果；部分引擎失败而仍有结果时为 true，需同时检查 `complete` |

`search_status` 的区别：

- `results`：有通过筛选的结果。
- `no_results`：所有尝试均正常返回无结果页。
- `filtered_empty`：有候选，但全部被筛掉，且没有引擎请求失败。
- `incomplete`：无可用结果，而且至少有一次请求失败或关键词为空。不能推断“没有资料”。

无效域名等参数错误返回 `{"ok": false, "error": "..."}`，不会发送搜索请求。

多查询 JSON 的顶层 `results` 只保留去重后的结果。每条结果的 `queries` 列出命中的查询，`provenance` 另带 `query`；顶层 `queries` 则逐条保存状态、诊断和 `result_urls`（规范化 URL）。这样重复结果不会被误报成“该查询没有结果”。顶层 `ok` / `complete` 分别是各查询对应字段的汇总。

`deep_research` 接受相同的 `strategy`、`include_domains`、`exclude_domains`，只抓取筛选后的前 N 条。JSON 增加上述搜索字段，原有正文与预算字段保持不变；其中 `ok` 继续表示所请求正文是否全部获取成功，`complete` 只描述搜索阶段。

## 本批验证

2026-10-04，本地 85 项离线测试通过，包含真实 MCP 子进程中的参数发现、域名校验、合并、JSON、多查询和调研流程。引擎 HTTP 响应在离线测试中使用固定样本；另用 `scripts/search_smoke.py` 通过真实 MCP 子进程访问引擎，固定 `max_results=5`、`include_domains=["docs.python.org"]`：

| 查询 | 策略 | 结果 | 当次观察 |
|---|---|---|---|
| Python asyncio official documentation | fallback | 5 条，均为指定域名 | DuckDuckGo 成功，约 1.70 秒 |
| Python asyncio official documentation | merge | 5 条，均为指定域名 | DuckDuckGo HTTP 202，Bing 成功，约 2.61 秒；`complete=false` |
| Python asyncio 官方文档 | fallback | 0 条，`incomplete` | Bing 的 10 条候选均被过滤，DuckDuckGo HTTP 202，约 3.73 秒 |
| Python asyncio 官方文档 | merge | 0 条，`incomplete` | Bing 的 10 条候选均被过滤，DuckDuckGo HTTP 202，约 3.59 秒 |

返回来源包括 [asyncio 文档](https://docs.python.org/3/library/asyncio.html)、[概念概述](https://docs.python.org/3/howto/a-conceptual-overview-of-asyncio.html)、[任务与协程](https://docs.python.org/3/library/asyncio-task.html)。这组小样本证明了本次来源筛选和错误保留的效果，不能证明合并模式普遍提高相关性或响应速度。中文检索的覆盖率仍需改进，额外后端留在后续路线中。

## 设计参考

- [Tavily Search 参数](https://docs.tavily.com/documentation/api-reference/endpoint/search)：参考其显式包含／排除域名的接口设计；本项目在收到公开引擎结果后独立校验，不调用 Tavily 服务。
- [SearXNG 结果管理](https://github.com/searxng/searxng/blob/master/searx/results.py)：参考合并时保留引擎来源的做法。本项目独立实现交替合并，没有移植其排序实现或接入 SearXNG 后端。
