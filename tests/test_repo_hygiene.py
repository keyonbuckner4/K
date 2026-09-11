"""Guards against source files being silently excluded from the repository by .gitignore."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_no_source_file_is_gitignored():
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        pytest.skip("not a git checkout")
    files = [p for d in ("src", "tests", "config") for p in (ROOT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    rel = [str(p.relative_to(ROOT)) for p in files]
    r = _git("check-ignore", "--no-index", *rel)
    ignored = [line for line in r.stdout.splitlines() if line.strip()]
    assert ignored == [], f"these source files are ignored by .gitignore and would be missing from a clone: {ignored}"


def test_every_package_directory_has_init():
    src = ROOT / "src" / "kalshi_bot"
    for d in [p for p in src.rglob("*") if p.is_dir() and "__pycache__" not in p.parts]:
        assert (d / "__init__.py").exists(), f"{d} has no __init__.py"
