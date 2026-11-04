import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import personio_ical as pi  # noqa: E402
from core import Store  # noqa: E402


def ev(start, end, summary):
    return (f"BEGIN:VEVENT\r\nUID:{abs(hash((start, summary)))}@personio.de\r\nDTSTAMP:20261104T100000Z\r\n"
            f"DTSTART;VALUE=DATE:{start}\r\nDTEND;VALUE=DATE:{end}\r\nSUMMARY:{summary}\r\nEND:VEVENT\r\n")


# Aufbau wie im echten Personio-Feed: „Abwesenheit“ (alle, ohne Art) + „Urlaub“ (mit Art) kombiniert
ICS = ("BEGIN:VCALENDAR\r\nPRODID:-//Personio//Firma iCal Feed absences//EN\r\nVERSION:2.0\r\n"
       "X-WR-CALNAME:Personio Firma Abwesenheit, Mein Kalender, Urlaub, Bildungsurlaub\r\n"
       + ev("20261116", "20261123", "[Urlaub] Max Mustermann")                          # Mo–So
       + ev("20261116", "20261123", "[Abwesenheit] Max Mustermann")                     # Duplikat
       + ev("20261127", "20261128", "[Urlaub] Max Mustermann (½ letzter Tag)")          # Fr vormittags frei
       + ev("20261127", "20261128", "[Abwesenheit] Max Mustermann (½ letzter Tag)")
       + ev("20261130", "20261203", "[Urlaub] Max Mustermann (½ erster Tag)")           # Mo ab mittags
       + ev("20261207", "20261212", "[Urlaub] Max Mustermann (½ letzter Tag)")          # Fr bis mittags
       + ev("20261104", "20261105", "[Abwesenheit] Max Mustermann")                     # Art unbekannt
       + ev("20261109", "20261111", "[Abwesenheit] Erika Muster, Vertretung: Max Mustermann")
       + ev("20261112", "20261113", "[Urlaub] Erika Muster")
       + ev("20261112", "20261113", "[Abwesenheit] Max Mustermann-Schmidt")             # ähnlicher Name
       + ev("20261214", "20261216", "[Bildungsurlaub] Max Mustermann")
       + ev("20261217", "20261218", "[Abwesenheit] Max  Mustermann(½ erster Tag)")      # ohne Leerzeichen
       + "END:VCALENDAR\r\n")


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = Store(tmp_path / "data.json")
    s.settings["tracking_start"] = "2026-01-01"
    s.settings["ical"]["name_filter"] = "Max Mustermann"
    s.touch()
    monkeypatch.setattr(pi, "fetch", lambda url: ICS)
    return s


def test_parse_summary():
    p = pi.parse_summary("[Abwesenheit] Erika Muster, Vertretung: Max Mustermann")
    assert p["person"] == "Erika Muster" and p["substitute"] == "Max Mustermann" and p["generic"]
    p = pi.parse_summary("[Urlaub] Max Mustermann (½ letzter Tag)")
    assert p == {"type": "Urlaub", "person": "Max Mustermann", "substitute": "", "half_any": True,
                 "first_half": False, "last_half": True, "generic": False}
    p = pi.parse_summary("[Abwesenheit] Kollege, Vertretung: Max (½ erster Tag)")
    assert p["person"] == "Kollege" and p["first_half"]


def test_import_personio_feed(store):
    report = pi.run_import(store, "x")
    ab = store.data["absences"]
    week = [k for k in sorted(ab) if "2026-11-16" <= k <= "2026-11-22"]
    assert week == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    assert all(ab[k]["kind"] == "urlaub" and ab[k]["portion"] == 1.0 for k in week)   # Duplikat nicht doppelt
    assert ab["2026-11-27"]["portion"] == 0.5 and ab["2026-11-27"]["half"] == "am" and ab["2026-11-27"]["kind"] == "urlaub"
    assert ab["2026-11-30"]["portion"] == 0.5 and ab["2026-11-30"]["half"] == "pm"
    assert ab["2026-12-01"]["portion"] == 1.0 and ab["2026-12-02"]["portion"] == 1.0
    assert ab["2026-12-11"]["portion"] == 0.5 and ab["2026-12-11"]["half"] == "am"     # Fr, Event bis Sa
    assert ab["2026-11-04"]["kind"] == "sonstiges" and "unbekannt" in ab["2026-11-04"]["note"]
    assert not any("2026-11-09" <= k <= "2026-11-10" for k in ab)                      # nur Vertretung
    assert "2026-11-12" not in ab                                                       # andere Personen
    assert ab["2026-12-14"]["kind"] == "sonstiges" and ab["2026-12-14"]["note"] == "Bildungsurlaub"
    assert ab["2026-12-17"]["portion"] == 0.5                                           # Name mit Doppel-Leerzeichen
    assert "Vertretung" in report and "ohne erkennbare Art" in report


