"""Erzeugt die eigenständige Cookie-Bereinigung.exe (benötigt PyInstaller, nur zum Bauen)."""

import shutil
import subprocess
import sys
from pathlib import Path

ordner = Path(__file__).resolve().parent
zwischen = ordner / "build"
subprocess.run(
    [sys.executable, "-m", "PyInstaller", "--onefile", "--windowed", "--noconfirm", "--clean",
     "--name", "Cookie-Bereinigung", "--distpath", str(zwischen / "ergebnis"),
     "--workpath", str(zwischen / "arbeit"), "--specpath", str(zwischen),
     str(ordner / "cookie_bereinigung.pyw")],
    check=True,
)
(zwischen / "ergebnis" / "Cookie-Bereinigung.exe").replace(ordner / "Cookie-Bereinigung.exe")
shutil.rmtree(zwischen)
