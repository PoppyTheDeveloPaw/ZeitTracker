"""Datenmodell, Speicherung und Berechnungen des ZeitTrackers.

Alle Zeiten werden als lokale Zeit (ohne Zeitzone) im ISO-Format gespeichert.
Die Daten liegen in %APPDATA%\\ZeitTracker\\data.json (unter Windows).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

try:  # optional: automatische gesetzliche Feiertage
    import holidays as _holidays  # type: ignore
except Exception:  # pragma: no cover
    _holidays = None

APP_NAME = "ZeitTracker"

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]

BUNDESLAENDER = {
    "": "– keine automatischen Feiertage –",
    "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg",
    "HB": "Bremen", "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen", "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz",
    "SL": "Saarland", "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}

# reduces = verringert die Soll-Arbeitszeit des Tages
ABSENCE_KINDS = {
    "urlaub":    {"label": "Urlaub",               "color": "#2e7d32", "half_color": "#81c784", "reduces": True},
    "krank":     {"label": "Krankheit",            "color": "#c62828", "half_color": "#e57373", "reduces": True},
    "feiertag":  {"label": "Feiertag",             "color": "#1565c0", "half_color": "#64b5f6", "reduces": True},
    "sonstiges": {"label": "Sonstige Abwesenheit", "color": "#6d4c41", "half_color": "#a1887f", "reduces": True},
    "fza":       {"label": "Freizeitausgleich",    "color": "#6a1b9a", "half_color": "#ba68c8", "reduces": False},
}


HALF_LABELS = {"am": "vormittags", "pm": "nachmittags"}   # welche Tageshälfte ist frei

# --------------------------------------------------------------------------- Helfer

def now_dt() -> datetime:
    return datetime.now().replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def day_start(d: date) -> datetime:
    return datetime.combine(d, time())


def daterange(start: date, end: date):
    """Alle Tage von start bis einschließlich end."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def fmt_hm(minutes: float, signed: bool = False) -> str:
    m = int(round(minutes))
    sign = "-" if m < 0 else ("+" if signed and m > 0 else "")
    m = abs(m)
    return f"{sign}{m // 60}:{m % 60:02d}"


def fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def fmt_time(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M") if dt else ""


def fmt_time_on(dt: Optional[datetime], d: date) -> str:
    """Uhrzeit relativ zum Arbeitstag d, z. B. '02:15 (+1)' bei Arbeit nach Mitternacht."""
    if not dt:
        return ""
    extra = (dt.date() - d).days
    return dt.strftime("%H:%M") + (f" (+{extra})" if extra > 0 else "")


def fmt_days(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def parse_hm(text: str) -> int:
    """'7:30', '7,5', '7.5h', '-1:15' -> Minuten."""
    t = text.strip().replace(" ", "").lower().rstrip("h")
    if not t:
        raise ValueError("leer")
    neg = t.startswith("-")
    t = t.lstrip("+-")
    if ":" in t:
        h, mm = t.split(":", 1)
        mins = int(mm)
        if not 0 <= mins < 60:
            raise ValueError("Minuten ungültig")
        val = int(h or 0) * 60 + mins
    else:
        val = round(float(t.replace(",", ".")) * 60)
    return -val if neg else val


def parse_clock(text: str, d: date) -> datetime:
    """'8:05' / '0805' / '8' -> datetime am Tag d."""
    t = text.strip().replace(".", ":").replace(" ", "")
    extra = 0
    for suffix in ("(+1)", "+1"):
        if t.endswith(suffix):
            t, extra = t[: -len(suffix)], 1
            break
    if extra:
        return parse_clock(t, d + timedelta(days=1))
    if ":" in t:
        h, m = t.split(":", 1)
    elif len(t) in (3, 4) and t.isdigit():
        h, m = t[:-2], t[-2:]
    else:
        h, m = t, "0"
    hh, mm = int(h), int(m)
    if hh == 24 and mm == 0:
        return day_start(d + timedelta(days=1))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"Ungültige Uhrzeit: {text}")
    return datetime.combine(d, time(hh, mm))


def data_dir() -> Path:
    override = os.environ.get("ZEITTRACKER_DATA")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def _merge(defaults: dict, loaded: dict) -> dict:
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict) and k not in ("absences",):
            _merge(defaults[k], v)
        else:
            defaults[k] = v
    return defaults


