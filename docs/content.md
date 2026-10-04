# Markdown 正文与引用

`fetch_page`、`fetch_pages` 和 `deep_research` 都接受 `content_format="markdown"`。默认仍为纯文本；`response_format` 只控制返回封装，两者独立。

```text
fetch_page(url="https://docs.python.org/3/library/asyncio-task.html",
           content_format="markdown", response_format="json", max_chars=5000)
```

返回 JSON 的 `text` 是 Markdown 正文。它保留正文提取器选中的标题、强调、列表、代码、表格和链接。代码块在提取前标记，渲染后恢复原始缩进与空行，避免列表和定义项中的代码被压成一行；代码含反引号时使用更长的围栏。

嵌套列表根据父项标记的实际宽度缩进，`9.` 到 `10.` 的编号变化不会让子项变成同级。列表内的段落和代码块继续属于所在条目，代码在渲染后的内容保留原缩进。支持 `ol start` 的 0–999999999 范围；相邻的独立列表用不可见 HTML 注释分隔，避免 Markdown 把它们连成一组。`reversed` 和单项 `li value` 尚未保留。

表格中恢复的行内代码会对竖线作 Markdown 转义，例如源码中的 `a | b` 仍显示在同一格；反斜杠与反引号的可见内容保留。这依照 [GFM 表格规则](https://github.github.com/gfm/#tables-extension-)，并使用 [markdown-it-py](https://markdown-it-py.readthedocs.io/en/latest/using.html) 的 CommonMark + table 渲染验证，而非只检查输出是否包含代码围栏。

### 表格位置与标题

- 合并单元格展开为占位网格：文字保留在起始格，其余覆盖位置留空，避免重复文字。Markdown 本身不显示真正的跨行／跨列边框。
- 在提取器删除行组结构之前，先处理 `rowspan="0"`（覆盖所在行组的剩余行），并将普通 rowspan 限定在所在行组内。规则依据 [HTML 表格模型](https://html.spec.whatwg.org/multipage/tables.html#processing-model-1)，分别处理 `thead`、`tbody`、`tfoot` 和直接置于 table 下的连续行。
- `caption` 转为表前说明，保留其格式和有效来源链接，不再作为一行数据或另一条表头分隔线。
- 无表头时添加一行空白表头，所有源数据仍放在数据行中；这是 [markdownify 的默认处理约定](https://github.com/matthewwithanm/python-markdownify#options)，不会把首条记录猜成字段名。已选中的行统一补齐列宽，Markdown 只产生一条表头分隔线。
- 列表／定义项内的表格在正文选择期间临时移到所属列表旁，选择结束后按保留的位置恢复到条目内。表格和位置都被保留时才能恢复原层级；不会从未选中的整页表格重新灌入内容。

例如来源的两行数据是 `A / one` 和“第一格仍由 A 跨行占用、第二格为 two”，输出为 `A | one`、`空 | two`，不会变成 `two | 空`。这些调整只作用于 Markdown，纯文本仍由原提取流程生成。

## 链接与引用

相对链接按 HTTP 跳转后的实际 URL 解析；页面有有效 `<base href>` 时遵循该地址。保留 HTTP(S) 链接，移除 JavaScript、data、包含用户凭据或无效地址的链接目标，保留其可读文字。

JSON 中的 `references` 是 Markdown 全文的唯一链接索引，每项含 `url` 和 `text`；不代表独立核实过的证据，也不只对应当前分页。最多返回 100 条，标签文字最多 300 字符。超出时 `references_truncated=true`；正文中的后续链接仍保留。

`structure` 包含完整 Markdown 的 `code_blocks`、`tables` 和唯一 `links` 数量。纯文本模式也返回这份 Markdown 提取元数据。`extractor` 和 `extractor_version` 标明解析器，方便排查版本差异。

## 续读、缓存与刷新

- 偏移按所选格式的 Unicode 字符计算，包括 Markdown 语法。分段可能切开围栏、表格和链接；逐段直接渲染可能不完整，应拼接后渲染。
- 续读保持 `content_format="markdown"`，传入上次的 `next_index` 和 `content_id`。Markdown 与纯文本的版本标识独立，混用会报版本变化。
- 同一个 URL 一次下载后生成两种格式，共享抓取时间和缓存；刷新同时替换两种格式。这样会增加解析 CPU 和缓存占用，缓存的字节预算包含两种正文和引用索引。
- Markdown 提取失败时明确报错，不自动换成纯文本。另一种格式仍可能可用。
- 调研的正文预算只限制 `pages[].text`，不包含引用索引等元数据。

## 内容提示和边界

网页按[编码声明规则](encoding.md)在解析子进程中解码。JSON 的 `encoding_info` 记录所用编码、声明来源和解码错误，分页与缓存沿用同一次读取记录。

`warnings` 是简单提示：`short_content` 表示少于 200 个去掉首尾空白后的字符，`replacement_characters` 表示含 Unicode 替换字符，`missing_title` 表示缺少页面标题。`decoding_errors` 表示原始 HTML 存在所选编码无法解码的字节，即使这些字节位于未提取的注释或广告中也会记录；它与正文中实际出现替换字符的提示不同。纯文本成功而 Markdown 失败时还会有 `markdown_unavailable`。这些提示不能判断事实真伪，也不能保证没有遗漏正文。

只处理静态 HTML，不执行 JavaScript，也不读取登录后的页面或 PDF。正文选择、单元格内块级代码、表格套表格和未被提取器保留的结构仍可能有损失；当前底层提取器还会把单项跨度限制为 100，超过该范围不能保证对齐。只恢复被提取器选中的内容，不完整模拟浏览器表格布局。Markdown 适合阅读和整理，不承诺完整复制原网页布局。正文提取在 [独立解析进程](parsing.md) 中执行，超时或取消时会终止该进程。

实现基于 [Trafilatura 的提取与渲染接口](https://trafilatura.readthedocs.io/en/latest/corefunctions.html)，依赖范围为 `trafilatura>=2.3.0,<3`。已有安装需更新 `requirements.txt` 中的依赖并重新连接 MCP 服务。

## 验证记录

固定样本覆盖代码缩进、空行、中文、反引号围栏、列表与定义项内代码、表格、相对链接、`base`、危险链接和引用数量上限。真实 MCP + 本地 HTTP 验证格式枚举、缓存共享、分页重组、跨格式版本拒绝和刷新。

2026-10-04，通过真实 MCP 子进程读取 [Python asyncio 任务文档](https://docs.python.org/3/library/asyncio-task.html)：返回 61,680 字符 Markdown、34 个代码块、117 个唯一链接，引用索引按上限返回 100 条。纯文本切换 Markdown 命中缓存，分页拼接与全文一致。检查前部示例确认列表中的代码保留换行与缩进；这次实测不代表所有网站布局均能正确提取。

0.2.1 补充了独立渲染验证，检查 HTML 中的实际列表层级、表格列数和代码内容；真实 MCP + 本地 HTTP 样本还覆盖结构化正文的刷新、缓存与分页重组。测试可通过 `requirements-dev.txt` 安装额外的验证依赖，普通安装不增加渲染器。

同日对同一份 Python 文档 HTML 比较 `a5148c7` 和本轮实现：两者渲染后均有 34 个代码块，逐块对照原 HTML，内容均一致（忽略结尾换行）；117 个唯一链接保持不变。旧版渲染时列表内代码脱离了条目，新版有 26 个代码块位于提取结果的列表条目内，包含提取器转换的定义条目。这不表示原 HTML 有 26 个普通列表项。Markdown 由 61,680 增至 63,931 字符，增加部分主要是结构缩进；单次提取约 0.273 → 0.317 秒，仅作本机开销观察，不是通用性能基准。原 HTML 的 SHA-256 为 `b34e08f8270d48c7fd0e873d285d3b3a9c8e58df9ee65295da1563404b89c609`。

0.2.2 增加表格逐格检查：行组边界、rowspan=0、colspan=0、无表头、标题和链接、多层表头、不齐的列数，以及列表／定义项中的多个表格和前后说明。固定样本通过真实 MCP 子进程，验证刷新后结果与缓存分页重组保持同一单元格矩阵。这里的证据来自可控 HTML 样本，尚不代表任意网站的复杂表格都能正确恢复。

0.2.4 增加编码样本：GB2312 标签下按 GB18030 解码、BOM 与 HTTP 声明冲突、Big5、日文及韩文编码、无效字节。真实 MCP 与全新安装通过原始 GB18030 字节检查中文代码、引用、Markdown 表格及缓存分页；未声称能恢复声明错误或没有声明的任意旧网页。
