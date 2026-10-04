"""Bounded, cancellable article extraction in disposable Python processes."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from request_policy import _setting


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

    async def parse(self, html, url):
        started = asyncio.get_running_loop().time()
        try:
            result = await asyncio.wait_for(self._queued(html, url), self.timeout)
        except asyncio.TimeoutError as exc:
            raise ParseFailure("parse_deadline", "正文解析时间预算已用尽（含排队与进程启动）") from exc
        result["parse_info"] = {"mode": "subprocess", "elapsed_seconds": round(
            asyncio.get_running_loop().time() - started, 3)}
        return result

    async def _queued(self, html, url):
        async with self.slots:
            return await self._run(html, url)

    async def _run(self, html, url):
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
            payload = json.dumps({"html": html, "url": url}, ensure_ascii=False).encode("utf-8")

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
                        raise ParseFailure("parse_output_too_large", "正文解析结果超过传输上限")
                    chunks.append(chunk)

            io_tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            _, output = await asyncio.gather(*io_tasks)
            code = await proc.wait()
            if code != 0:
                raise ParseFailure("parse_worker_failed", "正文解析进程异常退出")
            try:
                result = json.loads(output)
                if not isinstance(result, dict) or not all(
                        isinstance(result.get(key), kind) for key, kind in (
                            ("ok", bool), ("text", str), ("_markdown", str), ("title", str))):
                    raise ValueError("invalid parser result")
            except (ValueError, UnicodeError) as exc:
                raise ParseFailure("parse_worker_failed", "正文解析进程返回无效结果") from exc
            return result
        except OSError as exc:
            raise ParseFailure("parse_worker_failed", "无法启动或读取正文解析进程") from exc
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
