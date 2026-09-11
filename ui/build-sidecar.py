"""Build the Python bot into a single-file executable and place it where Tauri
expects a sidecar: ui/src-tauri/binaries/rxdltc-mm-<target-triple>[.exe].

Usage (from the repo root, inside the bot's venv with pyinstaller installed):
    python ui/build-sidecar.py

Tauri's `bundle.externalBin` entry "binaries/rxdltc-mm" resolves to this file
at build time and ships it next to the app executable as rxdltc-mm(.exe).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "ui" / "src-tauri" / "binaries"


def host_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("cannot determine rust host triple (is rustc installed?)")


def main() -> None:
    triple = host_triple()
    ext = ".exe" if sys.platform == "win32" else ""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "sidecar"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconfirm", "--clean",
        "--name", "rxdltc-mm",
        "--distpath", str(work / "dist"),
        "--workpath", str(work / "work"),
        "--specpath", str(work),
        "--paths", str(ROOT / "src"),
        "--collect-all", "pydantic",
        "--hidden-import", "rxdltc_mm.cli",
        str(ROOT / "src" / "rxdltc_mm" / "__main__.py"),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)
    built = work / "dist" / f"rxdltc-mm{ext}"
    target = OUT_DIR / f"rxdltc-mm-{triple}{ext}"
    shutil.copy2(built, target)
    print(f"sidecar ready: {target} ({os.path.getsize(target) // 1_000_000} MB)")
    check = subprocess.run([str(target), "--check-config", "--config", str(ROOT / "config.example.yaml"), "--env-file", ""],
                           capture_output=True, text=True)
    print(check.stdout.strip() or check.stderr.strip())
    if check.returncode != 0:
        raise SystemExit("sidecar smoke test failed")


if __name__ == "__main__":
    main()
