import asyncio
from email.utils import formatdate
import gzip
import os
import unittest
from unittest.mock import patch
import zlib

import httpx

from mcp_harries_resourcer.http_policy import HTTPPolicy, HTTPPolicyError, retry_after
from mcp_harries_resourcer.request_policy import RequestPolicy
from mcp_harries_resourcer import server


class Clock:
    def __init__(self):
        self.now = 100.0
        self.delays = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        self.delays.append(delay)
        await asyncio.sleep(0)


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class RetryHeaderTests(unittest.TestCase):
    def test_seconds_dates_and_invalid_headers(self):
        now = 1700000000
        self.assertEqual(retry_after(" 120 ", now), 120)
        self.assertEqual(retry_after(formatdate(now + 15, usegmt=True), now), 15)
        self.assertEqual(retry_after(formatdate(now - 10, usegmt=True), now), 0)
        for value in [None, "", "-3", "1.5", "NaN", "garbage", "１２"]:
            with self.subTest(value=value):
                self.assertIsNone(retry_after(value, now))

    def test_configuration_cannot_disable_size_or_deadline(self):
        with patch.dict(os.environ, {"RESOURCER_RESPONSE_MAX_BYTES": "0", "RESOURCER_REQUEST_TIMEOUT_SECONDS": "0",
                                     "RESOURCER_HTTP_RETRIES": "0"}):
            with self.assertLogs("mcp_harries_resourcer.request_policy", level="WARNING"):
                policy = RequestPolicy.from_env()
        self.assertEqual(policy.http.max_bytes, 8 * 1024 * 1024)
        self.assertEqual(policy.http.total_timeout, 60)
        self.assertEqual(policy.http.retries, 0)


