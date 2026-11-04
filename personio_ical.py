"""Import von Abwesenheiten aus dem Personio-Kalender-Abo (iCal-Link) – ohne API-Zugang.

Den Link erzeugt jeder Mitarbeiter selbst in Personio:
  Abwesenheit & Anwesenheit › Team-Kalender › ⋮ › Abonnieren › „Mein Kalender“ › Link kopieren

Einträge sehen typischerweise so aus: "[Urlaub] Max Mustermann". Ganztägige Einträge werden
tageweise übernommen; Einträge mit Uhrzeiten werden als halbe Tage erkannt (vormittags/nachmittags).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional

import requests

from core import Store, daterange, iso, now_dt, parse_date
from personio import classify_absence_type

KEYRING_SERVICE = "ZeitTracker-PersonioKalender"
SOURCE = "ical"


# --------------------------------------------------------------------------- Link sicher speichern

def get_url(store: Store) -> str:
    try:
        import keyring  # type: ignore
        val = keyring.get_password(KEYRING_SERVICE, "url")
        if val:
            return val
    except Exception:
        pass
    return store.settings.get("ical", {}).get("url_fallback", "")


def set_url(store: Store, url: str) -> None:
    cfg = store.settings.setdefault("ical", {})
    try:
        import keyring  # type: ignore
        if url:
            keyring.set_password(KEYRING_SERVICE, "url", url)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, "url")
            except Exception:
                pass
        cfg.pop("url_fallback", None)
    except Exception:
        cfg["url_fallback"] = url


# --------------------------------------------------------------------------- ICS lesen

def _unfold(text: str) -> list:
    lines: list = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_dt(value: str, params: dict):
    """-> (datetime|date, is_all_day). UTC-Zeiten ('Z') werden in lokale Zeit umgerechnet."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return date(int(v[:4]), int(v[4:6]), int(v[6:8])), True
    m = re.fullmatch(r"(\d{8})T(\d{2})(\d{2})(\d{2})?(Z?)", v)
    if not m:
        raise ValueError(f"Unbekanntes Datumsformat: {v}")
    d = m.group(1)
    dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
    if m.group(5) == "Z":
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return dt, False


def parse_ics(text: str) -> list:
    """Minimaler, robuster ICS-Parser: liefert eine Liste von Ereignis-Dicts."""
    events, cur = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        parts = head.split(";")
        name = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')
        if name in ("DTSTART", "DTEND"):
            try:
                cur[name], cur[name + "_ALLDAY"] = _parse_dt(value, params)
            except ValueError:
                pass
        elif name in ("SUMMARY", "DESCRIPTION", "STATUS", "UID", "CATEGORIES"):
            cur[name] = _unescape(value)
    return events


GENERIC_TYPES = {"abwesenheit", "abwesenheiten", "absence", "absences", "abwesend", "out of office"}
HALF_WORDS = ("halb", "half")

# Einträge, die keine Abwesenheit sind (z. B. aus „Mein Kalender“)
NOT_ABSENCE_WORDS = ("geburtstag", "birthday", "jubiläum", "jubilaeum", "anniversary", "jahrestag",
                     "eintritt", "hire date", "first day", "erster arbeitstag", "work anniversary",
                     "🎂", "🎉")


def split_summary(summary: str) -> tuple:
    """'[Urlaub] Max Mustermann' -> ('Urlaub', 'Max Mustermann'). Zusätze in Klammern bleiben am Namen."""
    s = summary.strip()
    m = re.match(r"^\[(.+?)\]\s*(.*)$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.*?)\s*\[(.+?)\]$", s)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    for sep in (" - ", " – ", ": "):
        if sep in s:
            a, b = s.split(sep, 1)
            return b.strip(), a.strip()
    return s, ""


def parse_summary(summary: str) -> dict:
    """Zerlegt Personio-Einträge wie
    '[Urlaub] Max Mustermann (½ letzter Tag)' oder
    '[Abwesenheit] Erika Muster, Vertretung: Max Mustermann (½ erster Tag)'."""
    typ, rest = split_summary(summary)
    half_info = ""
    m = re.search(r"\(([^)]*(?:½|halb|half)[^)]*)\)\s*$", rest, re.I)
    if m:
        half_info = m.group(1).lower()
        rest = rest[: m.start()].strip()
    person, substitute = rest, ""
    m = re.match(r"^(.*?),\s*(?:Vertretung|Vertreter(?:in)?|Substitute|Cover)\s*:\s*(.*)$", rest, re.I)
    if m:
        person, substitute = m.group(1).strip(), m.group(2).strip()
    first = bool(half_info) and ("erst" in half_info or "first" in half_info)
    last = bool(half_info) and ("letzt" in half_info or "last" in half_info)
    return {"type": typ, "person": person, "substitute": substitute, "half_any": bool(half_info),
            "first_half": first, "last_half": last, "generic": typ.strip().lower() in GENERIC_TYPES}


