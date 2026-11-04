import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import Store, fmt_hm, iso, parse_clock, parse_hm  # noqa: E402

MON = date(2026, 11, 2)  # Montag


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data.json")
    s.settings["tracking_start"] = MON.isoformat()
    s.touch()
    return s


def dt(d, hm):
    h, m = map(int, hm.split(":"))
    return datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m)


def test_parse_and_format():
    assert parse_hm("7:30") == 450
    assert parse_hm("7,5") == 450
    assert parse_hm("-1:15") == -75
    assert parse_hm("+12:30h") == 750
    assert fmt_hm(-75) == "-1:15"
    assert fmt_hm(75, True) == "+1:15"
    assert parse_clock("0805", MON) == dt(MON, "08:05")
    assert parse_clock("24:00", MON) == datetime.combine(MON + timedelta(days=1), datetime.min.time())


def test_targets_and_absences(store):
    assert store.target_minutes(MON) == 480
    assert store.target_minutes(MON + timedelta(days=5)) == 0  # Samstag
    n = store.set_absences(MON, MON + timedelta(days=6), "urlaub")
    assert n == 5  # nur Arbeitstage
    assert store.target_minutes(MON) == 0
    store.set_absences(MON + timedelta(days=1), MON + timedelta(days=1), "urlaub", 0.5)
    assert store.target_minutes(MON + timedelta(days=1)) == 240
    store.set_absences(MON + timedelta(days=2), MON + timedelta(days=2), "fza")
    assert store.target_minutes(MON + timedelta(days=2)) == 480  # FZA verbraucht Überstunden
    groups = store.absence_groups(2026)
    assert [g["kind"] for g in groups] == ["urlaub", "fza", "urlaub"]   # Mo + halber Di = ein Zeitraum
    vs = store.vacation_stats(2026, today=MON + timedelta(days=10))
    assert vs["taken"] == 3.5


def test_balance_and_status(store):
    # Montag 9h, Dienstag 7h -> Saldo +0:00 ; Mittwoch läuft
    store.data["sessions"] += [
        store._new_session(dt(MON, "08:00"), dt(MON, "12:00")),
        store._new_session(dt(MON, "12:30"), dt(MON, "17:30")),
        store._new_session(dt(MON + timedelta(1), "08:00"), dt(MON + timedelta(1), "15:00")),
    ]
    store.settings["initial_balance_minutes"] = 60
    store.touch()
    wed = MON + timedelta(days=2)
    store.start_work(dt(wed, "08:00"))
    st = store.status(dt(wed, "15:30"))
    assert st.worked == 450
    assert st.balance_before == 60 + 60 - 60
    assert not st.hands_ok and st.ring_ok
    assert st.remaining == 30 and st.remaining_with_balance == 0
    assert st.mode == "working"
    st2 = store.status(dt(wed, "16:00"))
    assert st2.hands_ok and st2.ring_ok
    assert store.pause_on(MON, dt(wed, "16:00")) == 30


def test_pause_flow_and_midnight(store):
    d = MON
    store.start_work(dt(d, "20:00"))
    store.start_pause(dt(d, "22:00"))
    assert store.mode() == "paused"
    store.end_pause(dt(d, "22:30"))
    store.stop_work(dt(d + timedelta(1), "01:00"))
    wbd = store.worked_by_day(dt(d + timedelta(1), "02:00"))
    # Arbeit über Mitternacht zählt komplett zum Starttag
    assert wbd[d] == 270 and wbd.get(d + timedelta(1), 0) == 0
    assert store.mode() == "idle"


def test_gap_handling(store):
    store.start_work(dt(MON, "08:00"))
    msg = store.handle_gap(dt(MON, "13:00"), dt(MON, "12:00"))
    assert "Pause" in msg and store.mode() == "working"     # Lücke = Pause, Erfassung läuft weiter
    assert store.pause_on(MON, dt(MON, "13:30")) == 60
    msg = store.handle_gap(dt(MON + timedelta(1), "08:00"), dt(MON, "17:00"))
    assert "automatisch" in msg and store.mode() == "idle"


def test_replace_day_sessions_marks_personio(store):
    store.data["sessions"].append(store._new_session(dt(MON, "08:00"), dt(MON, "16:00")))
    store.touch()
    sid = store.day_sessions(MON)[0]["id"]
    store.mark_exported(sid, "P1")
    store.replace_day_sessions(MON, [{"id": sid, "start": dt(MON, "08:00"), "end": dt(MON, "16:30")}])
    assert store.day_sessions(MON)[0]["dirty"] is True
    assert len(store.pending_exports(MON)) == 1
    store.replace_day_sessions(MON, [])
    assert store.take_deleted() == ["P1"]