class HTTPPolicyTests(unittest.IsolatedAsyncioTestCase):
    def policy(self, **kwargs):
        self.clock = Clock()
        return HTTPPolicy(clock=self.clock, sleep=self.clock.sleep, **kwargs)

    async def test_retry_after_then_success_and_close_before_retry(self):
        policy = self.policy()
        rejected = Stream([b"ignored error body"])
        starts = []
        def handler(request):
            starts.append(self.clock())
            if len(starts) == 1:
                return httpx.Response(429, headers={"Retry-After": "3"}, stream=rejected)
            self.assertTrue(rejected.closed)
            self.assertEqual(rejected.reads, 0)
            return httpx.Response(200, text="Recovered")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await policy.request(client, "GET", "https://example.org")
        self.assertEqual(starts, [100, 103])
        self.assertEqual(result.text, "Recovered")
        self.assertEqual(result.extensions["resourcer"]["retries"], 1)

    async def test_date_header_honored(self):
        policy = self.policy(wall_clock=lambda: 1700000000)
        calls = []
        def handler(request):
            calls.append(self.clock())
            return httpx.Response(503 if len(calls) == 1 else 200,
                                  headers={"Retry-After": formatdate(1700000004, usegmt=True)})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await policy.request(client, "GET", "https://example.org")
        self.assertEqual(calls, [100, 104])

    async def test_long_delay_not_shortened_or_retried_by_next_call(self):
        policy = self.policy(total_timeout=10)
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(429, headers={"Retry-After": "120"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await policy.request(client, "GET", "https://example.org/a")
            self.assertEqual(result.status_code, 429)
            self.assertEqual(result.extensions["resourcer"]["retry_after_seconds"], 120)
            with self.assertRaises(HTTPPolicyError) as raised:
                await policy.request(client, "GET", "https://example.org/b")
            self.assertEqual(raised.exception.code, "cooldown")
            await policy.request(client, "GET", "https://other.org/a")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.clock.delays, [])

    async def test_cooldown_expiry_and_bounded_origin_state(self):
        policy = self.policy(total_timeout=10)
        for index in range(257):
            policy._defer(("https", f"host{index}.example", None), 20)
        self.assertEqual(len(policy.cooldowns), 256)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
            with self.assertRaises(HTTPPolicyError):
                await policy.request(client, "GET", "https://other.example")
            self.clock.now += 21
            result = await policy.request(client, "GET", "https://other.example")
        self.assertEqual(result.status_code, 200)
        policy._defer(("https", "new.example", None), 5)
        self.assertEqual(len(policy.cooldowns), 1)

    async def test_retry_limit_and_each_attempt_is_paced(self):
        policy = self.policy(retries=2)
        starts, paced = [], []
        async def pace():
            paced.append(self.clock())
        def handler(request):
            starts.append(self.clock())
            return httpx.Response(502)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await policy.request(client, "GET", "https://example.org", pace=pace)
        self.assertEqual(starts, [100, 101, 103])
        self.assertEqual(paced, starts)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.extensions["resourcer"]["requests"], 3)

    async def test_connect_and_read_timeout_recovery(self):
        for failure in [httpx.ConnectError("offline"), httpx.ReadTimeout("slow")]:
            calls = []
            def handler(request):
                calls.append(request)
                if len(calls) == 1:
                    raise failure
                return httpx.Response(200, text="Recovered")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result = await self.policy().request(client, "GET", "https://example.org")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(calls), 2)

    async def test_permanent_errors_and_captcha_are_not_retried(self):
        for status in [202, 400, 401, 403, 404, 500]:
            stream = Stream([b"not needed"])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, stream=stream))) as client:
                result = await self.policy().request(client, "GET", "https://example.org")
            self.assertEqual(result.extensions["resourcer"]["requests"], 1)
            self.assertTrue(stream.closed)
            self.assertEqual(stream.reads, 0)

    async def test_redirect_body_skipped_and_each_hop_paced(self):
        stream = Stream([b"x" * 100000])
        calls, paced = [], []
        async def pace():
            paced.append(True)
        def handler(request):
            calls.append((request.method, str(request.url)))
            if len(calls) == 1:
                return httpx.Response(302, headers={"Location": "/final"}, stream=stream)
            return httpx.Response(200, text="ok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            result = await self.policy(max_bytes=10).request(client, "POST", "https://example.org/old", pace=pace)
        self.assertEqual(calls, [("POST", "https://example.org/old"), ("GET", "https://example.org/final")])
        self.assertEqual(str(result.url), "https://example.org/final")
        self.assertEqual(len(paced), 2)
        self.assertEqual(stream.reads, 0)
        self.assertTrue(stream.closed)

    async def test_redirect_limit_closes_all_responses(self):
        streams = []
        def handler(request):
            stream = Stream([b"unused"])
            streams.append(stream)
            return httpx.Response(302, headers={"Location": "/loop"}, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(HTTPPolicyError) as raised:
                await self.policy(max_redirects=2).request(client, "GET", "https://example.org")
        self.assertEqual(raised.exception.code, "redirect_limit")
        self.assertEqual(len(streams), 3)
        self.assertTrue(all(s.closed and not s.reads for s in streams))

    async def test_content_length_rejects_without_reading(self):
        stream = Stream([b"oversize"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, headers={"Content-Length": "1000000"}, stream=stream))) as client:
            with self.assertRaises(HTTPPolicyError) as raised:
                await self.policy(max_bytes=100).request(client, "GET", "https://example.org")
        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertTrue(stream.closed)
        self.assertEqual(stream.reads, 0)

    async def test_stream_limit_stops_download_without_content_length(self):
        stream = Stream([b"a" * 65536] * 4)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as client:
            with self.assertRaises(HTTPPolicyError):
                await self.policy(max_bytes=65540).request(client, "GET", "https://example.org")
        self.assertEqual(stream.reads, 2)
        self.assertTrue(stream.closed)

    async def test_compressed_body_expansion_is_bounded(self):
        wire = gzip.compress(b"a" * 1000000)
        stream = Stream([wire])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(wire))}, stream=stream))) as client:
            with self.assertRaises(HTTPPolicyError) as raised:
                await self.policy(max_bytes=2000).request(client, "GET", "https://example.org")
        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertTrue(stream.closed)

    async def test_valid_gzip_multimember_and_deflate_decode_once(self):
        body = "Readable 中文".encode()
        cases = [("gzip", gzip.compress(body)), ("gzip", gzip.compress(body[:5]) + gzip.compress(body[5:])),
                 ("deflate", zlib.compress(body)), ("deflate", zlib.compress(body)[2:-4])]
        for encoding, wire in cases:
            stream = Stream([wire[:3], wire[3:]])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                    200, headers={"Content-Encoding": encoding, "Content-Type": "text/html; charset=utf-8"}, stream=stream))) as client:
                result = await self.policy().request(client, "GET", "https://example.org")
            self.assertEqual(result.content, body)
            self.assertEqual(result.text, body.decode())

    async def test_corrupt_truncated_and_unsupported_encoding_fail(self):
        for encoding, body in [("gzip", b"corrupt"), ("gzip", gzip.compress(b"text")[:-3]),
                               ("br", b"unsupported"), ("deflate", b"corrupt")]:
            stream = Stream([body])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                    200, headers={"Content-Encoding": encoding}, stream=stream))) as client:
                with self.assertRaises(HTTPPolicyError) as raised:
                    await self.policy().request(client, "GET", "https://example.org")
            self.assertEqual(raised.exception.code, "content_encoding")
            self.assertTrue(stream.closed)

    async def test_slow_stream_total_deadline_closes_response(self):
        entered = asyncio.Event()
        class Slow(Stream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"never"
        stream = Slow([])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as client:
            with self.assertRaises(HTTPPolicyError) as raised:
                await HTTPPolicy(total_timeout=0.05).request(client, "GET", "https://example.org")
        self.assertTrue(entered.is_set())
        self.assertEqual(raised.exception.code, "deadline")
        self.assertTrue(stream.closed)

    async def test_cancellation_is_not_retried_and_closes_response(self):
        entered = asyncio.Event()
        class Slow(Stream):
            async def __aiter__(self):
                entered.set()
                await asyncio.Event().wait()
                yield b"never"
        stream = Slow([])
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(HTTPPolicy().request(client, "GET", "https://example.org"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(stream.closed)
        self.assertEqual(len(calls), 1)


class FetchBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_deadline_cleans_flight_and_does_not_cache_error(self):
        policy = RequestPolicy(fetch_rpm=0, fetch_concurrency=0, http_policy=HTTPPolicy(total_timeout=0.02))
        with patch.object(server, "NETWORK", policy):
            async with httpx.AsyncClient() as client:
                result = await server._fetch_one(client, "https://example.org", 100)
        self.assertEqual(result["error_code"], "deadline")
        self.assertFalse(result["ok"])
        self.assertEqual(policy.inflight, {})
        self.assertEqual(len(policy.cache.entries), 0)

    async def test_oversize_is_reported_without_extracting_or_caching_partial_body(self):
        policy = RequestPolicy(fetch_rpm=0, http_policy=HTTPPolicy(max_bytes=10))
        with patch.object(server, "NETWORK", policy), patch.object(server.PARSER, "parse") as extract:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="a" * 20))) as client:
                result = await server._fetch_one(client, "https://example.org", 5)
        self.assertEqual(result["error_code"], "response_too_large")
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(len(policy.cache.entries), 0)
        extract.assert_not_called()
