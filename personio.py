"""Anbindung an die Personio Public API (v2).

Benötigte API-Zugangsdaten (Personio > Einstellungen > Integrationen > API-Zugangsdaten):
  * Personen lesen            (personio:persons:read)
  * Abwesenheiten lesen       (personio:absences:read)
  * Anwesenheiten lesen/schreiben (personio:attendances:read / :write) – nur für den Export

Die Integration ist standardmäßig deaktiviert und wird nur aktiv, wenn sie in den
Einstellungen eingeschaltet wird.
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import requests

from core import Store, daterange, iso, now_dt, parse_date, parse_dt

BASE = "https://api.personio.de"
KEYRING_SERVICE = "ZeitTracker-Personio"


class PersonioError(Exception):
    pass


# --------------------------------------------------------------------------- Geheimnis speichern

def get_secret(store: Store) -> str:
    cid = store.settings["personio"].get("client_id", "")
    try:
        import keyring  # type: ignore
        val = keyring.get_password(KEYRING_SERVICE, cid or "default")
        if val:
            return val
    except Exception:
        pass
    return store.settings["personio"].get("client_secret_fallback", "")


def set_secret(store: Store, secret: str) -> str:
    """Speichert das Secret in der Windows-Anmeldeinformationsverwaltung (keyring)."""
    cid = store.settings["personio"].get("client_id", "")
    try:
        import keyring  # type: ignore
        keyring.set_password(KEYRING_SERVICE, cid or "default", secret)
        store.settings["personio"].pop("client_secret_fallback", None)
        return "keyring"
    except Exception:
        store.settings["personio"]["client_secret_fallback"] = secret
        return "datei"


# --------------------------------------------------------------------------- HTTP-Client

def _error_text(r: requests.Response) -> str:
    try:
        j = r.json()
        if "errors" in j and j["errors"]:
            return "; ".join(f"{e.get('title', '')}: {e.get('detail', '')}".strip(": ") for e in j["errors"])
        if "error_description" in j:
            return j["error_description"]
    except Exception:
        pass
    return r.text[:300]


class PersonioClient:
    def __init__(self, client_id: str, client_secret: str, timeout: int = 25):
        if not client_id or not client_secret:
            raise PersonioError("Client-ID und Client-Secret müssen in den Einstellungen hinterlegt sein.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "X-Personio-App-ID": "ZEITTRACKER_DESKTOP"})
        self._token: Optional[str] = None
        self._token_exp = 0.0

    def _auth(self) -> str:
        if self._token and _time.time() < self._token_exp - 60:
            return self._token
        r = self.session.post(f"{BASE}/v2/auth/token", timeout=self.timeout, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        if r.status_code != 200:
            raise PersonioError(f"Anmeldung bei Personio fehlgeschlagen ({r.status_code}): {_error_text(r)}")
        j = r.json()
        self._token = j["access_token"]
        self._token_exp = _time.time() + int(j.get("expires_in", 86400))
        return self._token

    def request(self, method: str, path_or_url: str, params: Optional[dict] = None,
                body: Optional[dict] = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        r = None
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._auth()}"}
            r = self.session.request(method, url, params=params, json=body, headers=headers, timeout=self.timeout)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code == 429:
                _time.sleep(min(30, int(r.headers.get("Retry-After", "5") or 5)))
                continue
            break
        if r.status_code >= 400:
            raise PersonioError(f"{method} {url.replace(BASE, '')} → {r.status_code}: {_error_text(r)}")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def paginate(self, path: str, params: Optional[dict] = None) -> Iterator[dict]:
        data = self.request("GET", path, params)
        seen = set()
        while True:
            yield from data.get("_data", [])
            nxt = (data.get("_meta") or {}).get("links", {}).get("next", {}) or {}
            href = nxt.get("href")
            if not href or href in seen:
                return
            seen.add(href)
            data = self.request("GET", href)

    # ------------------------------------------------------------ Endpunkte
    def find_person_id(self, email: str) -> str:
        if not email:
            raise PersonioError("Bitte die E-Mail-Adresse deines Personio-Kontos in den Einstellungen angeben.")
        data = self.request("GET", "/v2/persons", {"email": email, "limit": 1})
        items = data.get("_data", [])
        if not items:
            raise PersonioError(f"Keine Person mit der E-Mail {email} in Personio gefunden.")
        return str(items[0]["id"])

    def absence_types(self) -> dict:
        return {t["id"]: t for t in self.paginate("/v2/absence-types", {"limit": 100})}

    def absence_periods(self, person_id: str, start: date, end: date) -> list:
        return list(self.paginate("/v2/absence-periods", {
            "person.id": person_id,
            "limit": 100,
            "starts_from.date_time.lte": f"{end.isoformat()}T23:59:59Z",
            "ends_at.date_time.gte": f"{start.isoformat()}T00:00:00Z",
        }))

    def breakdowns(self, period_id: str) -> list:
        return list(self.paginate(f"/v2/absence-periods/{period_id}/breakdowns", {"limit": 28}))

    def create_attendance(self, person_id: str, start: datetime, end: datetime, comment: str = "") -> str:
        body = {"person": {"id": person_id}, "type": "WORK",
                "start": {"date_time": iso(start)}, "end": {"date_time": iso(end)}}
        if comment:
            body["comment"] = comment
        res = self.request("POST", "/v2/attendance-periods", body=body)
        return str(res.get("id"))

    def update_attendance(self, period_id: str, start: datetime, end: datetime) -> None:
        self.request("PATCH", f"/v2/attendance-periods/{period_id}", body={
            "start": {"date_time": iso(start)}, "end": {"date_time": iso(end)}})

    def delete_attendance(self, period_id: str) -> None:
        self.request("DELETE", f"/v2/attendance-periods/{period_id}")


# --------------------------------------------------------------------------- Zuordnung

def classify_absence_type(t: Optional[dict]) -> str:
    t = t or {}
    cat = (t.get("category") or "").upper()
    name = (t.get("name") or "").lower()
    if "SICK" in cat or "krank" in name or "sick" in name or "kind krank" in name:
        return "krank"
    if any(k in name for k in ("überstunden", "ueberstunden", "freizeitausgleich", "overtime", "gleitzeit", "zeitausgleich")) \
            or "OVERTIME" in cat:
        return "fza"
    if any(k in name for k in ("sonderurlaub", "bildungsurlaub", "bildungszeit", "bildungsfreistellung",
                                "elternzeit", "parental", "mutterschutz", "fortbildung", "weiterbildung",
                                "schulung", "training", "dienstreise", "business trip", "unbezahlt", "unpaid")):
        return "sonstiges"
    if "VACATION" in cat or "urlaub" in name or "vacation" in name or "holiday" in name:
        return "urlaub"
    return "sonstiges"


# --------------------------------------------------------------------------- Synchronisation

def half_day_part(d: date, first: date, last: date, start_type: Optional[str], end_type: Optional[str]) -> Optional[str]:
    """Welche Tageshälfte ist frei? 'am' = vormittags, 'pm' = nachmittags, None = unbekannt.

    Personio: starts_from.type = SECOND_HALF -> Abwesenheit beginnt mittags (Nachmittag frei),
              ends_at.type     = FIRST_HALF  -> Abwesenheit endet mittags (Vormittag frei).
    """
    st = (start_type or "").upper()
    en = (end_type or "").upper()
    if first == last:
        if st == "SECOND_HALF":
            return "pm"
        if en == "FIRST_HALF":
            return "am"
        return None
    if d == first and st == "SECOND_HALF":
        return "pm"
    if d == last and en == "FIRST_HALF":
        return "am"
    return None


def import_absences(store: Store, client: PersonioClient, person_id: str,
                    start: date, end: date) -> int:
    p = store.settings["personio"]
    types = client.absence_types()
    allowed = {"APPROVED"} | ({"PENDING"} if p.get("include_pending") else set())
    entries: dict = {}
    for period in client.absence_periods(person_id, start, end):
        status = ((period.get("approval") or {}).get("status") or "APPROVED").upper()
        if status not in allowed:
            continue
        ttype = types.get((period.get("absence_type") or {}).get("id"))
        kind = classify_absence_type(ttype)
        label = (ttype or {}).get("name", "")
        start_type = (period.get("starts_from") or {}).get("type")
        end_type = (period.get("ends_at") or {}).get("type")
        days = []
        for b in client.breakdowns(period["id"]):
            try:
                days.append((parse_date(b["date"]), b.get("effective_duration") or {}))
            except Exception:
                continue
        if not days:
            continue
        days.sort(key=lambda x: x[0])
        first, last = days[0][0], days[-1][0]
        for d, dur in days:
            if not (start <= d <= end):
                continue
            val = float(dur.get("value") or 0)
            if val <= 0:
                continue
            is_day_unit = (dur.get("unit") or "DAY").upper() != "MINUTE"
            if not is_day_unit:
                base = store.base_target(d)
                portion = 1.0 if base <= 0 else min(1.0, val / base)
            else:
                portion = min(1.0, val)
            portion = round(portion, 2)
            half = half_day_part(d, first, last, start_type, end_type) if is_day_unit and portion < 1 else None
            prev = entries.get(d)
            if prev:
                prev["portion"] = min(1.0, prev["portion"] + portion)
                prev["half"] = None if prev["portion"] >= 1 or prev.get("half") != half else half
            else:
                entries[d] = {"kind": kind, "portion": portion, "note": label, "half": half}
    return store.replace_external_absences(start, end, entries, source="personio")


def export_attendance(store: Store, client: PersonioClient, person_id: str) -> tuple:
    p = store.settings["personio"]
    try:
        from_date = parse_date(p.get("export_from") or date.today().isoformat())
    except ValueError:
        from_date = date.today()
    created = updated = deleted = 0
    errors: list = []

    ids = store.take_deleted()
    failed = []
    for pid in ids:
        try:
            client.delete_attendance(pid)
            deleted += 1
        except PersonioError as e:
            if "404" in str(e):
                continue
            failed.append(pid)
            errors.append(str(e))
    if failed:
        store.requeue_deleted(failed)

    for s in store.pending_exports(from_date):
        st, en = parse_dt(s["start"]), parse_dt(s["end"])
        if en - st > timedelta(hours=24):
            errors.append(f"Sitzung {st:%d.%m.%Y %H:%M} ist länger als 24 h und wurde nicht übertragen.")
            continue
        try:
            if s.get("personio_id"):
                client.update_attendance(s["personio_id"], st, en)
                store.mark_exported(s["id"], s["personio_id"])
                updated += 1
            else:
                new_id = client.create_attendance(person_id, st, en, "ZeitTracker")
                store.mark_exported(s["id"], new_id)
                created += 1
        except PersonioError as e:
            errors.append(str(e))
    return created, updated, deleted, errors


def run_sync(store: Store, secret: str) -> str:
    """Führt eine vollständige Synchronisation aus und liefert einen Bericht."""
    p = store.settings["personio"]
    if not p.get("enabled"):
        return "Personio-Anbindung ist deaktiviert."
    client = PersonioClient(p.get("client_id", ""), secret)
    person_id = p.get("person_id") or client.find_person_id(p.get("email", ""))
    if person_id != p.get("person_id"):
        with store.lock:
            p["person_id"] = person_id
            store.save()

    lines = []
    if p.get("import_absences", True):
        today = date.today()
        start = max(store.tracking_start, date(today.year - 1, 1, 1))
        end = date(today.year + 1, 12, 31)
        n = import_absences(store, client, person_id, start, end)
        lines.append(f"{n} Abwesenheitstage aus Personio übernommen.")
    if p.get("export_attendance"):
        c, u, d, errs = export_attendance(store, client, person_id)
        lines.append(f"Arbeitszeiten: {c} neu, {u} aktualisiert, {d} gelöscht.")
        lines.extend(errs[:5])
        if len(errs) > 5:
            lines.append(f"… und {len(errs) - 5} weitere Fehler.")

    with store.lock:
        p["last_sync"] = iso(now_dt())
        p["last_result"] = " ".join(lines)[:500]
        store.save()
    return "\n".join(lines) or "Nichts zu tun."


def test_connection(client_id: str, secret: str, email: str) -> str:
    client = PersonioClient(client_id, secret)
    pid = client.find_person_id(email)
    return pid
