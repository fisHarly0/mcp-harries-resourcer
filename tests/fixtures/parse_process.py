"""Controlled child process for cancellation, limits and protocol tests."""
import json
import os
from pathlib import Path
import sys
import time

mode, marker = sys.argv[1:3]
request = json.loads(sys.stdin.buffer.read())
Path(marker).write_text(str(os.getpid()), encoding="utf-8")
if mode == "busy":
    n = 0
    while True:
        n = (n + 1) % 1000000
if mode == "gate":
    while not Path(marker + ".release").exists():
        time.sleep(0.01)
if mode == "crash":
    sys.exit(7)
if mode == "invalid":
    print("invalid JSON")
elif mode == "oversize":
    sys.stdout.buffer.write(b"x" * 200000)
else:
    print(json.dumps({"ok": True, "title": "Fixture", "text": request["html"], "_markdown": request["html"]}))
