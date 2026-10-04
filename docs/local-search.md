# 找回本地资料

`search_local` 搜索指定目录中的 UTF-8 文本，返回路径、行号和命中行的前 200 个字符。关键词不区分大小写，采用 Unicode casefold 子串匹配；不是语义搜索、正则搜索或相关性排序。沿用原来的 `query`、`root`、`max_results` 和 `include_ext` 参数，默认仍返回文本。

## 按笔记信息筛选

例如，搜索 Python 集合中同时标有 asyncio 和 MCP、来源属于 python.org 的笔记：

```json
{
  "query": "取消",
  "root": "F:/research",
  "include_ext": "md",
  "collection": "Python",
  "tags": "asyncio, MCP",
  "source_domain": "python.org",
  "response_format": "json"
}
```

- `collection` 比较笔记元数据中的原始集合名，不是保存时清理过的文件夹名；忽略首尾空白和大小写。
- `tags` 用逗号分隔，要求全部精确匹配，忽略大小写。
- `source_domain` 接受裸域名，包含其子域名；`docs.python.org` 可以匹配 `python.org`，`python.org.evil.example` 不可以。只检查元数据里的 HTTP(S) 来源链接，不搜索正文里的域名，也不访问网络。
- 多个条件同时生效。`query` 留空或全为空白时，必须至少设置一个筛选条件，每份笔记返回一条，`line` 为 `null`。
- 有关键词时仍搜索整个文本（包含 frontmatter），按命中行计数。没有元数据的普通文件仍可做关键词搜索。

筛选读取 `save_finding` 当前生成的 `.md` 文件头：`---` 包围，字段值使用 JSON 字符串或字符串数组。这些值也是合法 YAML，但本工具**不是通用 YAML 解析器**；手写裸字符串、YAML 多行值、重复字段或未闭合头部不能用于筛选。头部必须在前 257 行内闭合；无法解析时报告 `skipped.metadata`，不执行 YAML 标签或对象构造。原文件不会修改。

## 范围和资源控制

| 参数 | 默认值 | 说明 |
|---|---|---|
| `max_results` | 50 | 1–1000；达到上限即停止，保守标记结果不完整 |
| `include_ext` | 原有文本与代码扩展名 | 逗号分隔，可带点；空字符串表示所有普通文件 |
| `exclude_dirs` | `.git,node_modules,__pycache__,.venv,venv,.next,dist,build,.godot` | 按目录名忽略大小写，在进入前排除；自定义值覆盖默认值，空值取消目录名排除 |
| `max_entries` | 100000 | 文件和目录条目总数，最大 1000000；被排除目录内部不计数、不遍历 |
| `max_file_bytes` | 2097152 | 单文件默认 2 MiB，最大 16 MiB；分块读取，超大文件不返回部分命中 |
| `time_budget_seconds` | 10 | 大于 0、最大 600；在遍历、读取块和逐行匹配间检查 |

目录名排除只作用于根目录内遇到的子目录。用户直接指定名为 `build` 的根目录时仍可搜索。扩展名以外的文件、符号链接、Windows reparse point（含目录联接和部分云占位文件）及特殊文件属于搜索范围之外；不会跟随这些链接。不会加载 `.gitignore`，不支持目录路径或 glob 排除表达式。

正文仅接受 UTF-8／UTF-8 BOM。含 NUL、非法 UTF-8、超大或无法读取的文件会被跳过并计数，不再静默丢掉坏字节。目录无法读取也会计入 `io_errors`。

文件工作在线程中执行，MCP 主循环可以继续处理其他请求。取消会通知扫描停止并等待线程结束，重复取消也不会丢下仍在扫描的线程。同步文件系统调用本身无法强制打断；慢网盘、磁盘故障或系统调用卡住时，返回与取消可能超过预算。此机制不是严格墙钟时限，也不是针对恶意并发路径替换的文件系统沙箱；应搜索受信任的本地目录。

## JSON 和完整性

成功执行返回 `ok: true`、`matches`、`complete`、`stop_reason`、`stats`、`skipped`、`filters`、`elapsed_seconds`。参数或根目录错误返回 `ok: false` 与 `error`。

`ok` 表示工具执行成功，不等于完整搜索。达到匹配上限、条目上限或时间预算时，`stop_reason` 分别为 `max_results`、`max_entries`、`time_budget`，保留已有命中。超大、二进制、编码错误、读写错误，或筛选时遇到无法解析的元数据，也使 `complete` 为 `false`。这时不能用空结果判断资料不存在。

主动排除目录、扩展名、链接和特殊文件不使 `complete` 变为 `false`；完整性只针对声明的范围。没有筛选时，头部不规范不妨碍全文搜索。每个命中附元数据预览：来源最多 2048 字符，其他字符串字段最多 256 字符，标签最多 32 个、每个 64 字符；截断用 `metadata_truncated` 标明，筛选仍使用完整字段。

扫描使用文件系统返回的顺序，不保证跨平台排序，也不提供续页游标。每次读取当前文件，不持久化索引或缓存；保存、修改和删除在下一次调用中可见。正在修改的目录不具有快照一致性。Python 代码可用 `from mcp_harries_resourcer import server`，直接调用 `server.search_local` 需 `await`；MCP 调用方式不变。

## 依据与验证

目录排除借鉴 [官方 Filesystem MCP 的搜索实现](https://github.com/modelcontextprotocol/servers/blob/main/src/filesystem/lib.ts) 的遍历前剪枝思路；该项目搜索文件名，本项目继续搜索文件内容。使用 [Python scandir](https://docs.python.org/3/library/os.html#os.scandir) 逐目录枚举并及时关闭句柄，实现独立编写，没有引入搜索服务或额外依赖。

回归覆盖保存后的组合筛选、来源域名边界、纯筛选浏览、修改与删除、坏编码、文件读取时增长、目录剪枝、显式根目录、数量／时间限制和重复取消。真实 MCP 子进程验证保存 → 筛选 → 修改 → 再搜索 → 删除；Windows 验证实际目录联接，Linux CI 验证符号链接与循环链接。

本批本地运行 193 项测试，192 项通过、1 项因 Windows 符号链接权限不足跳过，实际目录联接测试通过。期间发现已有批量缓存清理测试的 30 毫秒调度窗口偶发过短，改用 1 秒窗口，并增加慢任务启动、取消完成、超时状态与未完成 URL 断言；生产网络预算不变。四组跨平台发布结果以 [Actions](https://github.com/fisHarly0/mcp-harries-resourcer/actions/workflows/tests.yml) 为准。

生成目录基准：`python scripts/local_search_benchmark.py --output <结果.json>`。先把 `TEMP`／`TMP` 指向所需临时盘；脚本生成 1000 个笔记和排除目录内的 10000 个文件，交替运行四轮旧遍历方式与新版，核对命中一致。结果用于判断此样本的成本，不代表所有磁盘或真实资料库的表现。

2026-10-04，Windows / Python 3.12.12 / F 盘测量：旧方式四轮为 0.867、0.147、0.148、0.176 秒，新版为 0.087、0.086、0.098、0.087 秒；中位数分别约 0.162 与 0.087 秒。新版只枚举 1002 个条目、读取 1000 个文件，没有进入排除目录。初版曾因每次按文件上限读取导致小文件分配开销增加，测得约 0.502 秒；改成最多 64 KiB 分块读取后得到上述结果。
