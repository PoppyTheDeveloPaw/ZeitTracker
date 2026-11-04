"""Erzeugt die Windows-Versionsinfo für PyInstaller (--version-file) aus version.py.

Damit steht die Versionsnummer in der EXE (Eigenschaften › Details) statt im Dateinamen –
die Datei heißt immer ZeitTracker.exe, Autostart-Eintrag und Verknüpfungen bleiben gültig.
Aufruf: python make_version_info.py [Zieldatei]   (Standard: version_info.txt)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from version import __version__


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """'1.2.0' -> (1, 2, 0, 0); 'dev' oder Unlesbares -> (0, 0, 0, 0)."""
    nums = [int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0])][:4]
    return tuple(nums + [0] * (4 - len(nums)))  # type: ignore[return-value]


def render(version: str) -> str:
    t = version_tuple(version)
    return f"""# Automatisch erzeugt von make_version_info.py – nicht von Hand ändern
VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040704B0', [
      StringStruct('ProductName', 'ZeitTracker'),
      StringStruct('FileDescription', 'ZeitTracker – Arbeitszeiterfassung'),
      StringStruct('InternalName', 'ZeitTracker'),
      StringStruct('OriginalFilename', 'ZeitTracker.exe'),
      StringStruct('FileVersion', {version!r}),
      StringStruct('ProductVersion', {version!r})])]),
    VarFileInfo([VarStruct('Translation', [0x0407, 1200])])
  ]
)
"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "version_info.txt")
    out.write_text(render(__version__), encoding="utf-8")
    print(f"{out}: Version {__version__}")
