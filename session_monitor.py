"""Überwacht Windows-Sitzungsereignisse: Bildschirm sperren/entsperren und Herunterfahren/Abmelden.

Dafür wird ein unsichtbares Win32-Fenster in einem eigenen Thread angelegt, das
  * WM_WTSSESSION_CHANGE (Sperren = 7, Entsperren = 8) und
  * WM_QUERYENDSESSION / WM_ENDSESSION (Herunterfahren, Neustart, Abmelden)
empfängt. Auf anderen Betriebssystemen macht das Modul nichts.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable

log = logging.getLogger("ZeitTracker")

WM_DESTROY = 0x0002
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0


class SessionMonitor:
    def __init__(self, on_lock: Callable[[], None], on_unlock: Callable[[], None],
                 on_shutdown: Callable[[], None]):
        self.on_lock = on_lock
        self.on_unlock = on_unlock
        self.on_shutdown = on_shutdown
        self._shutdown_done = False
        self._thread = None

    def start(self) -> None:
        if not sys.platform.startswith("win"):
            return
        self._thread = threading.Thread(target=self._run, name="session-monitor", daemon=True)
        self._thread.start()

    def _fire_shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.on_shutdown()
        except Exception:
            log.exception("Fehler beim Herunterfahren-Handler")

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_WTSSESSION_CHANGE:
                    if wparam == WTS_SESSION_LOCK:
                        self.on_lock()
                    elif wparam == WTS_SESSION_UNLOCK:
                        self.on_unlock()
                    return 0
                if msg == WM_QUERYENDSESSION:
                    # Windows fragt, ob beendet werden darf -> Zeit sofort sichern
                    self._fire_shutdown()
                    return 1
                if msg == WM_ENDSESSION:
                    if wparam:
                        self._fire_shutdown()
                    else:
                        self._shutdown_done = False   # Herunterfahren wurde abgebrochen
                    return 0
            except Exception:
                log.exception("Fehler im Sitzungs-Handler")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(wndproc)  # Referenz halten, sonst räumt der GC sie weg
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "ZeitTrackerSessionMonitor"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            log.warning("RegisterClassW fehlgeschlagen: %s", ctypes.get_last_error())
            return
        # Unsichtbares Top-Level-Fenster (Message-Only-Fenster erhalten keine Shutdown-Broadcasts)
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "ZeitTracker", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            log.warning("CreateWindowExW fehlgeschlagen: %s", ctypes.get_last_error())
            return
        if not wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION):
            log.warning("WTSRegisterSessionNotification fehlgeschlagen: %s", ctypes.get_last_error())

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
