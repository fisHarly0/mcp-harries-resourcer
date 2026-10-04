"""Controlled child process for cancellation, limits and protocol tests."""
import json
import base64
import os
from pathlib import Path
import sys
import time

mode, marker = sys.argv[1:3]
request = json.loads(sys.stdin.buffer.read())
if request.get("html_base64"):
    request["html"] = base64.b64decode(request["html"]).decode("utf-8")
if mode not in {"search_gate", "search_busy"} or request.get("engine") == "ddg":
    Path(marker).write_text(str(os.getpid()), encoding="utf-8")
if mode == "busy" or (mode == "search_busy" and request.get("engine") == "ddg"):
    n = 0
    while True:
        n = (n + 1) % 1000000
if mode == "gate" or (mode == "search_gate" and request.get("engine") == "ddg"):
    while not Path(marker + ".release").exists():
        time.sleep(0.01)
if mode == "crash":
    sys.exit(7)
if mode == "invalid":
    print("invalid JSON")
elif mode == "oversize":
    sys.stdout.buffer.write(b"x" * 200000)
elif mode == "json":
    print(request["html"])
elif request.get("kind") == "search":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from mcp_harries_resourcer.search_content import parse_search
    print(json.dumps({"ok": True, "items": parse_search(
        request["html"], request["engine"], request["max_results"]), "error": ""}))
else:
    print(json.dumps({"ok": True, "title": "Fixture", "text": request["html"], "_markdown": request["html"]}))
