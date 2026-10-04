# MCP 进度反馈

六个联网工具支持 MCP `notifications/progress`：`web_search`、`web_search_multi`、`search_chinese`、`fetch_page`、`fetch_pages` 和 `deep_research`。通知在工具运行期间发送，最终正文和 JSON 返回格式保持原样。

客户端需在调用元数据中提供 `progressToken`。没有 token 时不发送通知、不创建通知任务；直接调用 Python 函数的现有用法也不需要改。客户端是否显示进度条、文字或日志由客户端决定，服务不能保证每个客户端都展示这些通知。

## Python 客户端示例

在已经初始化的 `ClientSession` 中：

```python
async def on_progress(progress, total, message):
    # 这是客户端的输出，不是 MCP 服务进程的 stdout。
    print(f"{progress}/{total if total is not None else '?'}: {message}")

result = await session.call_tool(
    "fetch_pages",
    {"urls": ["https://docs.python.org/3/library/asyncio.html",
              "https://docs.python.org/3/library/asyncio-task.html"],
     "content_format": "markdown", "response_format": "json"},
    progress_callback=on_progress,
)
```

SDK 为回调生成对应 token。`ctx` 由服务器内部注入，不是工具参数，也不会出现在 `tools/list` 的输入 schema 中。不要把 `progressToken`、回调或 `ctx` 填进工具的 `arguments`。

## 计数规则

| 工具 | 单位与总量 |
|---|---|
| 单次搜索、中文站点搜索 | 1 次查询；开始与结束通知 |
| 多查询搜索 | 输入的查询数；每个查询结束计 1 |
| 单页读取 | 1 页；等待、下载、解析期间为进行中，返回成功或错误后结束 |
| 批量读取 | 不同 URL 的数量；重复 URL 不重复计数，缓存命中也算该 URL 已结束 |
| 调研 | 开始时总量未知；搜索结束后为 1 个搜索阶段 + 实际计划读取的页面数 |

**进度表示工作项已结束，不等于所有资料成功。** 返回明确错误的查询或页面也算已结束，消息会说明失败数量。搜索计数与 JSON 的 `ok` 一致：有可用来源或正常无结果算成功；有来源但部分引擎失败时仍可能 `complete=false`。判断可用性仍需查看最终 `ok`、`complete`、`batch` 和逐项错误。

整批到期中止或尚未开始的工作不补成完成，所以最后一条进度可能小于总量。单页自身已返回超时错误则属于已结束的失败项。主动取消不会发送“全部完成”通知。不要等待进度达到 100% 才处理最终响应。

## 通知边界

- 每个请求独立计数，已发送的值严格递增；调研总量未知时不编造百分比。
- 同一请求只保留最新待发送状态，发送间隔至少 100 毫秒。快速调用可能只有最后一条通知；不保证每个中间计数都会送达。
- 通知与工作任务分开，通知发送最多等待 250 毫秒；失败或发送超时后停止该请求的通知，不丢弃已取得的正文。
- 正常结束时尝试发送最新状态，最多额外等待 350 毫秒，然后取消并回收通知任务。主动取消时直接回收，不冲刷待发状态；最终响应之后不会继续发送进度。
- 通知不包含查询文字或 URL，只含阶段和计数。它不是单页下载字节进度，也不是解析 CPU 完成百分比。
- 使用 `mcp>=1.30.0,<2`。更新已有安装的依赖并重新连接服务后生效。

设计依据：[MCP 进度规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress)。实现采用当前 v1 SDK 的 `Context.report_progress`，没有接入后台任务或改变 stdio 传输。

## 验证

单元测试覆盖通知合并、单调计数、重复 URL、失败项、整批超时、请求隔离、无 token、发送异常与堵塞、取消回收。真实 MCP 子进程测试验证 schema 不暴露 `ctx`，通知在最终响应之前送达，并发请求不混用进度，调研总量与实际阶段一致，取消的读取不会发出完成通知。

2026-10-04 真实联网读取两篇 Python 官方文档，客户端在约 0.84、1.76、3.21 秒收到 `0/2`、`1/2`、`2/2`，两页成功，通知均在最终响应前到达。这是协议与当前网络的单次验证，不保证其他客户端的界面展示或网络速度。
