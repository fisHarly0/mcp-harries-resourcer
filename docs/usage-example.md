# 使用演示：搜索、抓取、保存、找回

下面是可复用的演示流程。静态片段标为示例，不代表某次实时搜索结果；真实联网结果随时间和网络环境变化。

## 1. 收集资料

向客户端发送：

> 查找 Python asyncio 官方入门资料，搜索 5 条并抓取前 2 条正文。列出来源和抓取失败项，阅读后用中文整理笔记，保存到 python-demo，再检索保存的笔记。

客户端可调用：

```json
{
  "tool": "deep_research",
  "arguments": {
    "query": "Python asyncio official documentation",
    "num_results": 5,
    "fetch_top_n": 2,
    "max_chars_each": 5000
  }
}
```

正常返回包括搜索索引、来源链接、每页正文和成功数量。下列仅示意部分抓取失败的输出：

```text
# 深度调研：Python asyncio official documentation
_共 5 条搜索结果，尝试抓取 2 篇，成功 1 篇_

## 结果索引
✅ 1. [asyncio documentation](https://docs.python.org/3/library/asyncio.html)
❌ 2. [另一个来源](https://example.org/blocked)
```

如果搜索返回“搜索未完成”，先查看诊断，不能把它写成“互联网上没有相关资料”。正文抓取失败时，仍保留来源和错误信息。

短时间内再次调用 `fetch_page`、`fetch_pages` 或 `deep_research` 读取同一 URL，可能复用成功正文的缓存。输出会显示缓存年龄，默认 5 分钟后重新抓取；搜索索引本身仍从网络获取。第一次将正文截取到较少字符，不会限制后续更长的读取。

## 2. 整理并保存

`deep_research` 不会生成模型总结，也不会写文件。客户端阅读来源后，显式调用：

```json
{
  "tool": "save_finding",
  "arguments": {
    "collection": "python-demo",
    "title": "asyncio 入门笔记",
    "content": "# asyncio 入门笔记\n\n在这里写根据来源核对过的笔记。\n\n来源：https://docs.python.org/3/library/asyncio.html\n",
    "source_url": "https://docs.python.org/3/library/asyncio.html",
    "tags": "python, asyncio"
  }
}
```

返回实际文件的绝对路径，以及该 collection 下的 Markdown 数量。保存内容示例：

```markdown
---
title: "asyncio 入门笔记"
collection: "python-demo"
saved_at: "2026-10-04T12:00:00"
source_url: "https://docs.python.org/3/library/asyncio.html"
tags: ["python", "asyncio"]
---
# asyncio 入门笔记

在这里写根据来源核对过的笔记。
```

同一秒保存同标题时，第二份文件会得到 `-1` 后缀；原文件保留。

## 3. 搜索保存的资料

调用 `search_local`：`query` 填 `asyncio`，`root` 填上一步实际保存目录的绝对路径，`include_ext` 填 `md`。结果包含文件名、行号和命中文本，可据此打开原始笔记。

## 本地离线验收

`python -m unittest discover -s tests -v` 会启动真实 stdio MCP 子进程，验证初始化、8 个工具的注册、保存文件和本地检索。搜索引擎解析与切换使用固定样本测试，避免把外网抖动误判成代码回归。