def is_absence(summary: str, typ: str) -> bool:
    text = f"{summary} {typ}".lower()
    return not any(w in text for w in NOT_ABSENCE_WORDS)


def event_days(ev: dict) -> list:
    """Zerlegt ein Ereignis in [(date, portion, half)] – half: 'am' (Vormittag frei) / 'pm' / None."""
    start, end = ev.get("DTSTART"), ev.get("DTEND")
    if start is None:
        return []
    info = parse_summary(ev.get("SUMMARY", ""))
    if ev.get("DTSTART_ALLDAY"):
        last = (end - timedelta(days=1)) if isinstance(end, date) and not isinstance(end, datetime) and end > start else start
        days = list(daterange(start, last))
        out = [(d, 1.0, None) for d in days]
        if not out:
            return out
        if len(out) == 1:
            if info["half_any"]:
                # einzelner Tag: „½ letzter Tag“ = endet mittags -> Vormittag frei; „½ erster Tag“ = Nachmittag frei
                out[0] = (days[0], 0.5, "am" if info["last_half"] else "pm" if info["first_half"] else None)
            return out
        if info["first_half"]:
            out[0] = (days[0], 0.5, "pm")          # beginnt mittags
        if info["last_half"]:
            out[-1] = (days[-1], 0.5, "am")        # endet mittags
        return out

    # Termin mit Uhrzeiten (andere Kalender-Formate)
    end = end or (start + timedelta(hours=4))
    out = []
    for d in daterange(start.date(), (end - timedelta(seconds=1)).date()):
        lo = max(start, datetime.combine(d, time()))
        hi = min(end, datetime.combine(d + timedelta(days=1), time()))
        hours = (hi - lo).total_seconds() / 3600
        full_span = lo.time() == time(0) and hi == datetime.combine(d + timedelta(days=1), time())
        if full_span or (hours >= 5 and not info["half_any"]):
            out.append((d, 1.0, None))
        else:
            mid = lo + (hi - lo) / 2
            out.append((d, 0.5, "am" if mid.time() < time(12, 30) else "pm"))
    return out


# --------------------------------------------------------------------------- Abruf & Import

def fetch(url: str, timeout: int = 25) -> str:
    if not url:
        raise ValueError("Kein Kalender-Link hinterlegt.")
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    r = requests.get(url, timeout=timeout, headers={"Accept": "text/calendar, */*"})
    if r.status_code != 200:
        raise ValueError(f"Kalender konnte nicht geladen werden (HTTP {r.status_code}).")
    r.encoding = r.encoding or "utf-8"
    text = r.text
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("Der Link liefert keinen Kalender (kein iCal-Format). Bitte den Abo-Link aus Personio verwenden.")
    return text


GENERIC_KIND_DEFAULT = "sonstiges"


def build_entries(store: Store, events: list, name_filter: str = "") -> tuple:
    """-> (entries {date: {...}}, info dict).

    * Nur eigene Einträge (Name exakt, Vertretungen anderer werden ignoriert).
    * Einträge mit konkreter Art (z. B. [Urlaub]) haben Vorrang vor allgemeinen ([Abwesenheit]).
    * Nur Arbeitstage ohne Feiertag werden übernommen.
    """
    cfg = store.settings.get("ical", {})
    generic_kind = cfg.get("generic_kind", GENERIC_KIND_DEFAULT)
    nf = re.sub(r"\s+", " ", name_filter.strip().lower())
    specific: dict = {}
    generic: dict = {}
    names, skipped, cancelled, non_absence, substitutes = set(), 0, 0, 0, 0
    for ev in events:
        if (ev.get("STATUS") or "").upper() == "CANCELLED":
            cancelled += 1
            continue
        summary = ev.get("SUMMARY", "")
        info = parse_summary(summary)
        if not is_absence(summary, info["type"]):
            non_absence += 1
            continue
        if info["person"]:
            names.add(info["person"])
        person = re.sub(r"\s+", " ", info["person"].lower())
        if nf and person != nf:
            if nf in (info["substitute"] or "").lower():
                substitutes += 1
            else:
                skipped += 1
            continue
        if info["generic"]:
            if generic_kind == "ignore":
                continue
            kind, note, target = generic_kind, "Abwesenheit (Art unbekannt)", generic
        else:
            kind = classify_absence_type({"name": info["type"], "category": ev.get("CATEGORIES", "")})
            note, target = info["type"], specific
        for d, portion, half in event_days(ev):
            if not store.is_workday(d) or store.holiday_name(d):
                continue
            prev = target.get(d)
            if prev is None or portion > prev["portion"]:
                target[d] = {"kind": kind, "portion": portion, "note": note, "half": half}
    entries = dict(generic)
    entries.update(specific)                 # konkrete Art gewinnt
    return entries, {"names": sorted(names), "skipped_other_people": skipped, "cancelled": cancelled,
                     "non_absence": non_absence, "substitutes": substitutes,
                     "specific_days": len(specific), "generic_only_days": len(set(generic) - set(specific))}


