"""Make a git working copy of a Repro demo app (a folder under demo/) for Nimbus to fix.

The demo apps are plain folders in this repository, with their seeded bugs on disk. Repro needs a
repository of its own to branch and commit in, so this copies one into a fresh repo with a single
commit on `main`. The default destination, runs/demo/<app>, is gitignored and is where the app's
Repro settings point out of the box.

    .venv/bin/python tools/demo_repo.py shopapp            # create runs/demo/shopapp
    .venv/bin/python tools/demo_repo.py shopapp --reset    # start over after a fix
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = {p.name: p for p in sorted((ROOT / "demo").iterdir()) if p.is_dir()}
IGNORE = shutil.ignore_patterns("node_modules", ".DS_Store", ".git")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def seed(src: Path, dest: Path, author: str = "Nimbus Demo") -> Path:
    """A fresh repo at `dest` holding `src`'s files, on branch main with one commit and no remote."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=IGNORE)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "config", "user.name", author)
    git(dest, "config", "user.email", "nimbus-demo@localhost")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", f"{src.name} demo app")
    return dest


def digest(src: Path) -> str:
    """Content hash of a demo app's files, to check a run left the originals untouched."""
    h = hashlib.sha256()
    for path in sorted(p for p in src.rglob("*") if p.is_file() and "node_modules" not in p.parts):
        h.update(str(path.relative_to(src)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("app", choices=sorted(APPS))
    ap.add_argument("--dest", type=Path, help="where to put the working copy (default runs/demo/<app>)")
    ap.add_argument("--reset", action="store_true", help="replace an existing working copy and its branches")
    args = ap.parse_args()
    dest = (args.dest or ROOT / "runs" / "demo" / args.app).expanduser().resolve()
    if dest.exists() and not args.reset:
        print(f"{dest} already exists; pass --reset to replace it (its nimbus/repro-* branches go too)")
        return 1
    seed(APPS[args.app], dest)
    print(f"{args.app}: fresh repo on main at {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
