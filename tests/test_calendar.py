"""Kalender-Markierungen am Jahreswechsel (braucht Tk, wird ohne Anzeige übersprungen)."""
import sys
import tkinter as tk
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ui  # noqa: E402


@pytest.fixture
def cal():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("keine Anzeige für Tk")
    root.withdraw()
    c = ui.FixedCalendar(root, selectmode="none", locale="de_DE", firstweekday="monday",
                         showweeknumbers=True, year=2026, month=12, day=1)
    c.tag_config("krank", background="red")
    yield c
    root.destroy()


def styled(c, row, col):
    return c._calendar[row][col].cget("style").startswith("tag_")


def test_events_land_on_their_own_day_across_new_year(cal):
    cal.calevent_create(date(2026, 1, 5), "krank", tags=["krank"])   # Jan des gleichen Jahres
    cal.calevent_create(date(2027, 1, 4), "krank", tags=["krank"])   # sichtbar unten links
    # Dezember 2026: Raster beginnt Mo 30.11.2026, Zeile 5 = 04.–10.01.2027
    assert cal._get_day_coords(date(2026, 1, 5)) == (None, None)
    assert cal._get_day_coords(date(2027, 1, 4)) == (5, 0)
    assert styled(cal, 5, 0) and not styled(cal, 5, 1)
    assert not styled(cal, 4, 0)   # 28.12.2026


def test_week_numbers_include_week_53(cal):
    assert [lbl.cget("text") for lbl in cal._week_nbs] == ["49", "50", "51", "52", "53", "1"]
