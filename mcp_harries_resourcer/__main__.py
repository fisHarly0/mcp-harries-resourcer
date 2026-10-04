"""Console entry point. Normal startup reserves stdout for MCP messages."""
import argparse

from . import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="mcp-harries-resourcer",
        description="Run the Resourcer MCP server over stdio.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()
    # Help/version should work without importing or initializing network tools.
    from .server import mcp
    mcp.run()


if __name__ == "__main__":
    main()
