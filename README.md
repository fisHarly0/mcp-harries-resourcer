# mcp-harries-resourcer

一个给 [Claude Code](https://claude.com/claude-code) 用的 **资料查找 / 调研 MCP server**。一键搜索、抓取正文、并发深度调研，结果直接落到本地 markdown 收藏夹，方便整晚批量调研。

> 作者自用工具，公开分享。代码风格偏务实，欢迎 fork / 改造。

## 它能做什么

| 工具 | 用途 |
|---|---|
| `web_search` | 通用搜索，DuckDuckGo 优先，限流自动 fallback 到 Bing |
| `web_search_multi` | 并发跑多个 query，结果合并去重 |
| `search_chinese` | 知乎 / B站 / 微信公众号 / 简书 / CSDN / 雪球 定向搜索 |
| `fetch_page` | 抓单页正文，自动去广告/导航 |
| `fetch_pages` | 并发批量抓取（默认 5 路并发，避免被封） |
| `deep_research` | 一站式：搜索 → 抓前 N 篇正文 → 汇总 |
| `search_local` | 本地目录递归全文搜索 |
| `save_finding` | 整理好的资料写入 `~/research/<collection>/` |

## 安装

需要 Python 3.10+。

```bash
git clone https://github.com/<your-username>/mcp-harries-resourcer.git
cd mcp-harries-resourcer
pip install -r requirements.txt
```

## 接入 Claude Code

```bash
claude mcp add resourcer -- python /absolute/path/to/server.py
```

Windows 示例：

```powershell
claude mcp add resourcer -- python C:\Users\YOU\mcp-harries-resourcer\server.py
```

接入后跑一下确认：

```bash
claude mcp list
# resourcer: ... - ✓ Connected
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RESOURCER_RESEARCH_ROOT` | `~/research` | `save_finding` 落盘根目录 |

PowerShell 设置：

```powershell
[Environment]::SetEnvironmentVariable("RESOURCER_RESEARCH_ROOT", "D:\my-research", "User")
```

## 使用示例

接入后直接在 Claude Code 里说人话：

- "帮我调研一下 2026 年 MCP 协议的演进，整理一份资料"
  → 自动 `deep_research` → 抓正文 → `save_finding` 落盘
- "在 D:\\code 下搜一下 `RESEARCH_ROOT` 用在哪些地方"
  → `search_local`
- "把这几个 URL 的正文都抓回来" + 列表
  → `fetch_pages` 并发抓

## 设计取舍

- **没用 SerpAPI / Google CSE**：免 key、零成本，但靠抓取 DDG/Bing 的 HTML，引擎改版时需要更新解析器。
- **trafilatura 做正文提取**：比 readability-lxml 召回率高，对中文站点也友好。
- **Bing 走 `cn.bing.com`**：国内访问最稳定，靠 `mkt` 参数切换语言偏好。
- **stdio 传输**：不用 HTTP，全程本地，零网络暴露。所以 server 里禁用任何 stdout `print`。

## 已知限制

- DuckDuckGo HTML 端点有速率限制，触发后自动 fallback 到 Bing。
- Bing 的 `/ck/a` URL 包装格式偶尔会变，解析失败时会回退原 URL。
- 中文搜索引擎对 `site:` 语法支持不一致，`search_chinese` 命中率视目标站点而定。

## 合规说明

本工具仅作个人学习与研究用途。使用者应：

- 遵守目标站点的 `robots.txt` 与服务条款；
- 控制请求频率，不做高频抓取；
- 不将抓取结果用于商业再分发。

抓取部分通过 User-Agent 伪装为常规浏览器，仅为通过反爬启发式（与所有 headless 浏览器同理），不承担因滥用导致的责任。

## License

[MIT](./LICENSE)
