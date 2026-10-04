# 可选 SearXNG 接入

适用于已有自建或获准使用的 SearXNG 实例。Resourcer 不内置公共实例、不部署 SearXNG；默认搜索仍通过 Bing／DuckDuckGo。这里新增的是接入能力，尚未测得真实实例的搜索质量改善。

## 配置

1. 在实例的 `settings.yml` 中为 `search.formats` 启用 `json`，保留原有需要的格式，例如：

   ```yaml
   search:
     formats:
       - html
       - json
   ```

2. 向 MCP 服务子进程传入实例地址：

   ```json
   {"RESOURCER_SEARXNG_URL": "http://localhost:8080"}
   ```

3. 重新连接 MCP 服务，在下面四个工具的调用中指定 `backend="searxng"`。

可用 HTTP(S)、自定义端口、IPv6 和子路径，例如 `https://search.example.org/tools/` 对应 `/tools/search`。也接受直接填写 `/search` 端点。不接受 URL 中的账号、密码、查询参数或片段；当前没有额外认证头配置。需要认证的实例应先提供受控的、可由 MCP 服务直接访问的接口。

JSON 格式需要由实例开启；未开启可能返回 403，公共实例也可能禁用此格式。这是 [SearXNG Search API](https://docs.searxng.org/dev/search_api.html) 的要求，HTTP 403 本身不能唯一确定是格式配置问题还是访问权限问题。

## 调用

```python
web_search(
    query="Python asyncio 官方文档",
    backend="searxng",
    include_domains=["docs.python.org"],
    response_format="json",
)

web_search_multi(
    queries=["Python asyncio official documentation", "Python asyncio 官方文档"],
    backend="searxng",
    time_budget_seconds=30,
    response_format="json",
)

search_chinese(query="异步编程", site="zhihu", backend="searxng")

deep_research(
    query="Python asyncio 官方文档",
    backend="searxng",
    include_domains=["docs.python.org"],
    fetch_top_n=2,
    content_format="markdown",
    response_format="json",
)
```

保留默认 `strategy="fallback"` 即可。SearXNG 自己负责聚合，`strategy="merge"` 属于内置双引擎策略，和 `backend="searxng"` 同时使用会在请求前报参数错误。JSON 用 `backend="searxng"`、`effective_strategy="instance"` 表示实际执行方式；内置搜索也新增这两个字段，原有字段保持兼容。

## 结果与失败

- 每次查询只请求实例的第一页、`general` 分类，最多处理前 50 条候选，按实例原始顺序筛选、去重并截取需要的条数。语言、安全搜索和启用哪些上游引擎沿用实例配置。
- 包含／排除域名同时作为查询提示和本地链接检查；不依赖实例是否正确处理 `site:`。原有“结果少于请求数”和正文跳转边界仍适用。
- 每条结果的 `provenance` 保留 `engine="SearXNG"`、聚合列表的 `rank`、原始 `url` 和 `upstream_engines`。上游名称来自实例；聚合排名不代表某个上游引擎的原始排名。没有引擎信息时返回空列表，不猜测来源。
- 实例返回 `unresponsive_engines` 时，保留有效来源，`complete=false`，对应 attempt 标为 `partial`，附上游错误。没有来源且存在上游失败时为 `incomplete`，不能解释为没有资料。结构损坏的候选被跳过并报告不完整。
- HTML 登录页、无效 JSON、HTTP 错误、下载超限、超时分别报告。诊断不返回实例错误页面正文。
- 选中 SearXNG 后不会回退到内置搜索，也不跟随 HTTP 跳转；遇到跳转请配置最终端点。这保证适配器只向配置的搜索端点发送查询。实例本身仍会按其配置向上游引擎发送请求；`deep_research` 还会访问选中的来源网页。
- SearXNG 有独立搜索限速队列，沿用 `RESOURCER_SEARCH_RPM` 默认 30。HTTP 重试、响应大小、总请求预算和批量截止时间沿用已有设置。取消会等待正在进行的请求清理。

## 验证范围

`tests/test_searxng.py` 覆盖配置、JSON 协议、聚合排名与去重、域名边界、上游部分失败、无效响应、HTTP 403／429、超大响应、跳转拒绝、限速接入、超时和取消。真实 stdio MCP 连接本地 HTTP 样本，执行四个搜索工具、Markdown 正文读取，以及搜索质量脚本。

这些检查验证协议和工具流程，不等于连接了真实 SearXNG 服务。获得可用实例后，可对同一组中英文案例测量：

```powershell
$env:RESOURCER_SEARXNG_URL = 'http://localhost:8080'
python scripts/search_smoke.py --backend searxng --output searxng-results.json
python scripts/search_smoke.py --backend builtin --output builtin-results.json
```

SearXNG 默认只运行一次实例聚合，内置后端默认比较 fallback／merge。每份报告记录后端，结果仍区分域名正确与已知专题页命中；同一报告仅测一种后端。脚本不会保存实例地址，公开报告前仍需检查查询和来源链接。更完整的指标解释见 [搜索质量验证](search-quality.md)。