def test_schedule_versions(store):
    days = {str(i): {"active": i < 4, "hours": 8.0} for i in range(7)}  # 4-Tage-Woche
    store.set_schedule(days, MON + timedelta(days=7))
    assert store.target_minutes(MON + timedelta(days=4)) == 480          # Freitag alte Regel
    assert store.target_minutes(MON + timedelta(days=11)) == 0           # Freitag neue Regel


def test_persistence(tmp_path):
    s = Store(tmp_path / "data.json")
    s.start_work(datetime(2026, 11, 4, 8, 0))
    s2 = Store(tmp_path / "data.json")
    assert s2.is_working()


def test_lock_pause_and_unlock(store):
    store.start_work(dt(MON, "08:00"))
    assert store.lock_pause(dt(MON, "10:00"))
    assert store.mode() == "paused"
    assert store.unlock_resume(dt(MON, "10:15"))
    assert store.mode() == "working"
    # manuelle Pause wird beim Entsperren NICHT beendet
    store.start_pause(dt(MON, "12:00"))
    assert not store.unlock_resume(dt(MON, "12:05"))
    assert store.mode() == "paused"
    # Sperren ohne laufende Arbeit macht nichts
    store.stop_work(dt(MON, "12:06"))
    assert not store.lock_pause(dt(MON, "13:00"))
    assert store.mode() == "idle"


def test_shutdown_same_day_is_pause(store):
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "12:00"))
    assert store.mode() == "paused"
    msg = store.resume_after_shutdown(dt(MON, "12:45"))
    assert "Pause" in msg and store.mode() == "working"
    st = store.status(dt(MON, "13:45"))
    assert st.worked == 300 and st.pause_today == 45


def test_shutdown_next_day_ends_workday(store):
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "17:00"))
    assert store.resume_after_shutdown(dt(MON + timedelta(1), "08:00")) is None
    assert store.mode() == "idle"
    assert store.worked_by_day()[MON] == 540


def test_shutdown_mode_stop(store):
    store.settings["shutdown_mode"] = "stop"
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "12:00"))
    assert store.mode() == "idle"


def test_overnight_workday(store):
    d = MON
    store.start_work(dt(d, "18:00"))
    st = store.status(dt(d + timedelta(1), "01:00"))
    assert st.workday == d and st.worked == 420            # 18:00-01:00 gehört zum Montag
    store.start_pause(dt(d + timedelta(1), "01:00"))
    assert store.current_workday(dt(d + timedelta(1), "01:10")) == d
    store.end_pause(dt(d + timedelta(1), "01:15"))          # Pause nach Mitternacht -> gleicher Arbeitstag
    store.stop_work(dt(d + timedelta(1), "02:00"))
    wbd = store.worked_by_day()
    assert wbd[d] == 465 and wbd.get(d + timedelta(1), 0) == 0
    assert store.current_workday(dt(d + timedelta(1), "08:00")) == d + timedelta(1)
    assert store.status(dt(d + timedelta(1), "08:00")).worked == 0


def test_pause_over_day_boundary_ends_workday(store):
    d = MON
    store.start_work(dt(d, "15:00"))
    store.start_pause(dt(d, "23:00"))
    store.handle_gap(dt(d + timedelta(1), "00:00"), dt(d, "23:59"))   # Mitternacht: Arbeitstag läuft noch
    assert store.mode() == "paused"
    store.handle_gap(dt(d + timedelta(1), "05:00"), dt(d + timedelta(1), "04:59"))   # Tageswechsel 5 Uhr
    assert store.mode() == "idle"
    store.start_work(dt(d + timedelta(1), "08:00"))
    assert store.open_session()["workday"] == (d + timedelta(1)).isoformat()


def test_holidays_rp(store):
    store.settings["holiday_region"] = "RP"
    store.touch()
    allerheiligen = date(2026, 11, 1)                      # Sonntag
    fronleichnam = date(2026, 6, 4)                         # Donnerstag, RP-Feiertag
    store.settings["tracking_start"] = "2026-01-01"
    assert store.holiday_name(fronleichnam)
    assert store.target_minutes(fronleichnam) == 0
    assert store.holiday_name(allerheiligen)
    store.settings["holiday_region"] = "BE"; store._holiday_cache.clear()
    assert store.holiday_name(fronleichnam) is None
    assert store.target_minutes(fronleichnam) == 480


