# 网页编码

`fetch_page`、`fetch_pages`、`deep_research` 的正文，以及内置 Bing／DuckDuckGo 搜索 HTML，都将下载后的原始字节交给解析子进程。先确定字符编码，再提取标题、正文、代码、引用或搜索结果。SearXNG 使用独立 JSON 协议，不应用 HTML 编码扫描。

此前直接读取 HTTPX 的 `response.text`。按 [HTTPX 文档](https://www.python-httpx.org/advanced/text-encodings/)，没有 HTTP 字符集声明时默认 UTF-8；HTML 自身的 GBK 声明不会改变这个默认值，因此会在正文提取前丢失中文。

## 选择顺序

| 优先级 | 来源 | 行为 |
|---|---|---|
| 1 | 文件开头的 BOM | 识别 UTF-8、UTF-16LE、UTF-16BE，并移除开头的 BOM |
| 2 | HTTP Content-Type 的 charset | 使用有效的 Web 编码标签，优先于页面内声明 |
| 3 | 前 1,024 字节内的 meta | 支持 `charset`，或 `http-equiv="Content-Type"` 配合 `content`；使用第一个受支持的声明 |
| 4 | 没有受支持的声明 | 使用 UTF-8 |

优先级和扫描范围参考 [HTML 编码判定](https://html.spec.whatwg.org/multipage/parsing.html#determining-the-character-encoding)。扫描忽略注释、其他属性中引用的 meta，以及脚本、样式和文本示例容器内的伪标签。声明须在扫描范围内完整结束；不继续扫描全文，也不在后续解析中重新选择编码。

编码标签由 [python-webencodings](https://github.com/gsnedders/python-webencodings) 规范化，实际解码使用 Python 编码器。GB2312／GBK 标签使用 GB18030 解码，包含四字节字符；ISO-8859-1 等 Web 别名按 Windows-1252 处理。meta 声明 UTF-16 时按 UTF-8 处理，`x-user-defined` 按 Windows-1252 处理；真正的 UTF-16 可由 BOM 或 HTTP 声明确定。这些转换参照 [Encoding Standard](https://encoding.spec.whatwg.org/)。不接受 UTF-7、Python 转换编码或如今代表 replacement 的旧标签。

## 结果记录

正文 JSON 中增加：

```json
{
  "encoding_info": {
    "encoding": "gb18030",
    "source": "meta",
    "had_errors": false
  }
}
```

`source` 为 `bom`、`http`、`meta` 或 `default`。`encoding` 是实际选用的规范名称，因此 GBK 声明会记录 `gb18030`。搜索使用同一解码规则，但当前搜索结果没有输出该诊断对象。

`had_errors=true` 表示按所选编码解码时遇到无效字节：保留可读部分，用 Unicode 替换字符代替无效片段，并在正文 `warnings` 中附 `decoding_errors`。这个标志覆盖整份 HTML，即使受损字节只存在于注释中；原有 `replacement_characters` 只检查提取出的正文。原文自身包含合法的替换字符时，不算解码错误。

纯文本与 Markdown 来自同一次解码，共享编码记录。缓存命中与分页保留原记录；刷新后重新判断编码。仅更换源字节编码、而提取出的文字不变时，内容版本标识保持不变。

## 边界与验证

- 不使用统计猜测。无声明的 GBK 页面、声明太晚或声明错误的页面仍可能乱码。有效但错误的 HTTP 声明不会被正文猜测覆盖；`had_errors=false` 也不能证明所选编码正确。
- 不实现浏览器完整的 HTML 编码重新解析或全部历史字符映射。少见多字节字符、错误字节序列的恢复方式可能与浏览器不同，受当前 Python 编码器影响。
- 这是静态 HTML 的解码过程，不执行脚本，也不增加登录页面或 PDF 支持。
- 原始字节通过 Base64 传给解析进程，解码与提取共用已有预算；HTTP 下载和解压上限保持有效。

固定测试覆盖中文、繁体、日文、韩文、BOM、声明优先级、页头边界、无效标签、伪 meta 和损坏字节。真实 MCP + 本地 HTTP 以 GB18030 字节提供中文样本，验证标题、代码、引用、表格、缓存和分页。wheel 安装后从无关目录启动命令，也读取仅在 HTML 声明编码的样本。

2026-10-04 的 GBK 复现样本中，旧路径将标题读成乱码，原始解码文本出现 378 个替换字符；修复后标题为“中文编码测试”，正文含预期的“中文技术资料”，解码错误标志为 false。这是可控样本验证，不代表所有旧网页均可正确恢复。
