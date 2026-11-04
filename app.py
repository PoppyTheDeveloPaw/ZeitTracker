"""Tray-Anwendung: Symbol in der Taskleiste, Kontextmenü, Tooltip, Zeitsteuerung."""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox

import pystray

import personio
import personio_ical
import onedrive
import updater
from version import __version__
import ui
import winutil
from core import APP_NAME, Store, data_dir, fmt_hm, fmt_time, now_dt, parse_dt
from icon_render import render_icon
from session_monitor import SessionMonitor

log = logging.getLogger(APP_NAME)


class TrayApp:
    TICK_MS = 20_000

    def __init__(self):
        self.store = Store()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.report_callback_exception = self._tk_error
        ui.setup_style(self.root)
        self.q: queue.Queue = queue.Queue()
        self.windows: dict = {}
        self.icon: pystray.Icon | None = None
        self._icon_key = None
        self._menu_key = None
        self._last_tick = now_dt()
        self._started_at = now_dt()
        self._last_heartbeat = None
        self._prev_flags = None
        self._sync_running = False
        self._last_auto_sync: dict = {}
        self._od_sync = None
        self._od_key = None
        self._od_running = False
        self._od_error = None
        self._personio_err_notified = False
        self.update_info: dict | None = None
        self._update_last: datetime | None = None
        self._update_running = False
        self.updated_from: str | None = None
        self._od_last: datetime | None = None

    # ------------------------------------------------------------------ Start
    def start(self):
        od_conflict = None
        if self.store.settings.get("onedrive", {}).get("enabled"):
            try:
                log.info("OneDrive beim Start: %s", self.onedrive().sync())
                if self._od_sync.last_action == "download":
                    self.root.after(4000, self.notify_onedrive_download)
            except onedrive.Conflict as e:
                od_conflict = e
            except Exception as e:  # noqa: BLE001 – ohne Netz einfach lokal weiterarbeiten
                log.warning("OneDrive-Abgleich beim Start nicht möglich: %s", e)
        now = now_dt()
        st = self.store.data["state"]
        if st.pop("left_running", False):
            msg = self.store.handle_gap(now, None)     # bewusst weiterlaufen lassen
        else:
            msg = self.store.handle_gap(now, self.store.last_heartbeat())
        # Heruntergefahren und am selben Tag wieder gestartet -> Zeit dazwischen ist Pause
        resumed = self.store.resume_after_shutdown(now)
        msg = resumed or msg
        self.store.heartbeat(now)

        cfg = self.store.settings
        if not resumed and cfg.get("auto_start_work") and self.store.mode() == "idle" and (
                not cfg.get("auto_start_only_workdays", True)
                or self.store.target_minutes(self.store.logical_date(now)) > 0):
            self.store.start_work(now)
            msg = (msg + "\n" if msg else "") + f"Zeiterfassung automatisch gestartet ({fmt_time(now)} Uhr)."

        self.monitor = SessionMonitor(on_lock=lambda: self.q.put(("action", "lock")),
                                      on_unlock=lambda: self.q.put(("action", "unlock")),
                                      on_shutdown=self._on_shutdown)
        self.monitor.start()

        threading.Thread(target=self._tray_thread, name="tray", daemon=True).start()
        self.root.after(100, self._poll)
        self.root.after(1500, self._tick)
        if msg:
            self.root.after(3000, lambda: self.notify(msg))
        if od_conflict:
            self.root.after(1500, lambda: self.ask_onedrive_conflict(od_conflict))
        updater.cleanup_old()
        if self.updated_from:
            self.root.after(3500, lambda: self.notify(
                f"Auf Version {__version__} aktualisiert (vorher {self.updated_from}).", "Update"))
        if not cfg.get("holiday_region") and not cfg.get("holiday_region_asked"):
            self.root.after(1200, lambda: ui.ask_holiday_region(self))
        self.root.mainloop()

    def _tray_thread(self):
        st = self.store.status()
        self.icon = pystray.Icon(APP_NAME, render_icon(st.ring_ok, st.hands_ok, st.mode, st.now),
                                 self._tooltip(st), menu=self._build_menu())
        self.icon.run(setup=self._tray_ready)

    def _tray_ready(self, icon):
        icon.visible = True
        self.q.put(("refresh",))

    # ------------------------------------------------------------------ Menü
    def _build_menu(self):
        M = pystray.MenuItem

        def cb(name):
            return lambda icon, item: self.q.put(("action", name))

        def mode_is(*modes):
            return lambda item: self.store.mode() in modes

        return pystray.Menu(
            M(lambda item: self._menu_header(), lambda icon, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            M("▶  Arbeit starten", cb("start"), visible=mode_is("idle")),
            M("⏸  Pause starten", cb("pause"), visible=mode_is("working")),
            M("▶  Pause beenden", cb("resume"), visible=mode_is("paused")),
            M("⏹  Feierabend", cb("stop"), visible=mode_is("working", "paused")),
            pystray.Menu.SEPARATOR,
            M("Übersicht", cb("overview"), default=True),
            M("Historie / Zeiten bearbeiten", cb("history")),
            M("Saldo-Verlauf", cb("balance")),
            M("Urlaub und Krankheit", cb("absences")),
            M("Einstellungen", cb("settings")),
            M("Mit Personio synchronisieren", cb("sync"),
              visible=lambda item: bool(self.personio_sources())),
            M(lambda item: f"⬆  Update auf v{self.update_info['version']} installieren…" if self.update_info else "Update",
              cb("update"), visible=lambda item: self.update_info is not None),
            M("Mit OneDrive abgleichen", cb("onedrive"),
              visible=lambda item: bool(self.store.settings.get("onedrive", {}).get("enabled"))),
            pystray.Menu.SEPARATOR,
            M("Beenden", cb("quit")),
        )

    def _menu_header(self) -> str:
        st = self.store.status()
        return f"Heute {fmt_hm(st.worked)} / {fmt_hm(st.target)} h  ·  Saldo {fmt_hm(st.balance_now, True)} h"

    # ------------------------------------------------------------------ Ereignisschleife
    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                try:
                    if kind == "action":
                        self.do(item[1])
                    elif kind == "refresh":
                        self.refresh_all()
                    elif kind == "call":
                        item[1](*item[2:])
                except Exception:
                    log.exception("Fehler bei %s", item)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def run_bg(self, fn, on_done):
        """fn im Hintergrund ausführen, on_done(result, error) im UI-Thread aufrufen."""
        def worker():
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                log.warning("Hintergrundaufgabe fehlgeschlagen: %s", e)
                res, err = None, str(e)
            self.q.put(("call", on_done, res, err))
        threading.Thread(target=worker, daemon=True).start()

    def _tick(self):
        try:
            now = now_dt()
            gap_limit = timedelta(minutes=int(self.store.settings.get("sleep_gap_minutes", 15)))
            if now - self._last_tick > gap_limit or \
                    self.store.logical_date(now) != self.store.logical_date(self._last_tick):
                msg = self.store.handle_gap(now, self._last_tick)
                if msg:
                    self.notify(msg)
                self.refresh_all()
            self._last_tick = now
            if not self._last_heartbeat or now - self._last_heartbeat >= timedelta(seconds=60):
                self.store.heartbeat(now)
                self._last_heartbeat = now
            st = self.update_icon()
            self._check_notifications(st)
            self._auto_sync(now)
            self._auto_onedrive(now)
            self._auto_update_check(now)
        except Exception:
            log.exception("Tick fehlgeschlagen")
        self.root.after(self.TICK_MS, self._tick)

    # ------------------------------------------------------------------ Aktionen
    def do(self, name: str):
        s = self.store
        if name == "start":
            s.start_work()
        elif name == "pause":
            s.start_pause()
        elif name == "resume":
            s.end_pause()
        elif name == "stop":
            if not self._stop_after_auto_resume():
                s.stop_work()
            self.sync_onedrive(manual=False)
            p = s.settings["personio"]
            if p.get("enabled") and p.get("export_attendance"):
                self.sync_personio(manual=False, sources=["api"])
        elif name == "lock":
            if s.settings.get("pause_on_lock"):
                s.lock_pause()
        elif name == "unlock":
            if s.settings.get("pause_on_lock") and s.unlock_resume():
                self.notify("Willkommen zurück – die Zeiterfassung läuft wieder.")
        elif name in ui.WINDOWS:
            self.open_window(name)
            return
        elif name == "sync":
            self.sync_personio(manual=True)
            return
        elif name == "onedrive":
            self.sync_onedrive(manual=True)
            return
        elif name == "update":
            if self.update_info:
                ui.UpdateDialog(self, self.update_info)
            else:
                self.check_updates(manual=True)
            return
        elif name == "quit":
            self.quit()
            return
        self.refresh_all()

    def _stop_after_auto_resume(self) -> bool:
        """Feierabend kurz nach automatischem Fortsetzen (PC war aus): Tagesende aufs Herunterfahren legen?"""
        sess = self.store.auto_resumed_session()
        if not sess:
            return False
        started = parse_dt(sess["start"])
        if now_dt() - started > timedelta(minutes=30):
            return False
        off = parse_dt(sess["resumed_after"])
        ans = messagebox.askyesno(
            APP_NAME,
            f"Die Erfassung wurde nach dem Neustart um {fmt_time(started)} Uhr automatisch fortgesetzt.\n\n"
            f"Soll der Arbeitstag stattdessen um {fmt_time(off)} Uhr (Herunterfahren) enden?\n\n"
            f"Ja = Feierabend um {fmt_time(off)} Uhr, die Zeit danach zählt weder als Arbeit noch als Pause\n"
            f"Nein = Feierabend jetzt, {fmt_time(off)}–{fmt_time(started)} zählt als Pause")
        if ans:
            self.store.end_day_at_pause_start()
            return True
        return False

    def open_window(self, name: str, **kw):
        w = self.windows.get(name)
        if w is not None and w.winfo_exists():
            w.bring_to_front(**kw)
            return w
        w = ui.WINDOWS[name](self, **kw)
        self.windows[name] = w
        w.bring_to_front()
        return w

    def refresh_all(self):
        self.update_icon(force=True)
        for w in list(self.windows.values()):
            try:
                if w.winfo_exists():
                    w.refresh()
            except Exception:
                log.exception("Fenster-Aktualisierung fehlgeschlagen")

    # ------------------------------------------------------------------ Symbol
    def _tooltip(self, st) -> str:
        lines = [f"Heute: {fmt_hm(st.worked)} von {fmt_hm(st.target)} h"]
        if st.target > 0:
            if st.remaining > 0:
                line = f"Noch: {fmt_hm(st.remaining)} h"
                if st.remaining_with_balance < st.remaining:
                    line += f" (mit Saldo {fmt_hm(st.remaining_with_balance)} h)"
                lines.append(line)
            else:
                lines.append("Tagessoll erreicht ✓")
        else:
            a = self.store.absence_for(st.workday)
            lines.append(ui.absence_text(a)[:40] if a else "Kein Arbeitstag")
        lines.append({"working": f"Läuft seit {fmt_time(st.since)}",
                      "paused": f"Pause seit {fmt_time(st.since)}",
                      "idle": "Nicht eingestempelt"}[st.mode])
        lines.append(f"Saldo: {fmt_hm(st.balance_now, True)} h")
        return "\n".join(lines)[:127]

    def update_icon(self, force: bool = False):
        st = self.store.status()
        if not self.icon:
            return st
        key = (st.ring_ok, st.hands_ok, st.mode, st.now.hour, st.now.minute)
        try:
            if force or key != self._icon_key:
                self.icon.icon = render_icon(st.ring_ok, st.hands_ok, st.mode, st.now)
                self._icon_key = key
            self.icon.title = self._tooltip(st)
            self.icon.update_menu()
        except Exception:
            log.exception("Symbol konnte nicht aktualisiert werden")
        return st

    def notify(self, message: str, title: str = APP_NAME):
        try:
            if self.icon:
                self.icon.notify(message, title)
        except Exception:
            log.exception("Benachrichtigung fehlgeschlagen")

    def _check_notifications(self, st):
        flags = (st.now.date(), st.hands_ok, st.ring_ok)
        prev, self._prev_flags = self._prev_flags, flags
        if not self.store.settings.get("notify_target_reached", True) or st.target <= 0:
            return
        if not prev or prev[0] != flags[0] or st.mode != "working":
            return
        if st.ring_ok and not prev[2] and not st.hands_ok:
            self.notify(f"Mit deinem Überstunden-Saldo hast du heute genug gearbeitet "
                        f"(Saldo danach: {fmt_hm(st.balance_now, True)} h).")
        if st.hands_ok and not prev[1]:
            self.notify(f"Tagessoll von {fmt_hm(st.target)} h erreicht – schönen Feierabend!")

    # ------------------------------------------------------------------ Personio
    def personio_sources(self) -> list:
        """Aktive Personio-Quellen: 'api' und/oder 'ical' (Kalender-Abo)."""
        out = []
        if self.store.settings["personio"].get("enabled"):
            out.append("api")
        if self.store.settings.get("ical", {}).get("enabled"):
            out.append("ical")
        return out

    def sync_personio(self, manual: bool = True, on_done=None, sources: list | None = None):
        sources = sources if sources is not None else self.personio_sources()
        if not sources:
            if manual:
                messagebox.showinfo(APP_NAME, "Weder die Personio-API noch das Personio-Kalender-Abo ist "
                                              "in den Einstellungen aktiviert.")
            return
        if self._sync_running:
            if manual:
                self.notify("Synchronisation läuft bereits …")
            return
        self._sync_running = True
        secret = personio.get_secret(self.store) if "api" in sources else ""
        url = personio_ical.get_url(self.store) if "ical" in sources else ""

        def job():
            before = self.store.external_absence_snapshot()
            parts, errors = [], []
            if "ical" in sources:
                try:
                    parts.append(personio_ical.run_import(self.store, url))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Kalender-Abo: {e}")
                    with self.store.lock:
                        self.store.settings["ical"]["last_result"] = f"❌ {e}"[:500]
                        self.store.save()
            if "api" in sources:
                try:
                    parts.append(personio.run_sync(self.store, secret))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"API: {e}")
                    with self.store.lock:
                        self.store.settings["personio"]["last_result"] = f"❌ {e}"[:500]
                        self.store.save()
            changes = self.store.describe_absence_changes(before, self.store.external_absence_snapshot())
            if errors and not parts:
                raise RuntimeError("\n".join(errors))
            return "\n".join(parts + [f"❌ {e}" for e in errors]), changes

        def done(result, error):
            self._sync_running = False
            text, changes = result if result else (None, None)
            text = text if not error else f"❌ Personio-Abgleich fehlgeschlagen:\n{error}"
            self.refresh_all()
            want = self.store.settings.get("notify_personio_changes", True)
            if on_done:
                on_done(text + (f"\n{changes}" if changes else ""))
            elif manual:
                self.notify((changes or text)[:250], "Personio")
            elif changes and want:
                self.notify(changes[:250], "Neues aus Personio")
            if error:
                log.warning(text)
                if not manual and want and not self._personio_err_notified:
                    self.notify("Abwesenheiten konnten nicht aus Personio geladen werden. "
                                "Details unter Einstellungen › Personio.", "Personio")
                self._personio_err_notified = True
            else:
                self._personio_err_notified = False
        self.run_bg(job, done)

    def _auto_sync(self, now: datetime):
        due = []
        for src, cfg in (("api", self.store.settings["personio"]), ("ical", self.store.settings.get("ical", {}))):
            interval = int(cfg.get("auto_sync_minutes") or 0)
            if src not in self.personio_sources() or interval <= 0:
                continue
            last = self._last_auto_sync.get(src) or (parse_dt(cfg["last_sync"]) if cfg.get("last_sync") else None)
            if last is None or now - last >= timedelta(minutes=interval):
                self._last_auto_sync[src] = now
                due.append(src)
        if due:
            self.sync_personio(manual=False, sources=due)

    # ------------------------------------------------------------------ OneDrive
    def onedrive(self) -> "onedrive.OneDriveSync":
        cfg = self.store.settings.get("onedrive", {})
        key = onedrive.effective_client_id(cfg)
        if self._od_sync is None or self._od_key != key:
            self.od_auth = onedrive.Auth(key, self.store.path.parent / "onedrive_token.bin")
            self._od_sync = onedrive.OneDriveSync(self.store, self.od_auth.token)
            self._od_key = key
        return self._od_sync

    def sync_onedrive(self, manual: bool = True, on_done=None):
        if not self.store.settings.get("onedrive", {}).get("enabled"):
            if manual:
                messagebox.showinfo(APP_NAME, "Die OneDrive-Sicherung ist in den Einstellungen nicht aktiviert.")
            return
        if self._od_running:
            return
        self._od_running = True
        self._od_last = now_dt()

        def done(result, error):
            self._od_running = False
            self.refresh_all()
            conflict = isinstance(self._od_error, onedrive.Conflict)
            text = result or f"❌ {error}"
            if on_done:
                on_done(text)
            if conflict:
                self.ask_onedrive_conflict(self._od_error)
            elif manual and not on_done:
                self.notify(text[:250], "OneDrive")
            elif not error and self._od_sync and self._od_sync.last_action == "download":
                self.notify_onedrive_download()
            elif error:
                log.warning("OneDrive: %s", error)

        def job():
            self._od_error = None
            try:
                return self.onedrive().sync()
            except Exception as e:
                self._od_error = e
                raise
        self.run_bg(job, done)

    def notify_onedrive_download(self):
        if not self.store.settings.get("notify_onedrive_download", True):
            return
        when = ""
        mod = self._od_sync.last_remote_modified if self._od_sync else ""
        if mod:
            try:
                when = datetime.fromisoformat(mod.replace("Z", "+00:00")).astimezone().strftime(" vom %d.%m.%Y %H:%M")
            except ValueError:
                pass
        self.notify(f"Neuerer Stand{when} von OneDrive geladen – die Daten wurden aktualisiert.", "OneDrive")

    # ------------------------------------------------------------------ Updates
    def _auto_update_check(self, now: datetime):
        if not self.store.settings.get("update_check", True) or self._update_running:
            return
        first_due = self._update_last is None and (now - self._started_at) >= timedelta(seconds=20)
        if first_due or (self._update_last and now - self._update_last >= timedelta(hours=24)):
            self.check_updates(manual=False)

    def check_updates(self, manual: bool = True, on_done=None):
        if self._update_running:
            return
        self._update_running = True
        self._update_last = now_dt()

        def done(info, error):
            self._update_running = False
            if error:
                log.info("Update-Prüfung: %s", error)
                text = f"❌ {error}"
            elif info["newer"]:
                self.update_info = info
                self.update_icon(force=True)
                text = f"Version {info['version']} ist verfügbar (installiert: {__version__})."
                if manual:
                    ui.UpdateDialog(self, info)
                elif self.store.settings.get("update_notified") != info["version"]:
                    self.notify(f"Version {info['version']} ist verfügbar. Installieren über das Menü "
                                "(Rechtsklick › Update).", "Update verfügbar")
                    with self.store.lock:
                        self.store.settings["update_notified"] = info["version"]
                        self.store.save()
            elif __version__ == "dev":
                text = (f"Entwicklerversion – automatische Updates nur für die Release-EXE. "
                        f"Neueste Version auf GitHub: {info['version'] or '–'}.")
            else:
                text = f"Du hast die neueste Version ({__version__})."
            if on_done:
                on_done(text)
            elif manual and not (info and info.get("newer")):
                messagebox.showinfo("Updates", text)
        self.run_bg(updater.check, done)

    def perform_update(self, new_exe):
        """Neue EXE ist geladen und geprüft: austauschen, neu starten, beenden."""
        with self.store.lock:
            if self.store.mode() != "idle":
                self.store.data["state"]["left_running"] = True   # Erfassung läuft einfach weiter
            self.store.heartbeat()
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        winutil.release_single_instance()
        try:
            updater.install(new_exe, args=["--updated-from", __version__])
        except Exception as e:  # noqa: BLE001
            log.exception("Update fehlgeschlagen")
            messagebox.showerror("Update", f"{e}\n\nDer ZeitTracker wird neu gestartet.")
            with self.store.lock:
                self.store.data["state"].pop("left_running", None)
                self.store.save()
            os.execl(sys.executable, sys.executable, *sys.argv)
        self.root.quit()
        self.root.destroy()
        os._exit(0)

    def _auto_onedrive(self, now: datetime):
        cfg = self.store.settings.get("onedrive", {})
        minutes = int(cfg.get("sync_minutes") or 0)
        if not cfg.get("enabled") or minutes <= 0:
            return
        if self._od_last is None or now - self._od_last >= timedelta(minutes=minutes):
            self.sync_onedrive(manual=False)

    def ask_onedrive_conflict(self, err):
        when = ""
        if getattr(err, "remote_modified", ""):
            try:
                when = datetime.fromisoformat(err.remote_modified.replace("Z", "+00:00")).astimezone().strftime(
                    " (Stand auf OneDrive vom %d.%m.%Y %H:%M)")
            except ValueError:
                pass
        ans = messagebox.askyesnocancel(
            "OneDrive – welcher Stand gilt?",
            f"{getattr(err, 'reason', '') or err}{when}\n\n"
            "Ja = Stand von OneDrive übernehmen (der lokale Stand wird als Sicherung aufbewahrt)\n"
            "Nein = diesen lokalen Stand auf OneDrive hochladen (der OneDrive-Stand wird als Sicherung aufbewahrt)\n"
            "Abbrechen = später entscheiden (Einstellungen › OneDrive)")
        if ans is None:
            return
        use = "remote" if ans else "local"

        def done(result, error):
            self.refresh_all()
            self.notify((result or f"❌ {error}")[:250], "OneDrive")
        self.run_bg(lambda: self.onedrive().resolve(use), done)

    # ------------------------------------------------------------------ Beenden
    def quit(self):
        if self.store.mode() != "idle":
            ans = messagebox.askyesnocancel(
                APP_NAME, "Die Arbeitszeit läuft noch.\n\n"
                          "Ja = jetzt Feierabend buchen und beenden\n"
                          "Nein = Zeit weiterlaufen lassen (wird beim nächsten Start fortgesetzt)\n"
                          "Abbrechen = nicht beenden")
            if ans is None:
                return
            if ans:
                self.store.stop_work()
            else:
                with self.store.lock:
                    self.store.data["state"]["left_running"] = True
                    self.store.save()
        if self.store.settings.get("onedrive", {}).get("enabled") and not self._od_running:
            try:
                self.onedrive().sync()
            except Exception as e:  # noqa: BLE001
                log.warning("OneDrive beim Beenden: %s", e)
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        self.root.quit()
        self.root.destroy()

    def _on_shutdown(self):
        """Läuft im Monitor-Thread, während Windows herunterfährt – muss schnell und direkt sein."""
        try:
            self.store.shutdown()
            log.info("Herunterfahren: Zustand gesichert (%s)", self.store.settings.get("shutdown_mode"))
        except Exception:
            log.exception("Speichern beim Herunterfahren fehlgeschlagen")

    def _tk_error(self, exc, val, tb):
        log.error("Unbehandelter Fehler", exc_info=(exc, val, tb))
        messagebox.showerror(APP_NAME, f"Unerwarteter Fehler:\n{val}\n\nDetails stehen in der Logdatei.")


def main():
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=d / "zeittracker.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", encoding="utf-8")
    winutil.enable_dpi_awareness()
    if not winutil.single_instance():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "Der ZeitTracker läuft bereits – das Symbol findest du unten rechts in der Taskleiste "
                                      "(ggf. über den Pfeil ^ bei den ausgeblendeten Symbolen).")
        r.destroy()
        sys.exit(0)
    updated_from = None
    if "--updated-from" in sys.argv:
        i = sys.argv.index("--updated-from")
        updated_from = sys.argv[i + 1] if i + 1 < len(sys.argv) else "?"
    try:
        tray = TrayApp()
        tray.updated_from = updated_from
        tray.start()
    except Exception:
        log.exception("Absturz")
        raise


if __name__ == "__main__":
    main()