def test_parse_plus_one():
    assert parse_clock("00:30+1", MON) == dt(MON + timedelta(1), "00:30")
    assert parse_clock("00:30 (+1)", MON) == dt(MON + timedelta(1), "00:30")


def test_quick_restart_continues_work(store):
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "11:00"))
    msg = store.resume_after_shutdown(dt(MON, "11:06"))       # innerhalb 10 min
    assert "ohne Unterbrechung" in msg and store.mode() == "working"
    st = store.status(dt(MON, "12:00"))
    assert st.worked == 240 and st.pause_today == 0           # Neustartzeit zählt als Arbeit
    assert len(store.day_sessions(MON)) == 1


def test_quick_restart_also_with_stop_mode(store):
    store.settings["shutdown_mode"] = "stop"
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "11:00"))
    store.resume_after_shutdown(dt(MON, "11:03"))
    assert store.mode() == "working" and store.status(dt(MON, "11:30")).worked == 210


def test_slow_restart_is_pause(store):
    store.settings["restart_grace_minutes"] = 5
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "11:00"))
    store.resume_after_shutdown(dt(MON, "11:20"))
    st = store.status(dt(MON, "12:00"))
    assert st.mode == "working" and st.worked == 220 and st.pause_today == 20


def test_half_days_only_first_and_last(store):
    # Mo–Fr, erster und letzter Tag halbtags -> 0,5 + 1 + 1 + 1 + 0,5 = 4 Tage
    n = store.set_absences(MON, MON + timedelta(days=4), "urlaub", first_half=True, last_half=True)
    assert n == 5
    ab = store.data["absences"]
    assert [ab[(MON + timedelta(i)).isoformat()]["portion"] for i in range(5)] == [0.5, 1, 1, 1, 0.5]
    g = store.absence_groups(2026)
    assert len(g) == 1 and g[0]["days"] == 4 and g[0]["start_portion"] == 0.5 and g[0]["end_portion"] == 0.5


def test_half_days_skip_weekend_edges(store):
    # Sa–Fr mit "nur Arbeitstage": halber Tag landet auf dem ersten echten Urlaubstag (Montag)
    sat = MON - timedelta(days=2)
    store.set_absences(sat, MON + timedelta(days=4), "urlaub", first_half=True)
    assert store.data["absences"][MON.isoformat()]["portion"] == 0.5
    assert store.data["absences"][(MON + timedelta(4)).isoformat()]["portion"] == 1.0


def test_single_day_half(store):
    store.set_absences(MON, MON, "urlaub", first_half=True)
    assert store.data["absences"][MON.isoformat()]["portion"] == 0.5
    assert store.target_minutes(MON) == 240


def test_half_day_in_middle_splits_group(store):
    store.set_absences(MON, MON + timedelta(days=4), "urlaub")
    store.data["absences"][(MON + timedelta(2)).isoformat()]["portion"] = 0.5   # z. B. aus Import
    store.touch()
    g = store.absence_groups(2026)
    assert [(x["start"].day, x["end"].day) for x in g] == [(2, 4), (5, 6)]


def test_half_day_parts(store):
    store.set_absences(MON, MON + timedelta(days=4), "urlaub", first_half=True, last_half=True)
    ab = store.data["absences"]
    assert ab[MON.isoformat()]["half"] == "pm"                    # erster Tag: ab mittags frei
    assert ab[(MON + timedelta(4)).isoformat()]["half"] == "am"   # letzter Tag: bis mittags frei
    assert "half" not in ab[(MON + timedelta(2)).isoformat()]
    store.set_absences(MON + timedelta(7), MON + timedelta(7), "urlaub", single_half="am")
    a = ab[(MON + timedelta(7)).isoformat()]
    assert a["portion"] == 0.5 and a["half"] == "am"


def test_balance_series(store):
    store.settings["initial_balance_minutes"] = 30
    for i, (a, b) in enumerate([("08:00", "17:00"), ("08:00", "15:00"), ("08:00", "16:00")]):
        d = MON + timedelta(i)
        store.data["sessions"].append(store._new_session(dt(d, a), dt(d, b)))
    store.touch()
    now = dt(MON + timedelta(6), "12:00")
    s = store.balance_series(MON + timedelta(1), MON + timedelta(30), now)
    assert s["start_balance"] == 30 + 60                       # Startsaldo + Montag
    assert [x["balance"] for x in s["days"]][:2] == [30, 30]    # Di -1h, Mi ±0
    assert s["end"] == MON + timedelta(6)                       # nicht über heute hinaus
    assert s["days"][-1]["balance"] == 30 - 2 * 480             # Do+Fr ohne Arbeit


