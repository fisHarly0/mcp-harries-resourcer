"""Exercise an installed command (or a source launcher) from an unrelated cwd.

Example: python scripts/installed_smoke.py -- mcp-harries-resourcer
The command after -- is passed directly to the MCP subprocess, without a shell.
"""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke(command):
    source = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "technical_article.html").read_text(encoding="utf-8")
    body = source.replace('charset="utf-8"', 'charset="gb18030"').encode("gb18030")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="resourcer-installed-") as tmp:
            # Old flat module names in the caller's cwd must not shadow our modules.
            for name in ("server", "page_content", "request_policy", "parse_worker", "local_search"):
                (Path(tmp) / f"{name}.py").write_text("raise RuntimeError('caller cwd module imported')\n", encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
            env.update(RESOURCER_RESEARCH_ROOT=str(Path(tmp) / "notes"), RESOURCER_FETCH_RPM="0",
                       NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
            version = await asyncio.to_thread(subprocess.run, [*command, "--version"],
                                             cwd=tmp, env=env, capture_output=True, text=True, check=True, timeout=180)
            assert re.fullmatch(r"mcp-harries-resourcer \d+\.\d+\.\d+", version.stdout.strip()), version.stdout
            params = StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=tmp)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert names == {"web_search", "web_search_multi", "search_chinese", "fetch_page",
                                     "fetch_pages", "deep_research", "search_local", "save_finding"}, names
                    async def call(name, arguments):
                        result = await session.call_tool(name, arguments)
                        assert not result.isError, result
                        return "\n".join(block.text for block in result.content if block.type == "text")
                    url = f"http://127.0.0.1:{http.server_port}/article"
                    page = json.loads(await call("fetch_page", {"url": url, "content_format": "markdown", "response_format": "json"}))
                    assert page["ok"], page
                    assert page["parse_info"]["mode"] == "subprocess", page
                    assert "```python" in page["text"], page
                    assert '\"甲\", \"乙\"' in page["text"], page
                    assert page["encoding_info"] == {"encoding": "gb18030", "source": "meta", "had_errors": False}, page
                    assert page["structure"]["tables"] == 1, page
                    await call("save_finding", {"collection": "installed", "title": "Wheel note",
                                               "content": "Installed workflow verified", "source_url": url, "tags": "package,MCP"})
                    found = json.loads(await call("search_local", {"query": "workflow verified", "root": env["RESOURCER_RESEARCH_ROOT"],
                                                                 "tags": "MCP", "response_format": "json"}))
                    assert found["complete"] and len(found["matches"]) == 1, found
                    assert found["matches"][0]["metadata"]["collection"] == "installed", found
            return {"version": version.stdout.strip(), "tools": len(names), "foreign_cwd": True,
                    "parse_subprocess": True, "declared_encoding": True, "markdown_code_and_table": True, "save_and_search": True}
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command after --")
    print(json.dumps(asyncio.run(asyncio.wait_for(smoke(command), 240)), indent=2))


if __name__ == "__main__":
    main()
