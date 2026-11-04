"""Sicherung und Abgleich von data.json mit OneDrive (privates Microsoft-Konto) über Microsoft Graph.

Voraussetzung: eine eigene App-Registrierung im Microsoft-Entra-Portal (siehe README):
  * Kontotyp „Nur persönliche Microsoft-Konten“
  * Plattform „Öffentlicher Client/nativ“, Umleitungs-URI http://localhost
  * „Öffentliche Clientflows zulassen“ = Ja
  * Delegierte Berechtigungen: Files.ReadWrite.AppFolder, offline_access

Die Datei liegt im App-Ordner („Apps/<App-Name>/data.json“) – der ZeitTracker sieht nur diesen Ordner.
Der Anmelde-Cache wird mit Windows-DPAPI verschlüsselt in onedrive_token.bin gespeichert.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import requests

import winutil
from core import Store, iso, now_dt

AUTHORITY = "https://login.microsoftonline.com/consumers"
# Eingebaute App-Registrierung (kein Geheimnis – öffentliche Client-ID). In den Einstellungen überschreibbar.
DEFAULT_CLIENT_ID = "3648689d-6632-4211-85d4-8ce97035f174"


def effective_client_id(cfg: dict) -> str:
    return (cfg.get("client_id") or "").strip() or DEFAULT_CLIENT_ID
SCOPES = ["Files.ReadWrite.AppFolder"]
GRAPH = "https://graph.microsoft.com/v1.0"
REMOTE = f"{GRAPH}/me/drive/special/approot:/data.json"


class OneDriveError(Exception):
    pass


class Conflict(OneDriveError):
    """Lokal und in der Cloud wurde seit dem letzten Abgleich geändert."""

    def __init__(self, remote_modified: str = "", reason: str = ""):
        self.remote_modified = remote_modified
        self.reason = reason
        super().__init__(reason or "Konflikt zwischen lokalem Stand und OneDrive-Stand.")


# --------------------------------------------------------------------------- Anmeldung (MSAL)

class Auth:
    def __init__(self, client_id: str, cache_path: Path):
        if not client_id:
            raise OneDriveError("Bitte zuerst die Anwendungs-ID (Client-ID) deiner App-Registrierung eintragen.")
        import msal  # erst hier, damit der Rest auch ohne msal läuft
        self.cache_path = cache_path
        self.cache = msal.SerializableTokenCache()
        if cache_path.exists():
            try:
                self.cache.deserialize(winutil.unprotect(cache_path.read_bytes()).decode("utf-8"))
            except Exception:
                pass
        self.app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=self.cache, timeout=10)
        self._lock = threading.Lock()

    def _save(self) -> None:
        if self.cache.has_state_changed:
            self.cache_path.write_bytes(winutil.protect(self.cache.serialize().encode("utf-8")))

    def account(self) -> Optional[str]:
        accs = self.app.get_accounts()
        return accs[0].get("username") if accs else None

    def token(self) -> str:
        """Zugriffstoken ohne Rückfrage (erneuert sich selbst über das Refresh-Token)."""
        with self._lock:
            accs = self.app.get_accounts()
            if not accs:
                raise OneDriveError("Nicht bei OneDrive angemeldet.")
            res = self.app.acquire_token_silent(SCOPES, account=accs[0])
            self._save()
            if not res or "access_token" not in res:
                raise OneDriveError("Die OneDrive-Anmeldung ist abgelaufen – bitte in den Einstellungen neu anmelden.")
            return res["access_token"]

    def login_interactive(self) -> str:
        """Öffnet den Browser für die Microsoft-Anmeldung. Blockiert bis zur Anmeldung (max. 5 Min.)."""
        with self._lock:
            res = self.app.acquire_token_interactive(SCOPES, prompt="select_account", timeout=300)
            self._save()
        if "access_token" not in res:
            raise OneDriveError(f"Anmeldung fehlgeschlagen: {res.get('error_description') or res.get('error')}")
        return (res.get("id_token_claims") or {}).get("preferred_username") or self.account() or "angemeldet"

    def logout(self) -> None:
        with self._lock:
            for a in self.app.get_accounts():
                self.app.remove_account(a)
            self._save()
        try:
            self.cache_path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- Abgleich

def content_hash(data: dict) -> str:
    """Prüfsumme über die Daten – ohne den minütlichen Lebenszeichen-Zeitstempel."""
    d = copy.deepcopy(data)
    d.get("state", {}).pop("last_heartbeat", None)
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def is_fresh(data: dict) -> bool:
    """Noch keine eigenen Daten erfasst (z. B. ZeitTracker auf einem neuen PC)."""
    return not data.get("sessions") and not data.get("absences")


class SyncState:
    """Lokaler Abgleichsstand (nicht Teil von data.json, damit er nicht mit hochgeladen wird)."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"etag": None, "hash": None, "last_sync": None, "last_result": "", "conflict": None}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text("utf-8")))
            except Exception:
                pass

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), "utf-8")

    def __getitem__(self, k):
        return self.data.get(k)

    def __setitem__(self, k, v):
        self.data[k] = v


