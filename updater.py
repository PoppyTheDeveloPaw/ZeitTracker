"""Selbst-Update über GitHub-Releases – installiert wird nur nach Bestätigung durch den Nutzer.

Ablauf:
  1. check()       – fragt https://api.github.com/repos/<REPO>/releases/latest (ohne Anmeldung).
  2. download()    – lädt ZeitTracker-vX.Y.Z.exe neben die laufende EXE und prüft die SHA256-Prüfsumme.
  3. install()     – benennt die laufende EXE in ZeitTracker.old.exe um (unter Windows erlaubt),
                     legt die neue an ihre Stelle und startet sie.
  4. cleanup_old() – beim nächsten Start wird die alte EXE gelöscht.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

import requests

from version import __version__

REPO = "PoppyTheDeveloPaw/ZeitTracker"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ZeitTracker-Updater"}


class UpdateError(Exception):
    pass


def parse_version(v: str) -> Optional[tuple]:
    m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (v or "").strip())
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(remote: str, local: str = __version__) -> bool:
    r, l_ = parse_version(remote), parse_version(local)
    return bool(r and l_ and r > l_)


def is_frozen() -> bool:
    """Läuft als EXE (PyInstaller) – nur dann kann sich das Programm selbst austauschen."""
    return bool(getattr(sys, "frozen", False))


def check(session=None, timeout: int = 15) -> dict:
    """Liefert Infos zum neuesten Release: version, tag, notes, page, exe_url, sha_url, newer."""
    http = session or requests
    try:
        r = http.get(API_LATEST, headers=HEADERS, timeout=timeout)
    except requests.RequestException as e:
        raise UpdateError(f"GitHub nicht erreichbar: {e}") from e
    if r.status_code == 404:
        raise UpdateError("Auf GitHub gibt es noch kein Release.")
    if r.status_code >= 400:
        raise UpdateError(f"GitHub-Fehler {r.status_code}")
    rel = r.json()
    tag = rel.get("tag_name", "")
    exe = sha = None
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe"):
            exe = a.get("browser_download_url")
        elif name.lower().endswith(".exe.sha256"):
            sha = a.get("browser_download_url")
    return {"version": tag.lstrip("v"), "tag": tag, "notes": (rel.get("body") or "").strip(),
            "page": rel.get("html_url") or RELEASES_PAGE, "exe_url": exe, "sha_url": sha,
            "newer": is_newer(tag)}


def download(info: dict, target_dir: Path, session=None,
             progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Lädt die neue EXE herunter und prüft die Prüfsumme. Liefert den Pfad der neuen Datei."""
    if not info.get("exe_url") or not info.get("sha_url"):
        raise UpdateError("Das Release enthält keine EXE mit Prüfsumme.")
    http = session or requests
    try:
        sha_txt = http.get(info["sha_url"], headers=HEADERS, timeout=30).text
    except requests.RequestException as e:
        raise UpdateError(f"Prüfsumme konnte nicht geladen werden: {e}") from e
    m = re.search(r"\b([0-9a-fA-F]{64})\b", sha_txt or "")
    if not m:
        raise UpdateError("Die Prüfsummen-Datei ist ungültig.")
    expected = m.group(1).lower()

    target = Path(target_dir) / f"ZeitTracker.new-{info['version']}.exe"
    part = target.with_suffix(".part")
    h = hashlib.sha256()
    try:
        with http.get(info["exe_url"], headers=HEADERS, timeout=60, stream=True) as r:
            if r.status_code >= 400:
                raise UpdateError(f"Download fehlgeschlagen ({r.status_code}).")
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as fh:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        fh.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
    except requests.RequestException as e:
        _silent_unlink(part)
        raise UpdateError(f"Download fehlgeschlagen: {e}") from e
    except PermissionError as e:
        _silent_unlink(part)
        raise UpdateError(f"Keine Schreibrechte im Programmordner ({target_dir}). "
                          "Bitte die neue Version manuell von GitHub laden.") from e
    if h.hexdigest() != expected:
        _silent_unlink(part)
        raise UpdateError("Die Prüfsumme stimmt nicht – der Download wurde verworfen.")
    os.replace(part, target)
    return target


def install(new_exe: Path, current_exe: Optional[Path] = None, args: Optional[list] = None,
            launch: bool = True) -> Path:
    """Tauscht die laufende EXE gegen die neue aus und startet sie. Liefert den Pfad der alten EXE."""
    current = Path(current_exe or sys.executable)
    old = current.with_name(current.stem + ".old.exe")
    _silent_unlink(old)
    try:
        os.replace(current, old)          # laufende EXE darf umbenannt werden
        os.replace(new_exe, current)
    except OSError as e:
        if not current.exists() and old.exists():
            os.replace(old, current)       # zurückrollen
        raise UpdateError(f"Update konnte nicht installiert werden: {e}") from e
    if launch:
        flags = 0
        if sys.platform.startswith("win"):
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(current)] + (args or []), close_fds=True, creationflags=flags,
                         cwd=str(current.parent))
    return old


def cleanup_old(current_exe: Optional[Path] = None) -> None:
    """Reste eines früheren Updates entfernen (alte EXE, abgebrochene Downloads)."""
    if current_exe is None and not is_frozen():
        return
    current = Path(current_exe or sys.executable)
    for p in [current.with_name(current.stem + ".old.exe"),
              *current.parent.glob("ZeitTracker.new-*.part")]:
        _silent_unlink(p)


def _silent_unlink(p: Path) -> None:
    try:
        Path(p).unlink()
    except OSError:
        pass
