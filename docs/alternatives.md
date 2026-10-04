# 类似开源项目对比

核对日期：2026-10-04。依据各项目官方 GitHub README 和 GitHub 仓库许可证信息；没有安装或实测这些替代项目。以下四个 MCP 仓库当日均为 MIT、未归档。MCP 连接器的许可证不代表背后托管服务免费，或其全部基础设施使用相同许可证。

| 项目 | 与本项目的交集 | 主要依赖与差异 | 可借鉴之处 |
|---|---|---|---|
| [nickclyde/duckduckgo-mcp-server](https://github.com/nickclyde/duckduckgo-mcp-server) | DuckDuckGo 搜索、正文获取，最接近轻量免搜索 Key 路线 | 支持 uvx 启动；有请求限速、正文缓存和分页等配置 | 安装分发、限速、缓存与输出长度控制 |
| [ihor-sokoliuk/mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng) | 为 MCP 客户端提供网页搜索 | 需要可访问的 SearXNG 实例，把搜索交给聚合搜索后端 | 将后端适配与 MCP 工具分开，作为后续可选搜索源 |
| [tavily-ai/tavily-mcp](https://github.com/tavily-ai/tavily-mcp) | 搜索、正文提取、站点地图和抓取 | 使用 Tavily 服务；托管连接支持 API Key 或 OAuth，须考虑服务额度与条款 | 清晰的工具边界、参数说明、托管连接体验 |
| [firecrawl/firecrawl-mcp-server](https://github.com/firecrawl/firecrawl-mcp-server) | 网页搜索、抓取与内容提取 | 通常接入 Firecrawl 服务；也提供自托管 API 地址配置，部署成本应单独评估 | 批量处理、重试策略、抓取任务的状态反馈 |

## 对本项目的建议

最接近的参照是 DuckDuckGo MCP Server。若只需要通用搜索和正文读取，可优先评估它，避免重复维护相同能力。

本项目值得保留的组合是：中文站点快捷搜索、Bing / DuckDuckGo 切换、本地 Markdown 收藏与目录全文检索。此次修复保持这条轻量路线，没有引入新服务或收费 API。

后续若遇到持续限流，可考虑把 SearXNG 作为可选后端；若主要困难是复杂网页的正文获取，再评估 Firecrawl。没有必要在尚未验证需求时一次接入所有后端。

Tavily 适合愿意使用服务账号与配额换取托管搜索能力的场景，不能作为“免 Key 本地工具”的无成本替换。

## 此次直接落实的改进

- 搜索失败和真实无结果分开报告，保留后备引擎诊断。
- 固定 HTML 样本、模拟 HTTP 与真实 stdio 协议测试。
- 以完整“搜索—抓取—保存—检索”流程组织安装和使用文档。
- 后续修正已加入进程内共享请求限速、正文 TTL/LRU 缓存、内容容量预算，以及并发相同 URL 的下载复用；独立实现，无需引入其他项目代码或额外依赖。
- 持续优化的首批进一步加入分页续读、正文版本校验、单次刷新、JSON 输出和调研正文总预算；参数与行为见 [阅读指南](reading.md)，后续方向见 [优化路线](roadmap.md)。

后续批次已加入标准 Python 包、命令入口和 Git 来源的 `uvx --from` 启动方式，见 [安装说明](installation.md)。第三方搜索后端和 PyPI 发布仍未实施。
