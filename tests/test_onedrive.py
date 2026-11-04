import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import onedrive as od  # noqa: E402
from core import Store  # noqa: E402


class Resp:
    def __init__(self, status, body=b"", js=None):
        self.status_code = status
        self.content = body if body else (json.dumps(js).encode() if js is not None else b"")
        self.text = self.content.decode("utf-8", "replace")
        self._js = js

    def json(self):
        return self._js if self._js is not None else json.loads(self.content)


class FakeGraph:
    """Simuliert den OneDrive-App-Ordner mit eTag-Prüfung."""

    def __init__(self):
        self.content = None
        self.etag = None
        self.n = 0
        self.modified = "2026-11-04T10:00:00Z"

    def request(self, method, url, headers=None, timeout=None, params=None, data=None):
        assert headers["Authorization"] == "Bearer tok"
        assert url.startswith(od.REMOTE)
        is_content = url.endswith(":/content")
        if method == "GET" and not is_content:
            if self.content is None:
                return Resp(404, js={"error": {"code": "itemNotFound"}})
            return Resp(200, js={"eTag": self.etag, "lastModifiedDateTime": self.modified, "size": len(self.content)})
        if method == "GET" and is_content:
            return Resp(200, body=self.content)
        if method == "PUT":
            if headers.get("If-None-Match") == "*" and self.content is not None:
                return Resp(412)
            if "If-Match" in headers and headers["If-Match"] != self.etag:
                return Resp(412)
            self.content = data
            self.n += 1
            self.etag = f'"e{self.n}"'
            return Resp(201, js={"eTag": self.etag, "lastModifiedDateTime": self.modified})
        raise AssertionError(method)


def pc(tmp_path, name, graph):
    d = tmp_path / name
    d.mkdir()
    s = Store(d / "data.json")
    s.settings["tracking_start"] = "2026-11-01"
    s.touch()
    return s, od.OneDriveSync(s, lambda: "tok", session=graph)


def test_hash_ignores_heartbeat(tmp_path):
    s = Store(tmp_path / "d.json")
    h = od.content_hash(s.data)
    s.heartbeat(datetime(2026, 11, 4, 9, 0))
    assert od.content_hash(s.data) == h
    s.start_work(datetime(2026, 11, 4, 9, 0))
    assert od.content_hash(s.data) != h


def test_two_pcs_roundtrip(tmp_path):
    g = FakeGraph()
    a, sa = pc(tmp_path, "a", g)
    a.start_work(datetime(2026, 11, 4, 8, 0))
    a.stop_work(datetime(2026, 11, 4, 12, 0))
    assert "Erste Sicherung" in sa.sync()
    assert "aktuell" in sa.sync()                         # nichts geändert -> kein Upload
    assert g.n == 1

    b, sb = pc(tmp_path, "b", g)                          # neuer PC ohne Daten
    assert "übernommen" in sb.sync()
    assert b.worked_by_day()[date(2026, 11, 4)] == 240

    b.set_absences(date(2026, 11, 6), date(2026, 11, 6), "urlaub")
    assert "gesichert" in sb.sync()                       # B lädt hoch
    assert "Neuerer Stand" in sa.sync()                   # A holt sich den Stand von B
    assert "2026-11-06" in a.data["absences"]


def test_conflict_and_resolve(tmp_path):
    g = FakeGraph()
    a, sa = pc(tmp_path, "a", g)
    a.start_work(datetime(2026, 11, 4, 8, 0)); a.stop_work(datetime(2026, 11, 4, 12, 0))
    sa.sync()
    b, sb = pc(tmp_path, "b", g)
    sb.sync()
    a.set_absences(date(2026, 11, 9), date(2026, 11, 9), "urlaub"); sa.sync()       # A ändert + lädt hoch
    b.set_absences(date(2026, 11, 10), date(2026, 11, 10), "krank")                 # B ändert offline
    with pytest.raises(od.Conflict):
        sb.sync()
    with pytest.raises(od.Conflict):                       # bleibt offen bis entschieden
        sb.sync()
    msg = sb.resolve("remote")
    assert "übernommen" in msg and "2026-11-09" in b.data["absences"] and "2026-11-10" not in b.data["absences"]
    backups = list((b.path.parent / "backups").glob("data-lokal-vor-onedrive-*.json"))
    assert backups and "2026-11-10" in json.loads(backups[0].read_text("utf-8"))["absences"]
    assert "aktuell" in sb.sync()


def test_existing_data_on_new_pc_asks(tmp_path):
    g = FakeGraph()
    a, sa = pc(tmp_path, "a", g)
    a.start_work(datetime(2026, 11, 4, 8, 0)); a.stop_work(datetime(2026, 11, 4, 9, 0)); sa.sync()
    b, sb = pc(tmp_path, "b", g)
    b.set_absences(date(2026, 11, 12), date(2026, 11, 12), "urlaub")                  # B hat eigene Daten
    with pytest.raises(od.Conflict):
        sb.sync()
    sb.resolve("local")
    assert json.loads(g.content)["absences"].get("2026-11-12")
    assert list((b.path.parent / "backups").glob("data-onedrive-ueberschrieben-*.json"))


def test_invalid_remote_rejected(tmp_path):
    g = FakeGraph()
    g.content, g.etag = b'{"foo": 1}', '"x"'
    b, sb = pc(tmp_path, "b", g)
    with pytest.raises(od.OneDriveError, match="keine gültige"):
        sb.sync()
