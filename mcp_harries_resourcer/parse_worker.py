"""Private one-request worker; stdout carries only UTF-8 JSON, never MCP."""
import contextlib
import base64
import json
from pathlib import Path
import sys

# ParsePolicy executes this file directly so source checkouts and installed wheels
# work from any cwd. Limit the bootstrap to this package's actual parent directory.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "mcp_harries_resourcer"


def main():
    with contextlib.redirect_stdout(sys.stderr):
        request = json.loads(sys.stdin.buffer.read())
        encoding_info = None
        if request.get("html_base64"):
            from .html_encoding import decode_html
            request["html"], encoding_info = decode_html(
                base64.b64decode(request["html"], validate=True), request.get("content_type", ""))
        if request.get("kind") == "search":
            from .search_content import SearchEngineError, parse_search
            try:
                items = parse_search(request["html"], request["engine"], request["max_results"])
                result = {"ok": True, "items": items, "error": ""}
            except SearchEngineError as exc:
                result = {"ok": False, "items": [], "error": str(exc)}
        else:
            from .page_content import extract_page
            result = extract_page(request["html"], request["url"])
        if encoding_info is not None:
            result["encoding_info"] = encoding_info
        output = json.dumps(result, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
