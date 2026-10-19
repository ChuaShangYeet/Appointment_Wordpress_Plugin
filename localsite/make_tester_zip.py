"""Builds the zip to send to testers: the backend's code and this folder, without anything made on this computer.

    python make_tester_zip.py            writes JustSimple-local-test-site.zip next to the project folder

Left out: local data and secrets (data/), Python environments (.venv), caches, the backend's tests, logs.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKIP_DIRS = {".venv", "data", "__pycache__", ".mypy_cache", ".pytest_cache", "tests", ".git"}
SKIP_FILES = {".env", "data_run.log"}
TOP = "JustSimpleLocal"


def files(base: Path, rel: str):
    for path in sorted((base).rglob("*")):
        parts = path.relative_to(base).parts
        if any(p in SKIP_DIRS for p in parts[:-1]) or (path.is_dir()) or path.name in SKIP_FILES or path.suffix in (".pyc", ".log"):
            continue
        if "proj" in parts and path.suffix == ".tif":  # (PROJ's datum grids: not needed for distances, 60 MB, and long names that can break unzipping on Windows)
            continue
        yield path, f"{TOP}/{rel}/" + "/".join(parts)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / f"{TOP}.zip"
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base, rel in ((ROOT / "backend", "backend"), (HERE, "localsite")):
            for path, name in files(base, rel):
                z.write(path, name)
                count += 1
        z.writestr(f"{TOP}/START HERE.txt", (HERE / "README.md").read_text(encoding="utf8"))
    print(f"{out} ({count} files, {out.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