def test_shutdown_pause_until_end_of_day_is_not_counted(store):
    store.start_work(dt(MON, "08:00"))
    store.start_pause(dt(MON, "12:00")); store.end_pause(dt(MON, "12:30"))
    store.shutdown(dt(MON, "17:00"))
    st = store.status(dt(MON, "21:00"))                   # PC bleibt aus, Abend
    assert st.worked == 510 and st.pause_today == 30      # Pause nach 17:00 zählt nicht
    assert store.first_last(MON)[1] == dt(MON, "17:00")   # Tagesende = Herunterfahren
    store.handle_gap(dt(MON + timedelta(1), "08:00"), dt(MON, "17:00"))
    assert store.mode() == "idle" and store.pause_on(MON) == 30


def test_feierabend_right_after_auto_resume(store):
    store.start_work(dt(MON, "08:00"))
    store.shutdown(dt(MON, "17:00"))
    store.resume_after_shutdown(dt(MON, "20:00"))          # abends kurz hochgefahren
    assert store.auto_resumed_session()["resumed_after"] == iso(dt(MON, "17:00"))
    assert store.end_day_at_pause_start() == dt(MON, "17:00")
    assert store.mode() == "idle"
    assert store.first_last(MON)[1] == dt(MON, "17:00") and store.pause_on(MON) == 0
    assert store.worked_by_day()[MON] == 540


def _late_night(store):
    store.settings["tracking_start"] = "2026-11-01"
    store.touch()
    return MON, MON + timedelta(1)


def _check(store, mon, tue, mon_pause=0):
    now = dt(tue, "12:00")
    w = store.worked_by_day(now)
    assert store.first_last(mon)[1] == dt(tue, "02:00")
    assert w[mon] == 18 * 60 - mon_pause and store.pause_on(mon, now) == mon_pause
    assert store.first_last(tue)[0] == dt(tue, "09:00")
    assert w[tue] == 180 and store.pause_on(tue, now) == 0


def test_late_night_feierabend(store):
    mon, tue = _late_night(store)
    store.start_work(dt(mon, "08:00")); store.stop_work(dt(tue, "02:00"))
    store.handle_gap(dt(tue, "09:00"), dt(tue, "08:59")); store.start_work(dt(tue, "09:00"))
    _check(store, mon, tue)


def test_late_night_shutdown(store):
    mon, tue = _late_night(store)
    store.start_work(dt(mon, "08:00")); store.shutdown(dt(tue, "02:00"))
    store.handle_gap(dt(tue, "09:00"), store.last_heartbeat())
    assert store.resume_after_shutdown(dt(tue, "09:00")) is None   # nicht fortsetzen, neuer Tag
    assert store.mode() == "idle"
    store.start_work(dt(tue, "09:00"))
    _check(store, mon, tue)


def test_late_night_manual_pause(store):
    mon, tue = _late_night(store)
    store.start_work(dt(mon, "08:00")); store.start_pause(dt(tue, "02:00"))
    store.handle_gap(dt(tue, "09:00"), dt(tue, "08:59"))           # Tick nach Tageswechsel
    assert store.mode() == "idle"
    store.start_work(dt(tue, "09:00"))
    _check(store, mon, tue)


def test_late_night_pause_before_midnight(store):
    mon, tue = _late_night(store)
    store.start_work(dt(mon, "08:00")); store.start_pause(dt(mon, "23:30"))
    store.handle_gap(dt(tue, "00:00"), dt(mon, "23:59"))
    store.end_pause(dt(tue, "00:30")); store.stop_work(dt(tue, "02:00"))
    store.start_work(dt(tue, "09:00"))
    _check(store, mon, tue, mon_pause=60)


def test_day_boundary_setting(store):
    store.settings["day_boundary_hour"] = 0                        # Mitternacht
    assert store.logical_date(dt(MON + timedelta(1), "01:00")) == MON + timedelta(1)
    store.settings["day_boundary_hour"] = 5
    assert store.logical_date(dt(MON + timedelta(1), "04:59")) == MON
    assert store.logical_date(dt(MON + timedelta(1), "05:00")) == MON + timedelta(1)