def default_days() -> dict:
    return {str(i): {"active": i < 5, "hours": 8.0} for i in range(7)}


def default_data() -> dict:
    today = date.today().isoformat()
    return {
        "version": 1,
        "settings": {
            "tracking_start": today,
            "initial_balance_minutes": 0,
            "vacation_days_per_year": 30.0,
            "vacation_carryover": 0.0,
            "holiday_region": "",
            "launch_with_windows": False,
            "auto_start_work": False,
            "auto_start_only_workdays": True,
            "shutdown_mode": "pause",     # pause | stop | none
            "restart_grace_minutes": 10,
            "day_boundary_hour": 5,       # Tageswechsel: Arbeit bis 5 Uhr zählt zum Vortag  # Neustart innerhalb dieser Zeit = Arbeit läuft durch
            "holiday_region_asked": False,
            "pause_on_lock": False,
            "notify_target_reached": True,
            "sleep_gap_minutes": 15,
            "personio": {
                "enabled": False,
                "client_id": "",
                "email": "",
                "person_id": "",
                "import_absences": True,
                "include_pending": False,
                "export_attendance": False,
                "export_from": today,
                "auto_sync_minutes": 60,
                "last_sync": None,
                "last_result": "",
            },
            "onedrive": {                  # Sicherung/Abgleich über OneDrive (eigene App-Registrierung)
                "enabled": False,
                "client_id": "",
                "sync_minutes": 15,
            },
            "ical": {                      # Personio-Kalender-Abo (ohne API)
                "enabled": False,
                "name_filter": "",
                "generic_kind": "sonstiges",   # Einträge nur mit „[Abwesenheit]“ (Art unbekannt)
                "auto_sync_minutes": 180,
                "last_sync": None,
                "last_result": "",
            },
        },
        "schedules": [{"valid_from": "2000-01-01", "days": default_days()}],
        "sessions": [],
        "absences": {},
        "personio_deleted": [],
        "state": {"paused_since": None, "paused_by": None, "last_heartbeat": None},
    }


# --------------------------------------------------------------------------- Status

@dataclass
class Status:
    now: datetime
    workday: date             # aktueller Arbeitstag (bei Nachtarbeit ggf. gestern)
    mode: str                 # "working" | "paused" | "idle"
    since: Optional[datetime]
    worked: float             # heute gearbeitete Minuten
    target: float             # heutiges Soll in Minuten
    pause_today: float
    balance_before: float     # Überstundensaldo bis einschließlich gestern

    @property
    def balance_now(self) -> float:
        return self.balance_before + self.worked - self.target

    @property
    def remaining(self) -> float:
        return max(0.0, self.target - self.worked)

    @property
    def remaining_with_balance(self) -> float:
        return max(0.0, self.target - self.worked - self.balance_before)

    @property
    def hands_ok(self) -> bool:
        """Tagessoll allein durch die heutige Arbeitszeit erfüllt."""
        return self.worked + 0.5 >= self.target

    @property
    def ring_ok(self) -> bool:
        """Tagessoll unter Berücksichtigung des Überstundensaldos erfüllt."""
        return self.worked + self.balance_before + 0.5 >= self.target

    def eta(self, remaining: float) -> Optional[datetime]:
        if self.mode != "working" or remaining <= 0:
            return None
        return self.now + timedelta(minutes=remaining)


# --------------------------------------------------------------------------- Store

