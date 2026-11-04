"""Windows-spezifische Helfer (Autostart, Einzelinstanz, DPI). Auf anderen Systemen No-Ops."""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ZeitTracker"
_mutex = None


def launch_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller-EXE
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    script = Path(__file__).resolve().parent / "zeittracker.pyw"
    return f'"{exe}" "{script}"'


def set_autostart(enabled: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass


def single_instance() -> bool:
    """True, wenn dies die einzige laufende Instanz ist."""
    global _mutex
    if not IS_WINDOWS:
        return True
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\ZeitTrackerSingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def open_folder(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- DPAPI (Windows-Verschlüsselung)

def protect(data: bytes) -> bytes:
    """Daten an das Windows-Benutzerkonto gebunden verschlüsseln (DPAPI). Andere Systeme: unverändert."""
    if not IS_WINDOWS:
        return data
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), "ZeitTracker", None, None, None,
                                                  0x1, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData fehlgeschlagen")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def unprotect(data: bytes) -> bytes:
    if not IS_WINDOWS:
        return data
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                                    0x1, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData fehlgeschlagen")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
