import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import personio  # noqa: E402
from core import Store  # noqa: E402


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.content = b"x" if payload is not None else b""
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._p


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, url, data=None, timeout=None):
        assert url.endswith("/v2/auth/token") and data["grant_type"] == "client_credentials"
        return FakeResp(200, {"access_token": "tok", "expires_in": 86400})

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json))
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("/v2/persons"):
            return FakeResp(200, {"_data": [{"id": "42"}]})
        if url.endswith("/v2/absence-types"):
            return FakeResp(200, {"_data": [
                {"id": "T1", "name": "Urlaub", "category": "PAID_VACATION", "unit": "DAY"},
                {"id": "T2", "name": "Krankheit", "category": "SICK_LEAVE", "unit": "HOUR"}]})
        if url.endswith("/v2/absence-periods"):
            return FakeResp(200, {"_data": [
                {"id": "A1", "absence_type": {"id": "T1"}, "approval": {"status": "APPROVED"},
                 "starts_from": {"date_time": "2026-11-09T00:00:00", "type": "FIRST_HALF"},
                 "ends_at": {"date_time": "2026-11-10T00:00:00", "type": "FIRST_HALF"}},
                {"id": "A4", "absence_type": {"id": "T1"}, "approval": {"status": "APPROVED"},
                 "starts_from": {"date_time": "2026-11-20T00:00:00", "type": "SECOND_HALF"},
                 "ends_at": {"date_time": "2026-11-20T00:00:00", "type": "SECOND_HALF"}},
                {"id": "A2", "absence_type": {"id": "T2"}, "approval": {"status": "APPROVED"}},
                {"id": "A3", "absence_type": {"id": "T1"}, "approval": {"status": "PENDING"}}],
                "_meta": {"links": {"next": {"href": "https://api.personio.de/v2/absence-periods?cursor=c2"}}}})
        if "cursor=c2" in url:
            return FakeResp(200, {"_data": [], "_meta": {"links": {}}})
        if url.endswith("/A1/breakdowns"):
            return FakeResp(200, {"_data": [
                {"date": "2026-11-09", "effective_duration": {"value": 1.0, "unit": "DAY"}},
                {"date": "2026-11-10", "effective_duration": {"value": 0.5, "unit": "DAY"}},
                {"date": "2026-11-14", "effective_duration": {"value": 0, "unit": "DAY"}}]})
        if url.endswith("/A4/breakdowns"):
            return FakeResp(200, {"_data": [
                {"date": "2026-11-20", "effective_duration": {"value": 0.5, "unit": "DAY"}}]})
        if url.endswith("/A2/breakdowns"):
            return FakeResp(200, {"_data": [
                {"date": "2026-11-03", "effective_duration": {"value": 480, "unit": "MINUTE"}}]})
        if url.endswith("/v2/attendance-periods") and method == "POST":
            return FakeResp(201, {"id": "ATT1", "affected_periods": []})
        raise AssertionError(f"unerwartet: {method} {url}")


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "data.json")
    s.settings["tracking_start"] = "2026-01-01"
    p = s.settings["personio"]
    p.update(enabled=True, client_id="cid", email="me@example.com", export_attendance=True,
             export_from="2026-11-01")
    s.touch()
    monkeypatch.setattr(personio.requests, "Session", FakeSession)
    return s


def test_disabled_by_default(tmp_path):
    s = Store(tmp_path / "d.json")
    assert s.settings["personio"]["enabled"] is False
    assert personio.run_sync(s, "x") == "Personio-Anbindung ist deaktiviert."


def test_full_sync(store):
    store.data["sessions"].append(store._new_session(datetime(2026, 11, 2, 8), datetime(2026, 11, 2, 16)))
    store.set_absences(date(2026, 11, 10), date(2026, 11, 10), "krank")  # manuell hat Vorrang
    store.touch()
    report = personio.run_sync(store, "secret")
    ab = store.data["absences"]
    assert ab["2026-11-09"] == {"kind": "urlaub", "portion": 1.0, "source": "personio", "note": "Urlaub"}
    assert ab["2026-11-10"]["source"] == "manual"
    assert ab["2026-11-20"]["portion"] == 0.5 and ab["2026-11-20"]["half"] == "pm"   # nur Nachmittag
    assert ab["2026-11-03"]["kind"] == "krank" and ab["2026-11-03"]["portion"] == 1.0
    assert "2026-11-14" not in ab
    assert store.settings["personio"]["person_id"] == "42"
    assert store.data["sessions"][0]["personio_id"] == "ATT1"
    assert "1 neu" in report
    # zweiter Lauf exportiert nichts doppelt
    report2 = personio.run_sync(store, "secret")
    assert "0 neu" in report2


def test_classification():
    assert personio.classify_absence_type({"name": "Sonderurlaub", "category": "PAID_VACATION"}) == "sonstiges"
    assert personio.classify_absence_type({"name": "Überstundenabbau"}) == "fza"
    assert personio.classify_absence_type({"name": "Kind krank"}) == "krank"


def test_half_day_part_mapping():
    d1, d2 = date(2026, 11, 9), date(2026, 11, 13)
    f = personio.half_day_part
    assert f(d1, d1, d2, "SECOND_HALF", "SECOND_HALF") == "pm"   # Urlaub beginnt mittags
    assert f(d2, d1, d2, "FIRST_HALF", "FIRST_HALF") == "am"     # Urlaub endet mittags
    assert f(d1, d1, d1, "FIRST_HALF", "FIRST_HALF") == "am"     # nur Vormittag
    assert f(d1, d1, d1, "SECOND_HALF", "SECOND_HALF") == "pm"   # nur Nachmittag
