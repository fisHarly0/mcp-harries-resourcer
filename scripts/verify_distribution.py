"""Build sdist + wheel in a scratch copy, install into a clean venv, test real MCP.

Run with Python that has `build` installed. Pip installs runtime dependencies into
the isolated environment, so initial verification needs network access.
TEMP/TMP and PIP_CACHE_DIR control local scratch and download locations.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile


def run(*command, cwd, env):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", help="Optional uv executable to verify tool run from the scratch source copy")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="resourcer-distribution-") as tmp:
        root = Path(tmp).resolve()
        source = root / "source"
        source.mkdir()
        # Explicit source allowlist: never package local notes, env files or caches.
        for name in ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "CHANGELOG.md", "requirements.txt", "requirements-dev.txt", "server.py"):
            shutil.copy2(repo / name, source / name)
        for name in ("mcp_harries_resourcer", "docs", "scripts", "tests"):
            shutil.copytree(repo / name, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        artifacts = root / "artifacts"
        run(sys.executable, "-m", "build", "--outdir", str(artifacts), cwd=source, env=env)
        wheel, = artifacts.glob("*.whl")
        sdist, = artifacts.glob("*.tar.gz")
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert "mcp_harries_resourcer/parse_worker.py" in names, names
            assert all(name.startswith(("mcp_harries_resourcer/", "mcp_harries_resourcer-")) for name in names), names
        with tarfile.open(sdist) as archive:
            names = archive.getnames()
            for expected in ("requirements.txt", "requirements-dev.txt", "LICENSE", "server.py", "mcp_harries_resourcer/parse_worker.py",
                             "scripts/search_cases.json", "docs/evidence/search-reference-2026-10-04.json"):
                assert any(name.endswith("/" + expected) for name in names), expected
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        binaries = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binaries / ("python.exe" if os.name == "nt" else "python")
        cli = binaries / ("mcp-harries-resourcer.exe" if os.name == "nt" else "mcp-harries-resourcer")
        run(str(python), "-m", "pip", "install", str(wheel), cwd=root, env=env)
        run(str(python), "-m", "pip", "check", cwd=root, env=env)
        run(str(python), "-I", "-c", "import mcp_harries_resourcer as p; import sys; from pathlib import Path; "
            "from importlib.metadata import version; assert version('mcp-harries-resourcer') == p.__version__; "
            "assert Path(p.__file__).is_relative_to(Path(sys.prefix)), p.__file__", cwd=root, env=env)
        smoke = source / "scripts" / "installed_smoke.py"
        run(str(python), str(smoke), "--", str(cli), cwd=root, env=env)
        run(str(python), str(smoke), "--", str(python), "-m", "mcp_harries_resourcer", cwd=root, env=env)
        if args.uv:
            run(str(python), str(smoke), "--", args.uv, "tool", "run", "--python", str(python),
                "--from", str(source), "mcp-harries-resourcer", cwd=root, env=env)
        print("Distribution verification passed: sdist, wheel, isolated install, CLI and module MCP.")


if __name__ == "__main__":
    main()