def test_generic_kind_setting(store):
    store.settings["ical"]["generic_kind"] = "krank"
    pi.run_import(store, "x")
    assert store.data["absences"]["2026-11-04"]["kind"] == "krank"
    assert store.data["absences"]["2026-11-16"]["kind"] == "urlaub"   # konkrete Art gewinnt weiter
    store.settings["ical"]["generic_kind"] = "ignore"
    pi.run_import(store, "x")
    assert "2026-11-04" not in store.data["absences"]


def test_refuses_without_name_filter(store):
    store.settings["ical"]["name_filter"] = ""
    with pytest.raises(ValueError, match="nichts übernommen"):
        pi.run_import(store, "x")
    assert not store.data["absences"]


def test_reimport_replaces_and_respects_manual(store, monkeypatch):
    store.set_absences(date(2026, 11, 17), date(2026, 11, 17), "krank")
    pi.run_import(store, "x")
    assert store.data["absences"]["2026-11-17"]["source"] == "manual"
    shorter = ICS.replace("20261116\r\nDTEND;VALUE=DATE:20261123", "20261116\r\nDTEND;VALUE=DATE:20261119")
    monkeypatch.setattr(pi, "fetch", lambda url: shorter)
    pi.run_import(store, "x")
    assert "2026-11-20" not in store.data["absences"]


def test_holiday_and_weekend_skipped(store, monkeypatch):
    store.settings["holiday_region"] = "HE"
    store.touch()
    ics = ICS.replace("[Urlaub] Max Mustermann\r", "[Urlaub] X\r") + ""
    ics = ics.replace("END:VCALENDAR", ev("20261224", "20261228", "[Urlaub] Max Mustermann") + "END:VCALENDAR")
    monkeypatch.setattr(pi, "fetch", lambda url: ics)
    pi.run_import(store, "x")
    assert [k for k in store.data["absences"] if k.startswith("2026-12-2")] == ["2026-12-24"]


def test_skips_birthdays():
    assert not pi.is_absence("[Geburtstag] Max Mustermann", "Geburtstag")
    assert pi.is_absence("[Urlaub] Max Mustermann", "Urlaub")


def test_entries_outside_window_are_kept(store, monkeypatch):
    pi.run_import(store, "x")
    assert "2026-11-04" in store.data["absences"]
    # Personio schickt später nur noch Einträge ab Dezember – ältere fallen aus dem Fenster
    later = ("BEGIN:VCALENDAR\r\n" + ev("20261201", "20261203", "[Urlaub] Max Mustermann")
             + ev("20261220", "20261222", "[Abwesenheit] Erika Muster") + "END:VCALENDAR\r\n")
    monkeypatch.setattr(pi, "fetch", lambda url: later)
    report = pi.run_import(store, "x")
    ab = store.data["absences"]
    assert ab["2026-11-04"]["source"] == "ical" and ab["2026-11-16"]["kind"] == "urlaub"   # bleiben
    assert ab["2026-12-01"]["portion"] == 1.0
    assert "2026-12-11" not in ab and "2026-12-14" not in ab       # im Fenster, in Personio weg -> entfernt
    assert "bleiben erhalten" in report


def test_empty_feed_changes_nothing(store, monkeypatch):
    pi.run_import(store, "x")
    before = dict(store.data["absences"])
    monkeypatch.setattr(pi, "fetch", lambda url: "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    with pytest.raises(ValueError, match="nichts geändert"):
        pi.run_import(store, "x")
    assert store.data["absences"] == before
