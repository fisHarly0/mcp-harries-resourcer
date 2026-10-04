# 分页阅读、刷新与 JSON 输出

## 读取长网页

调用 `fetch_page` 时设置 `max_chars=3000`，并使用 `response_format="json"`。返回内容中的 `text` 是这一段正文，`next_index` 是下一段起点。

下一次传入相同 `url`，将 `start_index` 设为上次的 `next_index`，将 `expected_content_id` 设为上次的 `content_id`。重复操作，直到 `next_index` 为 `null`。正文偏移使用 Python 字符串的 Unicode 字符索引，不是 UTF-8 字节偏移。

```text
第一次：fetch_page(url, max_chars=3000, response_format="json")
第二次：fetch_page(url, max_chars=3000, start_index=上次.next_index,
                  expected_content_id=上次.content_id, response_format="json")
```

上面是调用顺序示意；客户端应使用实际返回值构造下一次参数。默认文本模式也会提供可复制的续读参数。

`max_chars=0` 表示从 `start_index` 一直读到结尾。起点超过文末时返回空正文、文末位置和 `next_index=null`；负数参数会返回错误。

## 正文版本与刷新

- 缓存命中时显示 `cached=true` 和 `cache_age_seconds`，`fetched_at` 仍是原始抓取时间。
- `content_id` 是完整提取正文的 SHA-256，与本次截取长度无关。
- 携带 `expected_content_id` 后，如果缓存到期或刷新发现正文改变，返回 `ok=false` 并要求从头读取，不混用新旧版本。
- 设置 `refresh=true` 可丢弃这一个 URL 的已完成缓存，重新抓取。同 URL 已经有进行中的请求时，仍会复用该请求。
- 刷新失败会返回错误，不把旧缓存伪装成最新内容。
- 单次 `fetch_pages` 中重复 URL 只抓取一次，返回顺序和条数仍与提交的 URL 列表一致。

## 成功页面的 JSON 字段

`response_format="json"` 返回的是合法 JSON 字符串，仍通过现有 MCP 文本内容传输，不要求客户端支持新的传输协议。

| 字段 | 含义 |
|---|---|
| `ok` / `error` | 页面是否成功；失败时先处理错误，不拼接正文 |
| `url` / `final_url` | 请求 URL / HTTP 跳转后的实际 URL |
| `title` / `text` | 页面标题 / 本次返回的正文片段 |
| `fetched_at` | 抓取并提取完成的 UTC 时间 |
| `content_id` | 完整正文的版本哈希 |
| `total_chars` | 完整提取正文的字符数 |
| `start_index` / `end_index` | 本段正文的左闭右开区间 |
| `next_index` | 下一段起点；`null` 表示已经到末尾 |
| `truncated` | 当前段之后是否还有正文 |
| `cached` / `cache_age_seconds` | 是否读取缓存 / 缓存年龄 |

失败页面不保证包含抓取时间、最终 URL 或正文版本。HTTP 成功也不保证正文准确或来源可信，仍需调用方核对。

## 调研资料包的正文预算

`deep_research` 默认每篇最多 20,000 字符，整个资料包正文最多 60,000 字符。需要更短上下文时，例如：

```text
deep_research(query="Python asyncio official documentation",
              num_results=5, fetch_top_n=3,
              max_chars_each=3000, max_total_chars=6000,
              response_format="json")
```

预算按搜索结果顺序分配。超出预算的正文不返回，但该页仍保留来源、长度和 `next_index`，可以再通过 `fetch_page` 读取。已有缓存会被复用；超大页面不进入缓存时，续读可能需要重新下载。

`max_total_chars=0` 表示取消总正文预算，`max_chars_each=0` 表示取消单篇预算。预算只计算 `pages[].text`，不包括标题、索引、摘要和诊断，也不限制 HTTP 下载字节数。

调研 JSON 包含 `results`、`pages`、`diagnostics`、`successful_pages` 和 `returned_body_chars`。`search_status` 区分找到结果、有效无结果和搜索未完成，避免把网络失败当作没有资料。
