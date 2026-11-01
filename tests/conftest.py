"""Shared fixtures: the demo shop served over HTTP, and throwaway git copies of it."""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
SHOP_DIR = ROOT / "demo" / "shopapp"
SHOP_FILES = ("index.html", "styles.css", "app.js", "cart.js", "cart.test.js", "package.json", ".gitignore")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1.0, trust_env=False).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="session")
def shop_url() -> Iterator[str]:
    """The demo shop on a free port (never 5173, so a developer's own server is left alone)."""
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                            cwd=SHOP_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        wait_for(url + "/")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def shop_repo(tmp_path: Path) -> Path:
    """A fresh git repo holding the demo shop, on branch main with one commit."""
    repo = tmp_path / "shop"
    repo.mkdir()
    for name in SHOP_FILES:
        shutil.copy2(SHOP_DIR / name, repo / name)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Nimbus Test")
    git(repo, "config", "user.email", "nimbus-test@localhost")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "shop")
    return repo
