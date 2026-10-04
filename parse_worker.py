"""Private one-request worker; stdout carries only UTF-8 JSON, never MCP."""
import contextlib
import json
import sys


def main():
    with contextlib.redirect_stdout(sys.stderr):
        from page_content import extract_page
        request = json.loads(sys.stdin.buffer.read())
        result = extract_page(request["html"], request["url"])
        output = json.dumps(result, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
