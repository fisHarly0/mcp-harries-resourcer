"""Bounded HTTP reads, polite recovery and per-origin Retry-After cooldowns."""
import asyncio
from .task_cleanup import wait_for_owned
from email.utils import parsedate_to_datetime
import math
import time
import zlib

import httpx


class HTTPPolicyError(Exception):
    def __init__(self, code, message, *, retry_after_seconds=None):
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


def retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        # Large valid delays stay large: never shorten a server's requested wait.
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None


class HTTPPolicy:
    def __init__(self, *, total_timeout=60, max_bytes=8 * 1024 * 1024,
                 retries=1, max_redirects=5, clock=time.monotonic,
                 wall_clock=time.time, sleep=asyncio.sleep):
        self.total_timeout = total_timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.max_redirects = max_redirects
        self.clock = clock
        self.wall_clock = wall_clock
        self.sleep = sleep
        self.cooldowns = {}
        self.overflow_until = 0.0

    @staticmethod
    def _origin(url):
        return (url.scheme, url.host, url.port)

    def _defer(self, origin, delay):
        now = self.clock()
        self.cooldowns = {key: end for key, end in self.cooldowns.items() if end > now}
        until = now + delay
        if origin in self.cooldowns or len(self.cooldowns) < 256:
            self.cooldowns[origin] = max(self.cooldowns.get(origin, 0), until)
        else:
            # Bound retained origin state without forgetting an active cooldown.
            self.overflow_until = max(self.overflow_until, until)

    async def _wait_origin(self, origin, deadline):
        while True:
            delay = max(self.cooldowns.get(origin, 0), self.overflow_until) - self.clock()
            if delay <= 0:
                self.cooldowns.pop(origin, None)
                return
            if delay >= deadline - self.clock():
                raise HTTPPolicyError("cooldown", "站点仍在限流等待期，超过本次请求时间预算，请稍后重试",
                                      retry_after_seconds=math.ceil(delay) if math.isfinite(delay) else None)
            await self.sleep(delay)

    async def request(self, client, method, url, *, headers=None, pace=None, follow_redirects=True):
        """GET and this server's read-only search POST only; cancellation propagates."""
        started = self.clock()
        try:
            return await wait_for_owned(
                self._request(client, method, url, headers, pace, started, follow_redirects), self.total_timeout)
        except asyncio.TimeoutError as exc:
            raise HTTPPolicyError("deadline", "请求总时间预算已用尽") from exc

    async def _request(self, client, method, url, headers, pace, started, follow_redirects):
        deadline = started + self.total_timeout
        requests = 0
        retries_used = 0
        original = client.build_request(method, url, headers={"Accept-Encoding": "gzip, deflate", **(headers or {})})
        while True:
            request = original
            redirects = 0
            retry_delay = None
            try:
                while True:
                    origin = self._origin(request.url)
                    while True:
                        await self._wait_origin(origin, deadline)
                        if pace is not None:
                            await pace()
                        # Re-pace if a concurrent response delayed this send.
                        if max(self.cooldowns.get(origin, 0), self.overflow_until) <= self.clock():
                            break
                    if self.clock() >= deadline:
                        raise HTTPPolicyError("deadline", "请求总时间预算已用尽")
                    requests += 1
                    response = await client.send(request, stream=True, follow_redirects=False)
                    try:
                        if response.status_code in {301, 302, 303, 307, 308} and response.next_request:
                            if not follow_redirects:
                                raise HTTPPolicyError("redirect_blocked", "搜索实例发生 HTTP 跳转；请配置最终实例地址，不自动转发查询")
                            if redirects >= self.max_redirects:
                                raise HTTPPolicyError("redirect_limit", "HTTP 跳转次数超过上限")
                            redirects += 1
                            request = response.next_request
                            continue

                        recoverable = response.status_code in {429, 502, 503, 504}
                        if recoverable:
                            delay = retry_after(response.headers.get("retry-after"), self.wall_clock())
                            retry_delay = delay if delay is not None else float(2 ** retries_used)
                            self._defer(origin, retry_delay)
                            if retries_used < self.retries and retry_delay < deadline - self.clock():
                                break

                        # Error bodies are not useful to callers and may be arbitrarily large.
                        body = await self._read(response) if 200 <= response.status_code < 300 and response.status_code != 202 else b""
                        # _read returns decoded bytes; avoid decoding the new response twice.
                        result_headers = response.headers.copy()
                        for name in ("content-encoding", "content-length", "transfer-encoding"):
                            result_headers.pop(name, None)
                        result = httpx.Response(response.status_code, headers=result_headers,
                                                content=body, request=response.request)
                        result.extensions["resourcer"] = {
                            "requests": requests, "retries": retries_used,
                            "response_bytes": len(body),
                            "elapsed_seconds": round(self.clock() - started, 3),
                        }
                        if recoverable:
                            result.extensions["resourcer"]["retry_after_seconds"] = (
                                math.ceil(retry_delay) if math.isfinite(retry_delay) else None)
                        return result
                    finally:
                        await response.aclose()
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError):
                if retries_used >= self.retries:
                    raise
                retry_delay = float(2 ** retries_used)
            if retry_delay is None or retry_delay >= deadline - self.clock():
                raise HTTPPolicyError("deadline", "剩余时间不足以重试请求")
            retries_used += 1
            await self.sleep(retry_delay)

    async def _read(self, response):
        length = response.headers.get("content-length", "")
        if length.isascii() and length.isdigit() and float(length) > self.max_bytes:
            raise HTTPPolicyError("response_too_large", f"响应体超过 {self.max_bytes} 字节上限")
        # In-memory transports may already have decoded a response before send().
        if response.is_stream_consumed:
            if len(response.content) > self.max_bytes:
                raise HTTPPolicyError("response_too_large", f"响应体超过 {self.max_bytes} 字节上限")
            return response.content
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "", "gzip", "deflate"}:
            raise HTTPPolicyError("content_encoding", f"不支持响应压缩编码：{encoding}")
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding in {"gzip", "deflate"} else None
        body = bytearray()
        wire_bytes = 0
        first = True
        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
            wire_bytes += len(chunk)
            if wire_bytes > self.max_bytes:
                raise HTTPPolicyError("response_too_large", f"响应体超过 {self.max_bytes} 字节上限")
            pending = chunk
            while pending:
                if decoder is not None:
                    if decoder.eof:
                        if encoding != "gzip":
                            raise HTTPPolicyError("content_encoding", "压缩响应包含多余数据")
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    try:
                        part = decoder.decompress(pending, self.max_bytes - len(body) + 1)
                    except zlib.error as exc:
                        if first and encoding == "deflate":
                            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                            try:
                                part = decoder.decompress(pending, self.max_bytes - len(body) + 1)
                            except zlib.error as raw_exc:
                                raise HTTPPolicyError("content_encoding", "响应压缩数据无效") from raw_exc
                        else:
                            raise HTTPPolicyError("content_encoding", "响应压缩数据无效") from exc
                    pending = decoder.unused_data if decoder.eof else decoder.unconsumed_tail
                else:
                    part, pending = pending, b""
                first = False
                if len(body) + len(part) > self.max_bytes:
                    raise HTTPPolicyError("response_too_large", f"响应体超过 {self.max_bytes} 字节上限")
                body.extend(part)
        if decoder is not None and not decoder.eof:
            raise HTTPPolicyError("content_encoding", "压缩响应不完整")
        return bytes(body)