class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else data_dir() / "data.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.version = 0
        self._holiday_cache: dict = {}
        self._balance_cache: dict = {}
        self._backup_done = False
        self.data = self._load()

    # ------------------------------------------------------------ Persistenz
    def _load(self) -> dict:
        base = default_data()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
            except Exception:
                broken = self.path.with_name(f"data.defekt-{datetime.now():%Y%m%d-%H%M%S}.json")
                shutil.copy2(self.path, broken)
                return base
            _merge(base, loaded)
        return base

    def save(self) -> None:
        with self.lock:
            self._daily_backup()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)

    def _daily_backup(self) -> None:
        if self._backup_done or not self.path.exists():
            return
        self._backup_done = True
        bdir = self.path.parent / "backups"
        bdir.mkdir(exist_ok=True)
        target = bdir / f"data-{date.today().isoformat()}.json"
        if not target.exists():
            shutil.copy2(self.path, target)
        for old in sorted(bdir.glob("data-*.json"))[:-14]:
            try:
                old.unlink()
            except OSError:
                pass

    def replace_data(self, new: dict, keep_local_state: bool = True) -> None:
        """Gesamten Datenstand ersetzen (z. B. Cloud-Stand übernehmen)."""
        with self.lock:
            hb = self.data["state"].get("last_heartbeat")
            merged = _merge(default_data(), copy.deepcopy(new))
            if keep_local_state:
                merged["state"]["last_heartbeat"] = hb
            self.data = merged
            self._holiday_cache.clear()
            self.touch()

    def touch(self) -> None:
        """Nach jeder inhaltlichen Änderung aufrufen."""
        with self.lock:
            self.version += 1
            self._balance_cache.clear()
            self.save()

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    @property
    def tracking_start(self) -> date:
        return parse_date(self.settings["tracking_start"])

    # ------------------------------------------------------------ Arbeitszeitmodell
    def schedule_for(self, d: date) -> dict:
        best = None
        for s in self.data["schedules"]:
            if s["valid_from"] <= d.isoformat() and (best is None or s["valid_from"] > best["valid_from"]):
                best = s
        if best is None:
            best = min(self.data["schedules"], key=lambda s: s["valid_from"])
        return best["days"]

    def set_schedule(self, days: dict, valid_from: Optional[date] = None) -> None:
        with self.lock:
            if valid_from is None:
                self.data["schedules"] = [{"valid_from": "2000-01-01", "days": copy.deepcopy(days)}]
            else:
                vf = valid_from.isoformat()
                self.data["schedules"] = [s for s in self.data["schedules"] if s["valid_from"] < vf]
                self.data["schedules"].append({"valid_from": vf, "days": copy.deepcopy(days)})
                self.data["schedules"].sort(key=lambda s: s["valid_from"])
            self.touch()

    def base_target(self, d: date) -> float:
        day = self.schedule_for(d).get(str(d.weekday()), {})
        return float(day.get("hours", 0)) * 60 if day.get("active") else 0.0

    def is_workday(self, d: date) -> bool:
        return self.base_target(d) > 0

    def holiday_name(self, d: date) -> Optional[str]:
        region = self.settings.get("holiday_region") or ""
        if not region or _holidays is None:
            return None
        key = (region, d.year)
        if key not in self._holiday_cache:
            try:
                try:
                    h = _holidays.country_holidays("DE", subdiv=region, years=d.year, language="de")
                except TypeError:
                    h = _holidays.country_holidays("DE", subdiv=region, years=d.year)
                self._holiday_cache[key] = dict(h)
            except Exception:
                self._holiday_cache[key] = {}
        return self._holiday_cache[key].get(d)

    def absence_for(self, d: date) -> Optional[dict]:
        a = self.data["absences"].get(d.isoformat())
        if a:
            return a
        name = self.holiday_name(d)
        if name:
            return {"kind": "feiertag", "portion": 1.0, "source": "auto", "note": name}
        return None

    def target_minutes(self, d: date) -> float:
        if d < self.tracking_start:
            return 0.0
        base = self.base_target(d)
        if base <= 0:
            return 0.0
        a = self.absence_for(d)
        if a and ABSENCE_KINDS.get(a["kind"], {}).get("reduces", True):
            base *= 1.0 - min(1.0, max(0.0, float(a.get("portion", 1.0))))
        return base

    # ------------------------------------------------------------ Sitzungen / Stempeln
    def open_session(self) -> Optional[dict]:
        for s in reversed(self.data["sessions"]):
            if s.get("end") is None:
                return s
        return None

    def is_working(self) -> bool:
        return self.open_session() is not None

    def paused_since(self) -> Optional[datetime]:
        p = self.data["state"].get("paused_since")
        return parse_dt(p) if p and not self.is_working() else None

    def is_paused(self) -> bool:
        return self.paused_since() is not None

    def mode(self) -> str:
        if self.is_working():
            return "working"
        if self.is_paused():
            return "paused"
        return "idle"

    def _new_session(self, start: datetime, end: Optional[datetime] = None,
                     workday: Optional[date] = None) -> dict:
        return {"id": uuid.uuid4().hex, "start": iso(start), "end": iso(end) if end else None,
                "workday": (workday or start.date()).isoformat(),
                "personio_id": None, "dirty": False}

    @staticmethod
    def session_workday(s: dict) -> date:
        """Arbeitstag, dem eine Sitzung zugerechnet wird (Nachtarbeit zählt zum Starttag)."""
        return parse_date(s.get("workday") or s["start"][:10])

    def _last_session(self) -> Optional[dict]:
        return max(self.data["sessions"], key=lambda s: s["start"]) if self.data["sessions"] else None

    def day_boundary(self) -> int:
        """Uhrzeit (volle Stunde), zu der ein Arbeitstag spätestens endet. Standard 5 Uhr."""
        try:
            return max(0, min(12, int(self.settings.get("day_boundary_hour", 5))))
        except (TypeError, ValueError):
            return 5

    def logical_date(self, t: datetime) -> date:
        """Arbeitstag zu einem Zeitpunkt: Bis zum Tageswechsel (z. B. 5 Uhr) zählt alles zum Vortag."""
        return (t - timedelta(hours=self.day_boundary())).date()

    def _continued_workday(self, t: datetime) -> date:
        """Arbeitstag für Arbeit, die zum Zeitpunkt t beginnt.

        Regeln mit Tageswechsel (z. B. 5 Uhr):
          * Arbeit vor dem Tageswechsel zählt zum Vortag – auch nach einer Pause.
          * Eine Pause, die über den Tageswechsel läuft, beendet den Arbeitstag (siehe end_stale_pause).
        """
        return self.logical_date(t)

    def current_workday(self, now: Optional[datetime] = None) -> date:
        now = now or now_dt()
        s = self.open_session()
        if s:
            return self.session_workday(s)
        return self.logical_date(now)

    def _close(self, s: dict, t: datetime) -> None:
        start = parse_dt(s["start"])
        if t <= start + timedelta(seconds=59):
            self._remove_session(s)   # Sitzung ohne Dauer verwerfen
        else:
            s["end"] = iso(t)

    def _remove_session(self, s: dict) -> None:
        if s.get("personio_id"):
            self.data["personio_deleted"].append(s["personio_id"])
        self.data["sessions"].remove(s)

    def _set_pause(self, since: Optional[datetime], by: Optional[str] = None) -> None:
        self.data["state"]["paused_since"] = iso(since) if since else None
        self.data["state"]["paused_by"] = by if since else None
        self.data["state"].pop("paused_by_lock", None)

    def paused_by(self) -> Optional[str]:
        return self.data["state"].get("paused_by") if self.is_paused() else None

    def start_work(self, t: Optional[datetime] = None) -> None:
        with self.lock:
            if self.is_working():
                return
            t = t or now_dt()
            wd = self._continued_workday(t)
            sess = self._new_session(t, workday=wd)
            p = self.paused_since()
            if p and self.data["state"].get("paused_by") in ("shutdown", "gap"):
                sess["resumed_after"] = iso(p)      # automatisch fortgesetzt nach PC aus/Standby
            self.data["sessions"].append(sess)
            self._set_pause(None)
            self.touch()

    def auto_resumed_session(self) -> Optional[dict]:
        """Laufende Sitzung, die nach Herunterfahren/Standby automatisch fortgesetzt wurde."""
        s = self.open_session()
        return s if s and s.get("resumed_after") else None

    def end_day_at_pause_start(self) -> Optional[datetime]:
        """Feierabend rückwirkend: die automatisch fortgesetzte Sitzung verwerfen. Der Arbeitstag
        endet damit zum Zeitpunkt des Herunterfahrens, die Pause danach zählt nicht."""
        with self.lock:
            s = self.auto_resumed_session()
            if not s:
                return None
            end = parse_dt(s["resumed_after"])
            self._remove_session(s)
            self._set_pause(None)
            self.touch()
            return end

    def start_pause(self, t: Optional[datetime] = None, by: Optional[str] = None) -> None:
        with self.lock:
            s = self.open_session()
            if not s:
                return
            t = t or now_dt()
            self._close(s, t)
            self._set_pause(t, by or "manual")
            self.touch()

    def lock_pause(self, t: Optional[datetime] = None) -> bool:
        """Bildschirm gesperrt: laufende Arbeit pausieren. True, wenn pausiert wurde."""
        if not self.is_working():
            return False
        self.start_pause(t, by="lock")
        return True

    def unlock_resume(self, t: Optional[datetime] = None) -> bool:
        """Bildschirm entsperrt: nur fortsetzen, wenn die Pause durch das Sperren entstand."""
        return self._resume_if(t, "lock")

    def resume_after_shutdown(self, t: Optional[datetime] = None) -> Optional[str]:
        """Beim Programmstart nach dem Herunterfahren.

        * Neustart innerhalb der Toleranz -> Arbeitszeit läuft einfach durch (keine Pause).
        * sonst am selben Tag -> Zeit dazwischen ist Pause, Erfassung läuft weiter.
        """
        with self.lock:
            t = t or now_dt()
            info = self.data["state"].pop("shutdown", None)
            if info and not self.is_working():
                at = parse_dt(info["at"])
                grace = timedelta(minutes=int(self.settings.get("restart_grace_minutes", 10)))
                if timedelta(0) <= t - at <= grace:
                    sess = next((x for x in self.data["sessions"] if x["id"] == info.get("session")), None)
                    if sess and sess.get("end") == info["at"]:
                        sess["end"] = None                  # Sitzung einfach weiterlaufen lassen
                        if sess.get("personio_id"):
                            sess["dirty"] = True
                    else:                                   # (z. B. sehr kurze, verworfene Sitzung)
                        self.data["sessions"].append(self._new_session(at, workday=self._continued_workday(at)))
                    self._set_pause(None)
                    self.touch()
                    return (f"Neustart nach {int((t - at).total_seconds() // 60)} Minuten – "
                            "die Arbeitszeit läuft ohne Unterbrechung weiter.")
            self.save()
            since = self.paused_since()
            if since and self._resume_if(t, "shutdown"):
                return (f"Willkommen zurück! Die Zeit von {fmt_time(since)} bis {fmt_time(t or now_dt())} "
                        "(PC aus) zählt als Pause – die Erfassung läuft weiter.")
            return None

    def _resume_if(self, t: Optional[datetime], reason: str) -> bool:
        with self.lock:
            p = self.paused_since()
            if not (p and self.data["state"].get("paused_by") == reason):
                return False
            t = max(t or now_dt(), p)
            if self.logical_date(p) != self.logical_date(t):   # Pause lief über den Tageswechsel
                self._set_pause(None)
                self.touch()
                return False
            self.start_work(t)
            return True

    def end_pause(self, t: Optional[datetime] = None) -> None:
        self.start_work(t)

    def stop_work(self, t: Optional[datetime] = None) -> None:
        with self.lock:
            s = self.open_session()
            if s:
                self._close(s, t or now_dt())
            self._set_pause(None)
            self.touch()

    def shutdown(self, t: Optional[datetime] = None) -> None:
        """Windows fährt herunter / Abmeldung – Verhalten laut Einstellung."""
        with self.lock:
            mode = self.settings.get("shutdown_mode", "pause")
            t = t or now_dt()
            s_open = self.open_session()
            self.data["state"]["shutdown"] = (
                {"at": iso(t), "session": s_open["id"]} if s_open and mode != "none" else None)
            if self.is_working():
                if mode == "pause":
                    self.start_pause(t, by="shutdown")
                elif mode == "stop":
                    self.stop_work(t)
            elif self.is_paused() and mode == "stop":
                self.stop_work(t)
            self.heartbeat(t)

    def heartbeat(self, t: Optional[datetime] = None) -> None:
        with self.lock:
            self.data["state"]["last_heartbeat"] = iso(t or now_dt())
            self.save()

    def last_heartbeat(self) -> Optional[datetime]:
        hb = self.data["state"].get("last_heartbeat")
        return parse_dt(hb) if hb else None

    def end_stale_pause(self, now: datetime) -> bool:
        """Eine Pause, die über das Tagesende hinaus läuft, beendet den Arbeitstag."""
        p = self.data["state"].get("paused_since")
        if p and self.logical_date(parse_dt(p)) != self.logical_date(now):
            self._set_pause(None)
            self.touch()
            return True
        return False

    def handle_gap(self, now: datetime, last_alive: Optional[datetime]) -> Optional[str]:
        """Nach Absturz, hartem Ausschalten oder Standby: Lücke als Pause bzw. Feierabend werten."""
        with self.lock:
            msg = None
            gap = timedelta(minutes=int(self.settings.get("sleep_gap_minutes", 15)))
            s = self.open_session()
            if s and last_alive and now - last_alive > gap:
                end = max(last_alive, parse_dt(s["start"]))
                self._close(s, end)
                if self.logical_date(end) == self.logical_date(now):
                    if self.settings.get("shutdown_mode", "pause") == "pause":
                        self._set_pause(end, "gap")
                        self.start_work(now)
                        msg = (f"Der PC war von {fmt_time(end)} bis {fmt_time(now)} aus bzw. im Standby. "
                               "Das zählt als Pause – die Erfassung läuft weiter.")
                    else:
                        self._set_pause(end, "gap")
                        msg = (f"Der PC war von {fmt_time(end)} bis {fmt_time(now)} aus bzw. im Standby. "
                               "Die Zeit wurde als Pause gewertet – über das Menü kannst du die Pause beenden.")
                else:
                    self._set_pause(None)
                    msg = (f"Die Arbeitszeit vom {fmt_date(self.session_workday(s))} wurde automatisch um "
                           f"{fmt_time(end)} beendet (PC war aus).")
                self.touch()
            self.end_stale_pause(now)
            return msg

    def day_sessions(self, d: date) -> list:
        res = [s for s in self.data["sessions"] if self.session_workday(s) == d]
        return sorted(res, key=lambda s: s["start"])

    def replace_day_sessions(self, d: date, rows: list) -> None:
        """rows: [{'id': str|None, 'start': datetime, 'end': datetime|None}] – alle gehören zum Arbeitstag d."""
        with self.lock:
            existing = {s["id"]: s for s in self.day_sessions(d)}
            for r in rows:
                s = existing.pop(r.get("id"), None) if r.get("id") else None
                new_start = iso(r["start"])
                new_end = iso(r["end"]) if r.get("end") else None
                if s is None:
                    self.data["sessions"].append(self._new_session(r["start"], r.get("end"), workday=d))
                elif s["start"] != new_start or s["end"] != new_end:
                    s["start"], s["end"] = new_start, new_end
                    s["workday"] = d.isoformat()
                    if s.get("personio_id"):
                        s["dirty"] = True
            for s in existing.values():
                self._remove_session(s)
            self.data["sessions"].sort(key=lambda s: s["start"])
            if self.is_working():
                self._set_pause(None)
            self.touch()

    # ------------------------------------------------------------ Abwesenheiten
    def set_absences(self, start: date, end: date, kind: str, portion: float = 1.0,
                     only_workdays: bool = True, note: str = "", source: str = "manual",
                     first_half: bool = False, last_half: bool = False,
                     single_half: Optional[str] = None) -> int:
        """Trägt eine Abwesenheit ein. Halbe Tage sind nur am ersten und/oder letzten
        eingetragenen Tag möglich: am ersten Tag ist dann der Nachmittag frei ('pm'), am letzten
        Tag der Vormittag ('am'). Bei einem einzelnen Tag wählt single_half die freie Hälfte."""
        with self.lock:
            days = [d for d in daterange(start, end) if not (only_workdays and not self.is_workday(d))]
            for i, d in enumerate(days):
                half = None
                if len(days) == 1:
                    p = 0.5 if (first_half or last_half or portion < 1 or single_half) else 1.0
                    half = (single_half or ("pm" if first_half else "am" if last_half else None)) if p < 1 else None
                elif i == 0:
                    p, half = (0.5, "pm") if first_half else (1.0, None)
                elif i == len(days) - 1:
                    p, half = (0.5, "am") if last_half else (1.0, None)
                else:
                    p = 1.0
                entry = {"kind": kind, "portion": p, "source": source, "note": note}
                if half:
                    entry["half"] = half
                self.data["absences"][d.isoformat()] = entry
            self.touch()
            return len(days)

    def remove_absences(self, start: date, end: date, source: Optional[str] = None) -> int:
        with self.lock:
            n = 0
            for d in daterange(start, end):
                a = self.data["absences"].get(d.isoformat())
                if a and (source is None or a.get("source") == source):
                    del self.data["absences"][d.isoformat()]
                    n += 1
            if n:
                self.touch()
            return n

    def replace_external_absences(self, start: date, end: date, entries: dict, source: str = "personio") -> int:
        """Ersetzt alle Abwesenheiten einer Quelle im Zeitraum. Manuelle Einträge haben Vorrang."""
        with self.lock:
            for d in daterange(start, end):
                a = self.data["absences"].get(d.isoformat())
                if a and a.get("source") == source:
                    del self.data["absences"][d.isoformat()]
            n = 0
            for d, e in entries.items():
                key = d.isoformat()
                if key in self.data["absences"]:
                    continue
                entry = {"kind": e["kind"], "portion": e["portion"], "source": source, "note": e.get("note", "")}
                if e.get("half") and e["portion"] < 1:
                    entry["half"] = e["half"]
                self.data["absences"][key] = entry
                n += 1
            self.touch()
            return n

    def absence_groups(self, year: Optional[int] = None) -> list:
        """Fasst zusammenhängende Abwesenheiten zu Zeiträumen zusammen.
        Halbe Tage können nur am Anfang oder Ende eines Zeitraums stehen."""
        items = sorted(self.data["absences"].items())
        groups: list = []
        for key, a in items:
            d = parse_date(key)
            if year and d.year != year:
                continue
            p = float(a.get("portion", 1.0))
            sig = (a["kind"], a.get("source"), a.get("note", ""))
            if groups:
                g = groups[-1]
                can_extend = g["end_portion"] >= 1 or g["start"] == g["end"]
                if g["sig"] == sig and can_extend and self._only_free_days_between(g["end"], d):
                    g["end"] = d
                    g["end_portion"] = p
                    g["end_half"] = a.get("half")
                    g["days"] += p
                    continue
            groups.append({"start": d, "end": d, "kind": a["kind"], "start_portion": p, "end_portion": p,
                           "start_half": a.get("half"), "end_half": a.get("half"),
                           "source": a.get("source", "manual"), "note": a.get("note", ""),
                           "days": p, "sig": sig})
        return groups

    def _only_free_days_between(self, a: date, b: date) -> bool:
        d = a + timedelta(days=1)
        while d < b:
            if self.is_workday(d) or d.isoformat() in self.data["absences"]:
                return False
            d += timedelta(days=1)
        return True

    def vacation_stats(self, year: int, today: Optional[date] = None) -> dict:
        today = today or date.today()
        taken = planned = sick = 0.0
        for key, a in self.data["absences"].items():
            d = parse_date(key)
            if d.year != year:
                continue
            p = float(a.get("portion", 1.0))
            if a["kind"] == "urlaub":
                if d <= today:
                    taken += p
                else:
                    planned += p
            elif a["kind"] == "krank":
                sick += p
        entitlement = float(self.settings.get("vacation_days_per_year", 0)) + \
            float(self.settings.get("vacation_carryover", 0))
        return {"entitlement": entitlement, "taken": taken, "planned": planned,
                "rest": entitlement - taken - planned, "sick": sick}

    # ------------------------------------------------------------ Berechnungen
    def worked_by_day(self, now: Optional[datetime] = None) -> dict:
        """Gearbeitete Minuten je Arbeitstag. Arbeit über Mitternacht zählt zum Tag, an dem sie begann."""
        now = now or now_dt()
        res: dict = defaultdict(float)
        with self.lock:
            sessions = [(self.session_workday(s), s["start"], s["end"]) for s in self.data["sessions"]]
        for wd, st_s, en_s in sessions:
            st = parse_dt(st_s)
            en = parse_dt(en_s) if en_s else now
            if en > st:
                res[wd] += (en - st).total_seconds() / 60.0
        return res

    def balance_before(self, d: date, wbd: Optional[dict] = None) -> float:
        """Überstundensaldo (Minuten) aller Tage vor d inkl. Startsaldo."""
        key = (d, self.version)
        if key in self._balance_cache:
            return self._balance_cache[key]
        wbd = wbd if wbd is not None else self.worked_by_day()
        total = float(self.settings.get("initial_balance_minutes", 0))
        for day in daterange(self.tracking_start, d - timedelta(days=1)):
            total += wbd.get(day, 0.0) - self.target_minutes(day)
        self._balance_cache[key] = total
        return total

    def pause_on(self, d: date, now: Optional[datetime] = None, wbd: Optional[dict] = None) -> float:
        now = now or now_dt()
        first = last = None
        for s in self.day_sessions(d):
            st = parse_dt(s["start"])
            en = parse_dt(s["end"]) if s["end"] else now
            first = st if first is None or st < first else first
            last = en if last is None or en > last else last
        if first is None:
            return 0.0
        wbd = wbd if wbd is not None else self.worked_by_day(now)
        return max(0.0, (last - first).total_seconds() / 60.0 - wbd.get(d, 0.0))

    def first_last(self, d: date, now: Optional[datetime] = None):
        sess = self.day_sessions(d)
        if not sess:
            return None, None, False
        first = parse_dt(sess[0]["start"])
        open_ = any(s["end"] is None for s in sess)
        ends = [parse_dt(s["end"]) for s in sess if s["end"]]
        return first, (max(ends) if ends else None), open_

    def status(self, now: Optional[datetime] = None) -> Status:
        now = now or now_dt()
        with self.lock:
            today = self.current_workday(now)
            wbd = self.worked_by_day(now)
            mode = self.mode()
            since = None
            if mode == "working":
                since = parse_dt(self.open_session()["start"])
            elif mode == "paused":
                since = self.paused_since()
            return Status(now=now, workday=today, mode=mode, since=since,
                          worked=wbd.get(today, 0.0), target=self.target_minutes(today),
                          pause_today=self.pause_on(today, now, wbd),
                          balance_before=self.balance_before(today, wbd))

    def period(self, start: date, end: date, now: Optional[datetime] = None, wbd: Optional[dict] = None) -> dict:
        now = now or now_dt()
        wbd = wbd if wbd is not None else self.worked_by_day(now)
        today = self.logical_date(now)
        worked = target_to_date = target_total = 0.0
        for d in daterange(start, end):
            worked += wbd.get(d, 0.0)
            t = self.target_minutes(d)
            target_total += t
            if d <= today:
                target_to_date += t
        return {"worked": worked, "target": target_to_date, "target_total": target_total,
                "diff": worked - target_to_date}

    def balance_series(self, start: date, end: date, now: Optional[datetime] = None) -> dict:
        """Saldo-Verlauf Tag für Tag (bis höchstens heute).

        Liefert {'start_balance': Saldo vor dem ersten Tag, 'days': [{date, worked, target, diff, balance}]},
        wobei balance der Saldo am Ende des jeweiligen Tages ist.
        """
        now = now or now_dt()
        today = self.logical_date(now)
        start = max(start, self.tracking_start)
        end = min(end, today)
        wbd = self.worked_by_day(now)
        bal = self.balance_before(start, wbd) if start <= end else 0.0
        out = {"start_balance": bal, "days": [], "start": start, "end": end}
        for d in daterange(start, end):
            w, t = wbd.get(d, 0.0), self.target_minutes(d)
            bal += w - t
            out["days"].append({"date": d, "worked": w, "target": t, "diff": w - t, "balance": bal})
        return out

    # ------------------------------------------------------------ Personio-Export-Hilfen
    def pending_exports(self, from_date: date) -> list:
        with self.lock:
            return [copy.deepcopy(s) for s in self.data["sessions"]
                    if s["end"] and parse_dt(s["start"]).date() >= from_date
                    and (not s.get("personio_id") or s.get("dirty"))]

    def mark_exported(self, session_id: str, personio_id: str) -> None:
        with self.lock:
            for s in self.data["sessions"]:
                if s["id"] == session_id:
                    s["personio_id"] = personio_id
                    s["dirty"] = False
            self.save()

    def take_deleted(self) -> list:
        with self.lock:
            ids = list(self.data["personio_deleted"])
            self.data["personio_deleted"] = []
            self.save()
            return ids

    def requeue_deleted(self, ids: list) -> None:
        with self.lock:
            self.data["personio_deleted"].extend(ids)
            self.save()
