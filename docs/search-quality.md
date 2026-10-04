# 搜索结果是否找到了目标资料

域名符合筛选条件，只能证明来源范围正确。搜索「Python asyncio 官方文档」却返回 Python 中文文档首页，仍然没有找到指定专题。现在把这两项分开记录。

## 固定样本与运行方式

[search_cases.json](../scripts/search_cases.json) 包含 asyncio、React useEffect、FastAPI 依赖注入三个主题的中英文查询，共 6 例。参考页面来自 [Python 官方文档](https://docs.python.org/3/library/asyncio.html)、[React 官方文档](https://react.dev/reference/react/useEffect) 和 [FastAPI 官方文档](https://fastapi.tiangolo.com/tutorial/dependencies/)。这是已知文档页的检索样本，不代表通用搜索题库。

在源码仓库目录运行：

```text
python scripts/search_smoke.py --output search-evidence.json
```

默认比较 `fallback` 与 `merge`，每例取前 5 条，合计 12 次搜索。通过真实 stdio MCP 调用 `web_search_multi`，每次只传一个查询，共用生产搜索逻辑，并使用 30 秒整批预算。网络限速、重试与诊断继续生效。

可调整：

```text
python scripts/search_smoke.py --case asyncio-zh --case react-effect-zh --strategies fallback --repeats 2 --per-query 5 --time-budget-seconds 30 --output selected-evidence.json
```

`--cases` 可指定其他 JSON 样本文件；每例需有唯一 `id`、`language`、`query`、非空 `include_domains` 和 `reference_urls`。参考 URL 必须属于允许域名，域名使用小写 ASCII 裸主机名。重复次数为 1–10，每次结果数为 1–30。

脚本不会在普通单元测试或 CI 中自动联网搜索。需要严格检查预期页是否出现时增加 `--require-reference`；任意一轮未命中就返回非零退出码，包括受限或超时的轮次。这表示该次检索未达到预期，不能证明网上没有该资料。默认仅在域名不符合要求或执行错误时失败。

## 如何读报告

| 字段 | 含义 |
|---|---|
| `domain_check` | 返回 URL 是否均属于指定域名；空结果为 `null` |
| `reference_found` | 本轮返回结果中是否出现样本定义的参考页面 |
| `first_reference_rank` | 第一条参考页面的返回序号，从 1 开始；没有则为 `null` |
| `reciprocal_rank` | 首个参考页面序号的倒数；未找到为 0 |
| `complete` / `diagnostics` / `attempts` | 保留生产工具的搜索完成状态、引擎错误和过滤数量 |
| `run_complete` / `planned_runs` | 是否跑完全部样本及计划次数；与单次搜索是否完整是不同概念 |
| `summary` | 按策略、语言分别汇总有结果轮次、参考页命中、平均倒数排名、未完成且无参考页轮次和耗时 |

参考匹配比较协议、主机、有效端口和路径，忽略片段、查询参数以及末尾斜杠；主机忽略大小写。域名允许子域名，参考页面仍须与列出的主机和路径匹配。其他版本、译文或同样有用的相关页面可能未列入参考，因此**参考命中率不是人工相关性评分，也不是完整召回率**。

报告保留每条结果的标题、摘要、链接、原始来源、逐条匹配判断和案例定义。每完成一轮就原子替换输出文件；中断或 MCP 异常时保留已完成记录，`run_complete=false`。Git 提交、工作区是否有修改、生产代码及评估脚本的指纹用于区分运行版本；Git 不可用时提交信息为 `null`。不记录代理地址或环境变量内容。

两种策略按案例和重复轮次交替先后顺序，减少固定后手受到限流的偏差，但不能消除上游状态、代理、时间和网络环境的影响。不同批次的小样本结果不能当成因果实验。

## 2026-10-04 实测

本机 Windows 环境，6 例 × 2 策略各运行一次，前 5 条结果、每次 30 秒预算，保留默认限速。[公开结果记录](evidence/search-reference-2026-10-04.json)保留了 12 轮的返回 URL、标题、来源、诊断和判断，省略摘要。该报告以 `ff753de` 为基线，在含标题修复和评估器修改的工作树中运行，`source_sha256` 标记实际生产源码；不是对未修改 `ff753de` 的测量。

| 查询 | fallback | merge |
|---|---|---|
| asyncio 英文 | 参考页第 1 条；有引擎失败 | 参考页第 1 条；有引擎失败 |
| asyncio 中文 | 仅返回中文文档首页 | 仅返回中文文档首页；有引擎失败 |
| React useEffect 英文 | 返回首页／入门页；有引擎失败 | 没有可用结果，搜索未完成 |
| React useEffect 中文 | 返回首页／入门页 | 返回首页／入门页；有引擎失败 |
| FastAPI 依赖注入英文 | 没有可用结果，搜索未完成 | 没有可用结果，搜索未完成 |
| FastAPI 依赖注入中文 | 没有可用结果，搜索未完成 | 没有可用结果，搜索未完成 |

本次观察中参考页命中为 **2/12**，所有实际返回链接均符合域名约束；DuckDuckGo 多次返回 HTTP 202，Bing 部分候选被域名筛选排除。该样本没有显示合并策略提高参考页覆盖率。保留默认策略，并把可选后端评估建立在同一组案例和原始证据上。

另外对 3 个查询比较了 `cn.bing.com` 与直接 `www.bing.com`，以及参考 [SearXNG Bing 适配器](https://github.com/searxng/searxng/blob/master/searx/engines/bing.py) 的 `setlang` 参数构造方式。直接域名避免了地区跳转追加 `mkt`，但这组对照仍未命中参考页；语言参数方案也没有改善命中。没有据此改变生产请求策略或移植其代码。

SearXNG 仍是可选后端候选。[官方 API 文档](https://docs.searxng.org/dev/search_api.html)说明 JSON 格式需要实例启用，许多公共实例关闭该格式。本次没有可供比较的已配置实例，没有把 API 接口存在写成搜索质量已经改善。

## 本批修正

搜索标题和摘要改为保留行内文本的原有空白，再统一连续空白；`br` 作为换行空格处理。这样 `Python <b>documentation</b>` 不会拼成一个词，`<b>use</b>Effect` 不会被拆开，中文词组和标点也不会凭空加空格。回归样本覆盖这些情况，以及首页误判、相似域名、端口、空结果和报告中断。

本地运行 203 项测试，202 项通过、1 项因 Windows 符号链接权限不足跳过；公开记录的全部判断和汇总也经过离线复核。跨平台回归及隔离安装结果以 [Actions](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml) 对应提交为准。
