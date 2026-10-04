import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest

import httpx

from mcp_harries_resourcer.client_pool import ClientPool


class ClientPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.created = []
        def factory():
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                                       trust_env=False, verify=False)
            self.created.append(client)
            return client
        self.pool = ClientPool(factory, max_idle=2, idle_seconds=60)

    async def test_reuse_is_exclusive_and_cookies_are_cleared(self):
        async with self.pool:
            async with self.pool.lease() as first:
                first.cookies.set("session", "one")
                async with self.pool.lease() as second:
                    self.assertIsNot(first, second)
                    self.assertFalse(second.cookies)
            async with self.pool.lease() as reused:
                self.assertIs(reused, first)
                self.assertFalse(reused.cookies)
                self.assertFalse(reused.is_closed)
        self.assertTrue(all(c.is_closed for c in self.created))
        self.assertFalse(self.pool.idle)
        self.assertFalse(self.pool.active)

    async def test_standalone_calls_close_without_retaining_idle_clients(self):
        async with self.pool.lease() as first:
            self.assertFalse(first.is_closed)
        self.assertTrue(first.is_closed)
        self.assertFalse(self.pool.idle)
        async with self.pool.lease() as second:
            self.assertIsNot(first, second)

    async def test_idle_bound_evicts_oldest_client(self):
        async with self.pool:
            async with self.pool.lease() as first, self.pool.lease() as second, self.pool.lease() as third:
                self.assertEqual(len(self.pool.active), 3)
            self.assertEqual(len(self.pool.idle), 2)
            self.assertNotIn(third, self.pool.idle)  # First returned, first evicted.
            await asyncio.gather(*list(self.pool.closing.values()))
            self.assertTrue(third.is_closed)
            self.assertFalse(first.is_closed or second.is_closed)

    async def test_idle_expiry_closes_without_another_request(self):
        self.pool.idle_seconds = 0.02
        async with self.pool:
            async with self.pool.lease() as client:
                pass
            async def expired():
                while not client.is_closed:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(expired(), 2)
            self.assertFalse(self.pool.idle)

    async def test_borrow_cancels_expiry_timer(self):
        self.pool.idle_seconds = 0.03
        async with self.pool:
            async with self.pool.lease() as original:
                pass
            async with self.pool.lease() as borrowed:
                await asyncio.sleep(0.05)
                self.assertIs(borrowed, original)
                self.assertFalse(borrowed.is_closed)

    async def test_cancelled_lease_does_not_close_another_call(self):
        entered = asyncio.Event()
        async def cancelled():
            async with self.pool.lease() as first:
                first.cookies.set("session", "cancelled")
                entered.set()
                await asyncio.Event().wait()
        async with self.pool:
            task = asyncio.create_task(cancelled())
            await entered.wait()
            async with self.pool.lease() as other:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(other.is_closed)
                async with self.pool.lease() as reused:
                    self.assertFalse(reused.cookies)
                    self.assertIsNot(reused, other)

    async def test_shutdown_waits_for_close_despite_repeated_cancel(self):
        started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowClient:
            cookies = httpx.Cookies()
            is_closed = False
            async def aclose(self):
                started.set()
                await release.wait()
                self.is_closed = True
                closed.set()
        pool = ClientPool(SlowClient)
        async def lifecycle():
            async with pool:
                async with pool.lease():
                    pass
        task = asyncio.create_task(lifecycle())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        self.assertFalse(closed.is_set())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())

    async def test_real_http_connection_reused_without_cross_lease_cookies(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_GET(self):
                requests.append((self.client_address[1], self.headers.get("Cookie")))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                if self.path == "/set":
                    self.send_header("Set-Cookie", "session=one; Path=/")
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *args):
                pass
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        pool = ClientPool(lambda: httpx.AsyncClient(trust_env=False, verify=False))
        url = f"http://127.0.0.1:{http.server_port}"
        async with pool:
            async with pool.lease() as client:
                await client.get(url + "/set")
                await client.get(url + "/echo")
            async with pool.lease() as client:
                await client.get(url + "/next")
        self.assertEqual(len({port for port, _ in requests}), 1)
        self.assertEqual([cookie for _, cookie in requests], [None, "session=one", None])