class OneDriveSync:
    def __init__(self, store: Store, token: Callable[[], str], state: Optional[SyncState] = None,
                 session: Optional[requests.Session] = None, timeout: int = 20):
        self.store = store
        self.token = token
        self.state = state or SyncState(store.path.parent / "onedrive_state.json")
        self.http = session or requests.Session()
        self.timeout = timeout
        self.lock = threading.Lock()
        self.last_action = None          # "upload" | "download" | "none" | "conflict"
        self.last_remote_modified = ""

    # ------------------------------------------------------------ HTTP
    def _req(self, method: str, url: str, **kw) -> requests.Response:
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token()}"
        try:
            return self.http.request(method, url, headers=headers, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise OneDriveError(f"OneDrive nicht erreichbar: {e}") from e

    def remote_meta(self) -> Optional[dict]:
        r = self._req("GET", REMOTE, params={"select": "eTag,lastModifiedDateTime,size"})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise OneDriveError(f"OneDrive-Fehler {r.status_code}: {r.text[:200]}")
        return r.json()

    def download(self) -> tuple:
        meta = self.remote_meta()
        if meta is None:
            raise OneDriveError("Auf OneDrive liegt noch kein Stand.")
        r = self._req("GET", REMOTE + ":/content")
        if r.status_code >= 400:
            raise OneDriveError(f"Download fehlgeschlagen ({r.status_code}).")
        try:
            data = json.loads(r.content.decode("utf-8"))
        except ValueError as e:
            raise OneDriveError("Der Stand auf OneDrive ist keine gültige ZeitTracker-Datei.") from e
        if not isinstance(data, dict) or "sessions" not in data:
            raise OneDriveError("Der Stand auf OneDrive ist keine gültige ZeitTracker-Datei.")
        return data, meta

    def upload(self, if_match: Optional[str], force: bool = False) -> dict:
        with self.store.lock:
            body = json.dumps(self.store.data, indent=1, ensure_ascii=False).encode("utf-8")
            h = content_hash(self.store.data)
        headers = {"Content-Type": "application/json"}
        if not force:
            if if_match:
                headers["If-Match"] = if_match
            else:
                headers["If-None-Match"] = "*"
        r = self._req("PUT", REMOTE + ":/content", data=body, headers=headers)
        self.last_action = "upload"
        if r.status_code in (409, 412):
            raise Conflict(reason="Der Stand auf OneDrive wurde inzwischen von einem anderen Gerät geändert.")
        if r.status_code >= 400:
            raise OneDriveError(f"Upload fehlgeschlagen ({r.status_code}): {r.text[:200]}")
        meta = r.json()
        self.state["etag"] = meta.get("eTag")
        self.state["hash"] = h
        return meta

    def _apply_remote(self, data: dict, meta: dict) -> None:
        self.last_action = "download"
        self.last_remote_modified = meta.get("lastModifiedDateTime", "")
        self.store.replace_data(data)
        self.state["etag"] = meta.get("eTag")
        self.state["hash"] = content_hash(self.store.data)

    def _finish(self, msg: str, conflict: Optional[dict] = None) -> str:
        self.state["last_sync"] = iso(now_dt())
        self.state["last_result"] = msg
        self.state["conflict"] = conflict
        self.state.save()
        return msg

    # ------------------------------------------------------------ Hauptlogik
    def sync(self) -> str:
        """Automatischer Abgleich. Wirft Conflict, wenn beide Seiten geändert wurden."""
        with self.lock:
            self.last_action = "none"
            if self.state["conflict"]:
                raise Conflict(self.state["conflict"].get("remote_modified", ""), "Ein Konflikt ist noch offen.")
            meta = self.remote_meta()
            local_h = content_hash(self.store.data)
            local_changed = local_h != self.state["hash"]
            if meta is None:
                self.upload(None)
                return self._finish(f"Erste Sicherung auf OneDrive angelegt ({now_dt():%d.%m.%Y %H:%M}).")
            remote_changed = meta.get("eTag") != self.state["etag"]
            if self.state["etag"] is None:                 # erster Abgleich auf diesem PC
                if is_fresh(self.store.data):
                    data, meta = self.download()
                    self._apply_remote(data, meta)
                    return self._finish("Daten von OneDrive übernommen (erster Abgleich auf diesem PC).")
                return self._conflict(meta, "Auf OneDrive liegt bereits ein Stand, und hier gibt es eigene Daten.")
            if remote_changed and not local_changed:
                data, meta = self.download()
                self._apply_remote(data, meta)
                return self._finish("Neuerer Stand von OneDrive übernommen.")
            if remote_changed and local_changed:
                return self._conflict(meta, "Sowohl hier als auch auf einem anderen Gerät wurde geändert.")
            if local_changed:
                try:
                    self.upload(self.state["etag"])
                except Conflict:
                    return self._conflict(self.remote_meta() or {}, "Der Stand auf OneDrive wurde inzwischen geändert.")
                return self._finish(f"Auf OneDrive gesichert ({now_dt():%d.%m.%Y %H:%M}).")
            return self._finish("OneDrive ist aktuell.")

    def _conflict(self, meta: dict, reason: str) -> str:
        self.last_action = "conflict"
        info = {"remote_modified": meta.get("lastModifiedDateTime", ""), "reason": reason}
        self._finish(f"⚠ Konflikt: {reason}", conflict=info)
        raise Conflict(info["remote_modified"], reason)

    def resolve(self, use: str) -> str:
        """use = 'local' (hiesigen Stand hochladen) oder 'remote' (OneDrive-Stand übernehmen).
        Der jeweils verworfene Stand wird als Sicherungsdatei aufbewahrt."""
        with self.lock:
            stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
            bdir = self.store.path.parent / "backups"
            bdir.mkdir(exist_ok=True)
            if use == "remote":
                data, meta = self.download()
                (bdir / f"data-lokal-vor-onedrive-{stamp}.json").write_text(
                    json.dumps(self.store.data, indent=1, ensure_ascii=False), "utf-8")
                self._apply_remote(data, meta)
                msg = "OneDrive-Stand übernommen. Der bisherige lokale Stand liegt im Ordner backups."
            else:
                try:
                    data, _ = self.download()
                    (bdir / f"data-onedrive-ueberschrieben-{stamp}.json").write_text(
                        json.dumps(data, indent=1, ensure_ascii=False), "utf-8")
                except OneDriveError:
                    pass
                self.upload(None, force=True)
                msg = "Lokaler Stand auf OneDrive hochgeladen. Der vorherige OneDrive-Stand liegt im Ordner backups."
            return self._finish(msg, conflict=None)