def coverage(events: list) -> tuple:
    """Zeitfenster (erster bis letzter Tag), das der Kalender abdeckt – über alle Personen."""
    days = []
    for ev in events:
        st, en = ev.get("DTSTART"), ev.get("DTEND")
        if st is None:
            continue
        st_d = st.date() if isinstance(st, datetime) else st
        if en is None:
            en_d = st_d
        elif isinstance(en, datetime):
            en_d = en.date()
        else:
            en_d = en - timedelta(days=1) if en > st_d else st_d
        days += [st_d, en_d]
    return min(days), max(days)


def generic_label(kind: str) -> str:
    from core import ABSENCE_KINDS
    return "ignoriert" if kind == "ignore" else ABSENCE_KINDS.get(kind, {}).get("label", kind)


def run_import(store: Store, url: str) -> str:
    cfg = store.settings.setdefault("ical", {})
    events = parse_ics(fetch(url))
    entries, info = build_entries(store, events, cfg.get("name_filter", ""))
    if not cfg.get("name_filter", "").strip() and len(info["names"]) > 1:
        msg = (f"Der Kalender enthält Einträge von {len(info['names'])} Personen. Bitte in den Einstellungen "
               "deinen Namen genau so eintragen, wie er in Personio steht – es wurde nichts übernommen.")
        with store.lock:
            cfg["last_sync"] = iso(now_dt())
            cfg["last_result"] = msg
            store.save()
        raise ValueError(msg)
    if not events:
        raise ValueError("Der Kalender enthält keine Einträge – nichts geändert. Ist der Link noch gültig?")
    lo, hi = coverage(events)
    # Nur innerhalb des Zeitfensters, das der Kalender abdeckt, abgleichen. Ältere Einträge, die Personio
    # nicht mehr mitschickt, bleiben erhalten.
    entries = {d: e for d, e in entries.items() if lo <= d <= hi}
    already = sum(1 for d in entries
                  if (a := store.data["absences"].get(d.isoformat())) and a.get("source") != SOURCE)
    n = store.replace_external_absences(lo, hi, entries, source=SOURCE)
    kept_old = sum(1 for k, a in store.data["absences"].items() if a.get("source") == SOURCE and parse_date(k) < lo)
    lines = [f"Kalender deckt {lo:%d.%m.%Y} – {hi:%d.%m.%Y} ab ({len(events)} Einträge gelesen).",
             f"{len(entries)} eigene Abwesenheitstage gefunden: {n} aus dem Kalender übernommen, "
             f"{already} waren schon eingetragen (manuell/Import – haben Vorrang)."]
    if kept_old:
        lines.append(f"{kept_old} ältere Kalender-Tage vor {lo:%d.%m.%Y} bleiben erhalten.")
    if cfg.get("name_filter") and not (info["specific_days"] or info["generic_only_days"]):
        lines.append(f"Keine Einträge für „{cfg['name_filter']}“ gefunden – stimmt die Schreibweise?")
    if info["generic_only_days"]:
        lines.append(f"{info['generic_only_days']} Tage ohne erkennbare Art (nur „[Abwesenheit]“) als "
                     f"„{generic_label(cfg.get('generic_kind', GENERIC_KIND_DEFAULT))}“ übernommen.")
    if info["substitutes"]:
        lines.append(f"{info['substitutes']} Einträge, bei denen du nur Vertretung bist, ignoriert.")
    if info["non_absence"]:
        lines.append(f"{info['non_absence']} Einträge ohne Abwesenheit (z. B. Geburtstage, Jubiläen) ignoriert.")
    if info["skipped_other_people"]:
        lines.append(f"{info['skipped_other_people']} Einträge anderer Personen ignoriert.")
    with store.lock:
        cfg["last_sync"] = iso(now_dt())
        cfg["last_result"] = " ".join(lines)[:500]
        cfg.pop("names_seen", None)
        store.save()
    return "\n".join(lines)
