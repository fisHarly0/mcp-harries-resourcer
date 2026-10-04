"""Bounded, cancellable HTML extraction in disposable Python processes."""
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

from .request_policy import _setting
from .task_cleanup import wait_for_owned


class ParseFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ParsePolicy:
    def __init__(self, concurrency=2, timeout=20, *, command=None, max_output_bytes=32 * 1024 * 1024):
        self.slots = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self.command = command or [sys.executable, str(Path(__file__).with_name("parse_worker.py"))]
        self.max_output_bytes = max_output_bytes
        self.active = set()

    @classmethod
    def from_env(cls):
        return cls(concurrency=_setting("RESOURCER_PARSE_CONCURRENCY", 2, 5, 1),
                   timeout=_setting("RESOURCER_PARSE_TIMEOUT_SECONDS", 20, 120, 1))

    async def parse(self, html, url, *, content_type=""):
        return await self._parse({"html": html, "url": url, "content_type": content_type})

    async def parse_search(self, html, engine, max_results, *, content_type=""):
        if engine not in {"ddg", "bing"} or type(max_results) is not int or not 1 <= max_results <= 50:
            raise ValueError("invalid search parser request")
        return await self._parse({"kind": "search", "html": html, "engine": engine,
                                  "max_results": max_results, "content_type": content_type})

    async def _parse(self, request):
        started = asyncio.get_running_loop().time()
        if isinstance(request["html"], bytes):
            request = {**request, "html": base64.b64encode(request["html"]).decode("ascii"), "html_base64": True}
        try:
            result = await wait_for_owned(self._queued(request), self.timeout)
        except asyncio.TimeoutError as exc:
            raise ParseFailure("parse_deadline", "HTML 解析时间预算已用尽（含排队与进程启动）") from exc
        result["parse_info"] = {"mode": "subprocess", "elapsed_seconds": round(
            asyncio.get_running_loop().time() - started, 3)}
        return result

    async def _queued(self, request):
        async with self.slots:
            return await self._run(request)

    @staticmethod
    def _valid_result(result, request):
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            return False
        info = result.get("encoding_info")
        if info is not None and (not isinstance(info, dict) or
                not isinstance(info.get("encoding"), str) or
                info.get("source") not in ("bom", "http", "meta", "default") or
                type(info.get("had_errors")) is not bool):
            return False
        if request.get("kind") == "search":
            items = result.get("items")
            if (not isinstance(items, list) or len(items) > request["max_results"] or
                    not isinstance(result.get("error"), str)):
                return False
            if not result["ok"]:
                return not items and bool(result["error"])
            return not result["error"] and all(
                isinstance(item, dict) and all(isinstance(item.get(key), str)
                    for key in ("url", "title", "snippet", "source")) and
                item["source"] == request["engine"] for item in items)
        return all(isinstance(result.get(key), str) for key in ("text", "_markdown", "title"))

    async def _run(self, request):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        command = list(self.command)
        if os.name == "nt" and command[0] == sys.executable and sys.executable != sys._base_executable:
            # Match multiprocessing's Windows venv launch convention: own the
            # actual interpreter, not the intermediate venv redirector process.
            command[0] = sys._base_executable
            options["env"] = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, **options))
        io_tasks = []

        async def cleanup():
            # Even cancellation during creation must acquire and reap the child.
            try:
                proc = await spawn
            except Exception:
                return
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            for task in io_tasks:
                task.cancel()
            await asyncio.gather(*io_tasks, return_exceptions=True)
            # Drain after killing to avoid a full stdout pipe blocking wait().
            try:
                await proc.communicate()
            finally:
                self.active.discard(proc)

        try:
            proc = await asyncio.shield(spawn)
            self.active.add(proc)
            payload = json.dumps(request, ensure_ascii=False).encode("utf-8")

            async def send():
                try:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            async def receive():
                chunks, size = [], 0
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        return b"".join(chunks)
                    size += len(chunk)
                    if size > self.max_output_bytes:
                        raise ParseFailure("parse_output_too_large", "HTML 解析结果超过传输上限")
                    chunks.append(chunk)

            io_tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            _, output = await asyncio.gather(*io_tasks)
            code = await proc.wait()
            if code != 0:
                raise ParseFailure("parse_worker_failed", "HTML 解析进程异常退出")
            try:
                result = json.loads(output)
                if not self._valid_result(result, request):
                    raise ValueError("invalid parser result")
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ParseFailure("parse_worker_failed", "HTML 解析进程返回无效结果") from exc
            return result
        except OSError as exc:
            raise ParseFailure("parse_worker_failed", "无法启动或读取 HTML 解析进程") from exc
        finally:
            finished = asyncio.create_task(cleanup())
            cancelled = False
            while not finished.done():
                try:
                    await asyncio.shield(finished)
                except asyncio.CancelledError:
                    cancelled = True
            finished.result()
            if cancelled:
                raise asyncio.CancelledError()
