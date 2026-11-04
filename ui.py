"""Fenster des ZeitTrackers (tkinter): Übersicht, Historie, Abwesenheiten, Einstellungen."""
from __future__ import annotations

import csv
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

from tkcalendar import Calendar, DateEntry

import personio
import personio_ical
import winutil
from core import (ABSENCE_KINDS, BUNDESLAENDER, HALF_LABELS, MONTHS, WEEKDAYS, WEEKDAYS_SHORT, _holidays,
                  daterange, fmt_date, fmt_days, fmt_hm, fmt_time, fmt_time_on, parse_clock, parse_date,
                  parse_dt, parse_hm)

PAD = 8
SCALE = 1.0          # Bildschirmskalierung (100 % = 1.0, 4K mit 200 % = 2.0) – wird in setup_style gesetzt


def px(n: float) -> int:
    """Pixelmaß an die Windows-Skalierung anpassen (wichtig für 4K-Bildschirme)."""
    return int(round(n * SCALE))
KIND_KEYS = list(ABSENCE_KINDS.keys())
KIND_LABELS = [ABSENCE_KINDS[k]["label"] for k in KIND_KEYS]
GENERIC_KIND_LABELS = {
    "sonstiges": "Sonstige Abwesenheit",
    "krank": "Krankheit",
    "urlaub": "Urlaub",
    "ignore": "nicht übernehmen",
}
SHUTDOWN_MODES = {
    "pause": "als Pause werten – bei Neustart am selben Tag automatisch weiter",
    "stop": "Feierabend buchen",
    "none": "nichts tun (Erfassung läuft weiter)",
}
SOURCE_LABELS = {"manual": "manuell", "personio": "Personio (API)", "ical": "Personio (Kalender)",
                 "auto": "automatisch"}


def setup_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
    style.configure("Big.TLabel", font=("Segoe UI", 11))
    style.configure("Muted.TLabel", foreground="#666666")
    global SCALE, PAD
    try:
        SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        SCALE = 1.0
    PAD = px(8)
    import tkinter.font as tkfont
    line = max(tkfont.nametofont("TkDefaultFont").metrics("linespace"),
               tkfont.Font(family="Segoe UI", size=10).metrics("linespace"))
    style.configure("Treeview", rowheight=max(px(24), line + px(8)))
    style.configure("Treeview.Heading", padding=(px(4), px(3)))


def date_entry(parent, value: date | None = None, **kw) -> DateEntry:
    de = DateEntry(parent, locale="de_DE", date_pattern="dd.mm.yyyy", firstweekday="monday",
                   showweeknumbers=True, width=12, **kw)
    if value:
        de.set_date(value)
    return de


def kind_label(kind: str) -> str:
    return ABSENCE_KINDS.get(kind, {}).get("label", kind)


def absence_text(a: dict | None) -> str:
    if not a:
        return ""
    txt = kind_label(a["kind"])
    p = float(a.get("portion", 1.0))
    if p < 1:
        if abs(p - 0.5) < 0.01:
            part = HALF_LABELS.get(a.get("half") or "")
            txt += f" (halbtags, {part} frei)" if part else " (halbtags)"
        else:
            txt += f" ({fmt_days(p)} Tag)"
    if a.get("note"):
        txt += f" – {a['note']}"
    return txt


def half_suffix(g: dict) -> str:
    first, last = g["start_portion"] < 1, g["end_portion"] < 1
    if g["start"] == g["end"]:
        part = HALF_LABELS.get(g.get("start_half") or "")
        return (f" (halbtags, {part} frei)" if part else " (halbtags)") if first else ""
    if first and last:
        return " (1. + letzter Tag halbtags)"
    if first:
        return " (1. Tag halbtags)"
    if last:
        return " (letzter Tag halbtags)"
    return ""


class BaseWindow(tk.Toplevel):
    key = ""

    def __init__(self, app, title: str):
        super().__init__(app.root)
        self.app = app
        self.store = app.store
        self.title(f"ZeitTracker – {title}")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())

    def close(self):
        self.app.windows.pop(self.key, None)
        self.destroy()

    def refresh(self):
        pass

    def bring_to_front(self, **kw):
        self.deiconify()
        self.lift()
        self.focus_force()


# =========================================================================== Übersicht

class OverviewWindow(BaseWindow):
    key = "overview"

    def __init__(self, app, **kw):
        super().__init__(app, "Übersicht")
        self.resizable(False, False)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)

        self.lbl_state = ttk.Label(f, style="Header.TLabel")
        self.lbl_state.grid(row=0, column=0, columnspan=4, sticky="w")

        bf = ttk.Frame(f)
        bf.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 10))
        self.btn_start = ttk.Button(bf, text="▶ Arbeit starten", command=lambda: app.do("start"))
        self.btn_pause = ttk.Button(bf, text="⏸ Pause starten", command=lambda: app.do("pause"))
        self.btn_resume = ttk.Button(bf, text="▶ Pause beenden", command=lambda: app.do("resume"))
        self.btn_stop = ttk.Button(bf, text="⏹ Feierabend", command=lambda: app.do("stop"))
        for i, b in enumerate((self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop)):
            b.grid(row=0, column=i, padx=(0, 6))

        self.lbl_today = ttk.Label(f, style="Big.TLabel", justify="left")
        self.lbl_today.grid(row=2, column=0, columnspan=4, sticky="w")
        self.lbl_balance_today = ttk.Label(f, style="Big.TLabel", justify="left")
        self.lbl_balance_today.grid(row=3, column=0, columnspan=4, sticky="w", pady=(2, 10))

        cols = ("Gearbeitet", "Soll", "Differenz")
        self.tree = ttk.Treeview(f, columns=cols, height=5, selectmode="none")
        self.tree.heading("#0", text="Zeitraum")
        self.tree.column("#0", width=px(230))
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=px(105), anchor="e")
        self.tree.tag_configure("neg", foreground="#b71c1c")
        self.tree.tag_configure("pos", foreground="#1b5e20")
        self.tree.grid(row=4, column=0, columnspan=4, sticky="ew")

        self.lbl_saldo = ttk.Label(f, style="Header.TLabel")
        self.lbl_saldo.grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.lbl_vac = ttk.Label(f, style="Muted.TLabel")
        self.lbl_vac.grid(row=6, column=0, columnspan=4, sticky="w", pady=(2, 10))

        nb = ttk.Frame(f)
        nb.grid(row=7, column=0, columnspan=4, sticky="w")
        ttk.Button(nb, text="Historie", command=lambda: app.open_window("history")).pack(side="left", padx=(0, 6))
        ttk.Button(nb, text="Urlaub & Krankheit", command=lambda: app.open_window("absences")).pack(side="left", padx=(0, 6))
        ttk.Button(nb, text="Einstellungen", command=lambda: app.open_window("settings")).pack(side="left")

        self.refresh()
        self._auto()

    def _auto(self):
        if self.winfo_exists():
            self.refresh()
            self.after(20_000, self._auto)

    def refresh(self):
        st = self.store.status()
        now, today = st.now, st.workday
        mode_txt = {"working": f"🟢 Arbeit läuft seit {fmt_time(st.since)}",
                    "paused": f"🟡 Pause seit {fmt_time(st.since)}",
                    "idle": "⚪ Nicht eingestempelt"}[st.mode]
        self.lbl_state.config(text=mode_txt)
        for b, show in ((self.btn_start, st.mode == "idle"), (self.btn_pause, st.mode == "working"),
                        (self.btn_resume, st.mode == "paused"), (self.btn_stop, st.mode != "idle")):
            b.state(["!disabled"] if show else ["disabled"])

        absence = self.store.absence_for(today)
        if st.target <= 0:
            line = f"Heute gearbeitet: {fmt_hm(st.worked)} h  –  kein Soll heute"
            if absence:
                line += f" ({absence_text(absence)})"
        else:
            line = f"Heute gearbeitet: {fmt_hm(st.worked)} h von {fmt_hm(st.target)} h   (Pause: {fmt_hm(st.pause_today)} h)"
            if st.remaining > 0:
                eta = st.eta(st.remaining)
                line += f"\nNoch {fmt_hm(st.remaining)} h" + (f"  →  Feierabend ca. {fmt_time(eta)} Uhr" if eta else "")
            else:
                line += "\n✓ Tagessoll durch heutige Arbeitszeit erfüllt"
        self.lbl_today.config(text=line)

        if st.target > 0:
            if st.remaining_with_balance > 0:
                eta = st.eta(st.remaining_with_balance)
                txt = f"Mit Überstunden-Saldo noch {fmt_hm(st.remaining_with_balance)} h" + \
                      (f"  →  ca. {fmt_time(eta)} Uhr" if eta else "")
            else:
                txt = "✓ Mit Überstunden-Saldo ist das Tagessoll bereits gedeckt"
        else:
            txt = ""
        self.lbl_balance_today.config(text=txt)

        wbd = self.store.worked_by_day(now)
        monday = today - timedelta(days=today.weekday())
        start_total = self.store.tracking_start
        rows = [
            ("Heute" if today == now.date() else f"Arbeitstag {fmt_date(today)} (über Mitternacht)", today, today),
            (f"Diese Woche (KW {today.isocalendar()[1]})", monday, monday + timedelta(days=6)),
            (f"{MONTHS[today.month - 1]} {today.year}", today.replace(day=1),
             (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)),
            (f"Jahr {today.year}", date(today.year, 1, 1), date(today.year, 12, 31)),
            (f"Gesamt seit {fmt_date(start_total)}", start_total, today),
        ]
        self.tree.delete(*self.tree.get_children())
        for label, a, b in rows:
            p = self.store.period(a, b, now, wbd)
            diff = p["diff"]
            if label.startswith("Gesamt"):
                diff += float(self.store.settings.get("initial_balance_minutes", 0))
            tag = "neg" if diff < -0.5 else ("pos" if diff > 0.5 else "")
            self.tree.insert("", "end", text=label, tags=(tag,),
                             values=(fmt_hm(p["worked"]) + " h", fmt_hm(p["target"]) + " h",
                                     fmt_hm(diff, True) + " h"))

        self.lbl_saldo.config(text=f"Überstunden-Saldo: {fmt_hm(st.balance_now, True)} h")
        vs = self.store.vacation_stats(today.year, today)
        self.lbl_vac.config(text=(
            f"Urlaub {today.year}: Anspruch {fmt_days(vs['entitlement'])} · genommen {fmt_days(vs['taken'])} · "
            f"geplant {fmt_days(vs['planned'])} · Rest {fmt_days(vs['rest'])} Tage    |    "
            f"Krankheitstage: {fmt_days(vs['sick'])}"))


# =========================================================================== Historie

class HistoryWindow(BaseWindow):
    key = "history"

    def __init__(self, app, **kw):
        super().__init__(app, "Historie")
        self.geometry(f"{px(980)}x{px(620)}")
        today = date.today()
        self.year, self.month = today.year, today.month

        top = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        top.pack(fill="x")
        ttk.Button(top, text="◀", width=3, command=lambda: self.shift(-1)).pack(side="left")
        self.lbl_month = ttk.Label(top, style="Header.TLabel", width=16, anchor="center")
        self.lbl_month.pack(side="left", padx=6)
        ttk.Button(top, text="▶", width=3, command=lambda: self.shift(1)).pack(side="left")
        ttk.Button(top, text="Heute", command=self.go_today).pack(side="left", padx=10)
        ttk.Button(top, text="CSV exportieren…", command=self.export_csv).pack(side="right")
        ttk.Button(top, text="Saldo-Verlauf", command=lambda: self.app.open_window("balance")).pack(side="right", padx=(0, 6))
        ttk.Button(top, text="Abwesenheit…", command=self.open_absence).pack(side="right", padx=6)
        ttk.Button(top, text="Tag bearbeiten…", command=self.edit_selected).pack(side="right")

        cols = ("tag", "beginn", "ende", "pause", "arbeit", "soll", "diff", "abw")
        heads = ("Tag", "Beginn", "Ende", "Pause", "Gearbeitet", "Soll", "Differenz", "Abwesenheit / Hinweis")
        widths = (40, 80, 95, 70, 90, 70, 85, 290)
        frame = ttk.Frame(self, padding=PAD)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=cols, selectmode="browse")
        self.tree.heading("#0", text="Datum")
        self.tree.column("#0", width=px(100))
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=px(w), anchor="w" if c == "abw" else "center", stretch=(c == "abw"))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("free", foreground="#9e9e9e")
        self.tree.tag_configure("today", background="#e3f2fd")
        self.tree.tag_configure("neg", foreground="#b71c1c")
        self.tree.tag_configure("pos", foreground="#1b5e20")
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())
        self.tree.bind("<Return>", lambda e: self.edit_selected())

        self.lbl_sum = ttk.Label(self, padding=(PAD, 0, PAD, PAD), style="Big.TLabel")
        self.lbl_sum.pack(fill="x")
        self.refresh()

    def shift(self, delta: int):
        m = self.month - 1 + delta
        self.year += m // 12
        self.month = m % 12 + 1
        self.refresh()

    def go_today(self):
        t = date.today()
        self.year, self.month = t.year, t.month
        self.refresh()

    def month_days(self):
        first = date(self.year, self.month, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return first, last

    def refresh(self):
        self.lbl_month.config(text=f"{MONTHS[self.month - 1]} {self.year}")
        first, last = self.month_days()
        now = datetime.now().replace(microsecond=0)
        today = now.date()
        wbd = self.store.worked_by_day(now)
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for d in daterange(first, last):
            worked = wbd.get(d, 0.0)
            target = self.store.target_minutes(d)
            begin, end, is_open = self.store.first_last(d, now)
            pause = self.store.pause_on(d, now, wbd) if begin else 0.0
            a = self.store.absence_for(d)
            tags = []
            if d == today:
                tags.append("today")
            diff = worked - target if d <= today else 0.0
            if not self.store.is_workday(d) and worked == 0:
                tags.append("free")
            elif d <= today and target > 0:
                tags.append("neg" if diff < -0.5 else "pos")
            hint = absence_text(a)
            if d < self.store.tracking_start:
                hint = (hint + "  " if hint else "") + "(vor Erfassungsbeginn)"
            self.tree.insert("", "end", iid=d.isoformat(), text=fmt_date(d), tags=tags, values=(
                WEEKDAYS_SHORT[d.weekday()],
                fmt_time(begin),
                "läuft" if is_open else fmt_time_on(end, d),
                fmt_hm(pause) if begin else "",
                fmt_hm(worked) if worked else "",
                fmt_hm(target) if target else "",
                fmt_hm(diff, True) if (d <= today and (target or worked)) else "",
                hint))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
            self.tree.see(sel[0])
        elif self.tree.exists(today.isoformat()):
            self.tree.see(today.isoformat())
        p = self.store.period(first, last, now, wbd)
        self.lbl_sum.config(text=f"Summe {MONTHS[self.month - 1]}: gearbeitet {fmt_hm(p['worked'])} h · "
                                 f"Soll bis heute {fmt_hm(p['target'])} h · Differenz {fmt_hm(p['diff'], True)} h · "
                                 f"Soll ganzer Monat {fmt_hm(p['target_total'])} h")

    def selected_date(self) -> date | None:
        sel = self.tree.selection()
        return parse_date(sel[0]) if sel else None

    def edit_selected(self):
        d = self.selected_date() or date.today()
        DayDialog(self, d)

    def open_absence(self):
        d = self.selected_date() or date.today()
        self.app.open_window("absences", select=d)

    def export_csv(self):
        first, last = self.month_days()
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            initialfile=f"Arbeitszeit-{self.year}-{self.month:02d}.csv",
                                            filetypes=[("CSV-Datei", "*.csv")])
        if not path:
            return
        now = datetime.now()
        wbd = self.store.worked_by_day(now)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Datum", "Wochentag", "Beginn", "Ende", "Pause (h)", "Gearbeitet (h)", "Soll (h)",
                        "Differenz (h)", "Abwesenheit", "Zeiträume"])
            for d in daterange(first, last):
                begin, end, _ = self.store.first_last(d, now)
                worked, target = wbd.get(d, 0.0), self.store.target_minutes(d)
                spans = ", ".join(f"{parse_dt(s['start']):%H:%M}-{parse_dt(s['end']):%H:%M}" if s["end"]
                                  else f"{parse_dt(s['start']):%H:%M}-…" for s in self.store.day_sessions(d))
                w.writerow([fmt_date(d), WEEKDAYS[d.weekday()], fmt_time(begin), fmt_time_on(end, d),
                            fmt_hm(self.store.pause_on(d, now, wbd)) if begin else "",
                            fmt_hm(worked), fmt_hm(target), fmt_hm(worked - target, True),
                            absence_text(self.store.absence_for(d)), spans])
        messagebox.showinfo("Export", f"Gespeichert:\n{path}", parent=self)


class DayDialog(tk.Toplevel):
    """Arbeitszeiträume eines Tages bearbeiten."""

    def __init__(self, parent: BaseWindow, d: date):
        super().__init__(parent)
        self.parent_win = parent
        self.app = parent.app
        self.store = parent.store
        self.d = d
        self.title(f"Arbeitszeiten – {WEEKDAYS[d.weekday()]}, {fmt_date(d)}")
        self.transient(parent)
        self.resizable(False, False)
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=f"{WEEKDAYS[d.weekday()]}, {fmt_date(d)}", style="Header.TLabel").pack(anchor="w")
        ttk.Label(outer, style="Muted.TLabel", justify="left", text=(
            "Uhrzeiten als HH:MM eingeben. Lücken zwischen den Zeiträumen gelten als Pause.\n"
            "Ende leer lassen = Zeitraum läuft noch. Ende vor Beginn = endet am Folgetag.\n"
            "Arbeit nach Mitternacht mit „+1“ eingeben, z. B. 00:30+1 – sie zählt zu diesem Arbeitstag.")
        ).pack(anchor="w", pady=(2, 8))
        self.rows_frame = ttk.Frame(outer)
        self.rows_frame.pack(fill="x")
        ttk.Label(self.rows_frame, text="Beginn").grid(row=0, column=0)
        ttk.Label(self.rows_frame, text="Ende").grid(row=0, column=2)
        self.rows: list = []
        for s in self.store.day_sessions(d):
            st = parse_dt(s["start"])
            en = parse_dt(s["end"]) if s["end"] else None
            self.add_row(s["id"], fmt_time_on(st, d), fmt_time_on(en, d) if en else "")
        if not self.rows:
            self.add_row(None, "", "")
        ttk.Button(outer, text="+ Zeitraum hinzufügen", command=lambda: self.add_row(None, "", "")).pack(anchor="w", pady=8)
        a = self.store.absence_for(d)
        info = f"Soll: {fmt_hm(self.store.target_minutes(d))} h"
        if a:
            info += f"   ·   {absence_text(a)}"
        ttk.Label(outer, text=info, style="Muted.TLabel").pack(anchor="w")
        bf = ttk.Frame(outer)
        bf.pack(fill="x", pady=(12, 0))
        ttk.Button(bf, text="Speichern", command=self.save).pack(side="right")
        ttk.Button(bf, text="Abbrechen", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def add_row(self, sid, start_txt, end_txt):
        r = len(self.rows) + 1
        e1 = ttk.Entry(self.rows_frame, width=11, justify="center")
        e1.insert(0, start_txt)
        dash = ttk.Label(self.rows_frame, text="–")
        e2 = ttk.Entry(self.rows_frame, width=11, justify="center")
        e2.insert(0, end_txt)
        row = {"id": sid, "widgets": (e1, dash, e2), "start": e1, "end": e2}
        btn = ttk.Button(self.rows_frame, text="✕", width=3, command=lambda: self.remove_row(row))
        row["widgets"] += (btn,)
        for c, w in enumerate(row["widgets"]):
            w.grid(row=r, column=c, padx=3, pady=2)
        self.rows.append(row)
        e1.focus_set()

    def remove_row(self, row):
        for w in row["widgets"]:
            w.destroy()
        self.rows.remove(row)

    def save(self):
        parsed = []
        try:
            for row in self.rows:
                s_txt, e_txt = row["start"].get().strip(), row["end"].get().strip()
                if not s_txt and not e_txt:
                    continue
                if not s_txt:
                    raise ValueError("Jeder Zeitraum braucht einen Beginn.")
                st = parse_clock(s_txt, self.d)
                en = None
                if e_txt:
                    en = parse_clock(e_txt, self.d)
                    if en <= st:
                        en += timedelta(days=1)
                parsed.append({"id": row["id"], "start": st, "end": en})
            parsed.sort(key=lambda r: r["start"])
            open_rows = [r for r in parsed if r["end"] is None]
            if open_rows:
                if self.d != self.store.current_workday():
                    raise ValueError("Ein offener Zeitraum (ohne Ende) ist nur am aktuellen Arbeitstag möglich.")
                if len(open_rows) > 1 or open_rows[0] is not parsed[-1]:
                    raise ValueError("Nur der letzte Zeitraum darf ohne Ende sein.")
                if open_rows[0]["start"] > datetime.now():
                    raise ValueError("Der laufende Zeitraum kann nicht in der Zukunft beginnen.")
                other = self.store.open_session()
                if other and self.store.session_workday(other) != self.d:
                    raise ValueError("Es läuft bereits eine Erfassung an einem anderen Tag.")
            for a, b in zip(parsed, parsed[1:]):
                if a["end"] is None or a["end"] > b["start"]:
                    raise ValueError(f"Die Zeiträume überschneiden sich ({fmt_time(a['start'])} / {fmt_time(b['start'])}).")
            for r in parsed:
                if r["end"] and r["end"] - r["start"] > timedelta(hours=24):
                    raise ValueError("Ein Zeitraum darf nicht länger als 24 Stunden sein.")
        except ValueError as e:
            messagebox.showerror("Ungültige Eingabe", str(e), parent=self)
            return
        self.store.replace_day_sessions(self.d, parsed)
        self.destroy()
        self.app.refresh_all()


# =========================================================================== Abwesenheiten

class AbsenceWindow(BaseWindow):
    key = "absences"

    def __init__(self, app, select: date | None = None, **kw):
        super().__init__(app, "Urlaub & Krankheit")
        self.geometry(f"{px(960)}x{px(780)}")
        self._pick_second = False
        today = date.today()

        main = ttk.Frame(self, padding=PAD)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # --- Kalender
        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="nsew")
        self.cal = Calendar(left, selectmode="day", locale="de_DE", firstweekday="monday",
                            showweeknumbers=True, date_pattern="dd.mm.yyyy",
                            font=("Segoe UI", 11), weekendforeground="#555555",
                            othermonthforeground="#aaaaaa", othermonthweforeground="#aaaaaa")
        self.cal.pack(fill="both", expand=True)
        self.cal.bind("<<CalendarSelected>>", self.on_pick)
        self.cal.bind("<<CalendarMonthChanged>>", lambda e: self.paint_calendar())
        for k, v in ABSENCE_KINDS.items():
            self.cal.tag_config(k, background=v["color"], foreground="white")
            self.cal.tag_config(k + "_half", background=v["half_color"], foreground="black")
        self.cal.tag_config("auswahl", background="#ff9800", foreground="black")

        legend = ttk.Frame(left)
        legend.pack(fill="x", pady=(6, 0))
        for k, v in ABSENCE_KINDS.items():
            tk.Label(legend, text=f" {v['label']} ", bg=v["color"], fg="white",
                     font=("Segoe UI", 8)).pack(side="left", padx=2)
        tk.Label(legend, text=" halbtags = heller ", bg="#e0e0e0", font=("Segoe UI", 8)).pack(side="left", padx=2)
        tk.Label(legend, text=" Auswahl ", bg="#ff9800", font=("Segoe UI", 8)).pack(side="left", padx=2)

        # --- Formular
        form = ttk.LabelFrame(main, text="Zeitraum eintragen", padding=10)
        form.grid(row=0, column=1, sticky="nsew", padx=(PAD, 0))
        ttk.Label(form, style="Muted.TLabel", justify="left",
                  text="Im Kalender: 1. Klick = Startdatum,\n2. Klick = Enddatum.").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(form, text="Von:").grid(row=1, column=0, sticky="w")
        self.e_from = date_entry(form, today)
        self.e_from.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(form, text="Bis:").grid(row=2, column=0, sticky="w")
        self.e_to = date_entry(form, today)
        self.e_to.grid(row=2, column=1, sticky="w", pady=2)
        self.e_from.bind("<<DateEntrySelected>>", lambda e: self.paint_calendar())
        self.e_to.bind("<<DateEntrySelected>>", lambda e: self.paint_calendar())
        ttk.Label(form, text="Art:").grid(row=3, column=0, sticky="w")
        self.kind_var = tk.StringVar(value=KIND_LABELS[0])
        cb = ttk.Combobox(form, textvariable=self.kind_var, values=KIND_LABELS, state="readonly", width=22)
        cb.grid(row=3, column=1, sticky="w", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_half_state())
        hf = ttk.Frame(form)
        hf.grid(row=4, column=1, sticky="w", pady=2)
        self.first_half_var = tk.BooleanVar(value=False)
        self.last_half_var = tk.BooleanVar(value=False)
        self.chk_first = ttk.Checkbutton(hf, text="Erster Tag halbtags", variable=self.first_half_var,
                                         command=self.update_half_state)
        self.part_var = tk.StringVar(value="vormittags frei")
        self.part_cb = ttk.Combobox(hf, textvariable=self.part_var, state="readonly", width=16,
                                    values=["vormittags frei", "nachmittags frei"])
        self.chk_last = ttk.Checkbutton(hf, text="Letzter Tag halbtags", variable=self.last_half_var)
        self.chk_first.grid(row=0, column=0, sticky="w")
        self.part_cb.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.chk_last.grid(row=1, column=0, columnspan=2, sticky="w")
        self.workdays_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Nur Arbeitstage eintragen", variable=self.workdays_var).grid(row=5, column=1, sticky="w")
        ttk.Label(form, text="Notiz:").grid(row=6, column=0, sticky="w")
        self.e_note = ttk.Entry(form, width=25)
        self.e_note.grid(row=6, column=1, sticky="w", pady=2)
        bf = ttk.Frame(form)
        bf.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(bf, text="Eintragen", command=self.add).pack(side="left")
        ttk.Button(bf, text="Zeitraum löschen", command=self.remove).pack(side="left", padx=6)
        self.lbl_stats = ttk.Label(form, justify="left")
        self.lbl_stats.grid(row=8, column=0, columnspan=2, sticky="w", pady=(16, 0))
        if app.personio_sources():
            ttk.Button(form, text="Aus Personio laden", command=lambda: app.sync_personio(manual=True)).grid(
                row=9, column=0, columnspan=2, sticky="w", pady=(10, 0))

        # --- Liste
        lf = ttk.LabelFrame(main, text="Eingetragene Abwesenheiten", padding=6)
        lf.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(PAD, 0))
        hdr = ttk.Frame(lf)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="Jahr:").pack(side="left")
        self.year_var = tk.IntVar(value=today.year)
        sp = ttk.Spinbox(hdr, from_=2000, to=2100, width=6, textvariable=self.year_var, command=self.refresh_list)
        sp.pack(side="left", padx=4)
        sp.bind("<Return>", lambda e: self.refresh_list())
        ttk.Button(hdr, text="Ausgewählte löschen", command=self.delete_selected).pack(side="right")
        cols = ("bis", "art", "tage", "quelle", "notiz")
        self.tree = ttk.Treeview(lf, columns=cols, height=8)
        self.tree.heading("#0", text="Von")
        self.tree.column("#0", width=px(100))
        for c, h, w in zip(cols, ("Bis", "Art", "Tage", "Quelle", "Notiz"), (100, 260, 60, 90, 220)):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=px(w), anchor="center" if c in ("tage",) else "w")
        self.tree.pack(fill="both", expand=True, pady=(4, 0))
        self.tree.bind("<<TreeviewSelect>>", self.on_list_select)
        self.groups: dict = {}

        self.update_half_state()
        if select:
            self.bring_to_front(select=select)
        self.refresh()

    # ---------------------------------------------------------------- Interaktion
    def bring_to_front(self, select: date | None = None, **kw):
        super().bring_to_front()
        if select:
            self.e_from.set_date(select)
            self.e_to.set_date(select)
            self.cal.selection_set(select)
            self.cal.see(select)
            self.paint_calendar()

    def update_half_state(self):
        """Halbe Tage nur am ersten/letzten Tag – bei einem einzelnen Tag nur ein Haken „Halbtags“."""
        kind = KIND_KEYS[KIND_LABELS.index(self.kind_var.get())]
        a, b = self.get_range()
        single = a == b
        self.chk_first.config(text="Halbtags" if single else "Erster Tag halbtags (ab mittags frei)")
        self.chk_last.config(text="Letzter Tag halbtags (bis mittags frei)")
        if single:
            self.last_half_var.set(False)
            self.chk_last.grid_remove()
            if self.first_half_var.get():
                self.part_cb.grid()
            else:
                self.part_cb.grid_remove()
        else:
            self.chk_last.grid()
            self.part_cb.grid_remove()
        for chk, var in ((self.chk_first, self.first_half_var), (self.chk_last, self.last_half_var)):
            if kind == "fza":
                var.set(False)
                chk.state(["disabled"])
            else:
                chk.state(["!disabled"])

    def on_pick(self, _e=None):
        d = self.cal.selection_get()
        if not self._pick_second or d < self.e_from.get_date():
            self.e_from.set_date(d)
            self.e_to.set_date(d)
            self._pick_second = True
        else:
            self.e_to.set_date(d)
            self._pick_second = False
        self.paint_calendar()

    def get_range(self):
        a, b = self.e_from.get_date(), self.e_to.get_date()
        if b < a:
            a, b = b, a
        return a, b

    def add(self):
        a, b = self.get_range()
        kind = KIND_KEYS[KIND_LABELS.index(self.kind_var.get())]
        existing = [d for d in daterange(a, b) if d.isoformat() in self.store.data["absences"]]
        if existing and not messagebox.askyesno(
                "Überschreiben?", f"Im Zeitraum sind bereits {len(existing)} Tage eingetragen. Überschreiben?", parent=self):
            return
        n = self.store.set_absences(a, b, kind, 1.0, self.workdays_var.get(), self.e_note.get().strip(),
                                    first_half=self.first_half_var.get(),
                                    last_half=self.last_half_var.get() and a != b,
                                    single_half=(("am" if self.part_var.get().startswith("vor") else "pm")
                                                 if a == b and self.first_half_var.get() else None))
        if n == 0:
            messagebox.showinfo("Hinweis", "Im gewählten Zeitraum liegen keine Arbeitstage.\n"
                                "(Option „Nur Arbeitstage eintragen“ abwählen, um trotzdem einzutragen.)", parent=self)
        self._pick_second = False
        self.app.refresh_all()

    def remove(self):
        a, b = self.get_range()
        n = sum(1 for d in daterange(a, b) if d.isoformat() in self.store.data["absences"])
        if not n:
            messagebox.showinfo("Hinweis", "Im Zeitraum ist nichts eingetragen.", parent=self)
            return
        if messagebox.askyesno("Löschen", f"{n} eingetragene Tage zwischen {fmt_date(a)} und {fmt_date(b)} löschen?", parent=self):
            self.store.remove_absences(a, b)
            self.app.refresh_all()

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        if not messagebox.askyesno("Löschen", f"{len(sel)} Zeitraum/Zeiträume löschen?", parent=self):
            return
        for iid in sel:
            g = self.groups.get(iid)
            if g:
                for d in daterange(g["start"], g["end"]):
                    a = self.store.data["absences"].get(d.isoformat())
                    if a and (a["kind"], a.get("source"), a.get("note", "")) == g["sig"]:
                        del self.store.data["absences"][d.isoformat()]
        self.store.touch()
        self.app.refresh_all()

    def on_list_select(self, _e=None):
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        g = self.groups.get(sel[0])
        if not g:
            return
        self.e_from.set_date(g["start"])
        self.e_to.set_date(g["end"])
        self.kind_var.set(kind_label(g["kind"]))
        self.first_half_var.set(g["start_portion"] < 1)
        self.last_half_var.set(g["end"] != g["start"] and g["end_portion"] < 1)
        if g["start"] == g["end"] and g.get("start_half"):
            self.part_var.set(HALF_LABELS[g["start_half"]] + " frei")
        self.e_note.delete(0, "end")
        self.e_note.insert(0, g["note"])
        self.cal.see(g["start"])
        self._pick_second = False
        self.paint_calendar()

    # ---------------------------------------------------------------- Darstellung
    def refresh(self):
        self.paint_calendar()
        self.refresh_list()

    def paint_calendar(self):
        self.cal.calevent_remove("all")
        month, year = self.cal.get_displayed_month()
        lo, hi = date(year - 1, 12, 1), date(year + 1, 1, 31)
        for d in daterange(lo, hi):
            a = self.store.absence_for(d)
            if not a:
                continue
            tag = a["kind"] + ("_half" if float(a.get("portion", 1)) < 1 else "")
            self.cal.calevent_create(d, absence_text(a), tags=[tag])
        a, b = self.get_range()
        for d in daterange(a, b):
            self.cal.calevent_create(d, "Auswahl", tags=["auswahl"])
        self.update_half_state()
        self.update_stats()

    def refresh_list(self):
        try:
            year = int(self.year_var.get())
        except (ValueError, tk.TclError):
            return
        self.tree.delete(*self.tree.get_children())
        self.groups = {}
        for i, g in enumerate(self.store.absence_groups(year)):
            iid = f"g{i}"
            self.groups[iid] = g
            art = kind_label(g["kind"]) + half_suffix(g)
            self.tree.insert("", "end", iid=iid, text=fmt_date(g["start"]), values=(
                fmt_date(g["end"]), art, fmt_days(g["days"]), SOURCE_LABELS.get(g["source"], g["source"]), g["note"]))
        self.update_stats()

    def update_stats(self):
        try:
            year = int(self.year_var.get())
        except (ValueError, tk.TclError):
            year = date.today().year
        vs = self.store.vacation_stats(year)
        a, b = self.get_range()
        wd = sum(1 for d in daterange(a, b) if self.store.is_workday(d) and not self.store.holiday_name(d))
        self.lbl_stats.config(text=(
            f"Auswahl: {fmt_date(a)} – {fmt_date(b)}\n({wd} Arbeitstage)\n\n"
            f"Urlaub {year}\n"
            f"  Anspruch:  {fmt_days(vs['entitlement'])}\n"
            f"  genommen:  {fmt_days(vs['taken'])}\n"
            f"  geplant:   {fmt_days(vs['planned'])}\n"
            f"  Rest:      {fmt_days(vs['rest'])}\n"
            f"Krankheitstage: {fmt_days(vs['sick'])}"))


# =========================================================================== Einstellungen

class SettingsWindow(BaseWindow):
    key = "settings"

    def __init__(self, app, **kw):
        super().__init__(app, "Einstellungen")
        self.resizable(False, False)
        s = self.store.settings
        p = s["personio"]
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=PAD, pady=PAD)

        # ---------------- Arbeitszeit
        t1 = ttk.Frame(nb, padding=12)
        nb.add(t1, text="Arbeitszeit")
        ttk.Label(t1, text="Arbeitstage und Soll-Stunden", style="Header.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        days = self.store.schedule_for(date.today())
        self.day_vars = []
        for i in range(7):
            dd = days.get(str(i), {"active": False, "hours": 0})
            av = tk.BooleanVar(value=bool(dd.get("active")))
            hv = tk.StringVar(value=fmt_hm(float(dd.get("hours", 0)) * 60))
            ttk.Checkbutton(t1, text=WEEKDAYS[i], variable=av, command=self.update_week_sum).grid(row=1 + i, column=0, sticky="w")
            e = ttk.Entry(t1, textvariable=hv, width=7, justify="center")
            e.grid(row=1 + i, column=1, sticky="w", padx=6, pady=1)
            hv.trace_add("write", lambda *a: self.update_week_sum())
            ttk.Label(t1, text="Stunden").grid(row=1 + i, column=2, sticky="w")
            self.day_vars.append((av, hv))
        self.lbl_week = ttk.Label(t1, style="Muted.TLabel")
        self.lbl_week.grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 8))
        self.valid_from_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(t1, text="Änderung erst gültig ab:", variable=self.valid_from_var).grid(row=9, column=0, columnspan=2, sticky="w")
        self.e_valid_from = date_entry(t1, date.today())
        self.e_valid_from.grid(row=9, column=2, sticky="w")
        ttk.Label(t1, style="Muted.TLabel", text="(sonst gilt das Modell rückwirkend für alle Tage)").grid(row=10, column=0, columnspan=4, sticky="w")

        ttk.Separator(t1).grid(row=11, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(t1, text="Zeiterfassung seit:").grid(row=12, column=0, sticky="w")
        self.e_start = date_entry(t1, self.store.tracking_start)
        self.e_start.grid(row=12, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Label(t1, text="Überstunden-Startsaldo:").grid(row=13, column=0, sticky="w")
        self.init_bal_var = tk.StringVar(value=fmt_hm(float(s.get("initial_balance_minutes", 0)), True) or "0:00")
        ttk.Entry(t1, textvariable=self.init_bal_var, width=9, justify="center").grid(row=13, column=1, sticky="w", pady=2)
        ttk.Label(t1, text="h (z. B. +12:30 oder -3:00)", style="Muted.TLabel").grid(row=13, column=2, columnspan=2, sticky="w")
        ttk.Label(t1, text="Urlaubstage pro Jahr:").grid(row=14, column=0, sticky="w")
        self.vac_var = tk.StringVar(value=fmt_days(float(s.get("vacation_days_per_year", 30))))
        ttk.Entry(t1, textvariable=self.vac_var, width=9, justify="center").grid(row=14, column=1, sticky="w", pady=2)
        ttk.Label(t1, text="Resturlaub Vorjahr:").grid(row=15, column=0, sticky="w")
        self.carry_var = tk.StringVar(value=fmt_days(float(s.get("vacation_carryover", 0))))
        ttk.Entry(t1, textvariable=self.carry_var, width=9, justify="center").grid(row=15, column=1, sticky="w", pady=2)
        ttk.Label(t1, text="Bundesland (Feiertage):").grid(row=16, column=0, sticky="w")
        self.region_var = tk.StringVar(value=BUNDESLAENDER.get(s.get("holiday_region", ""), BUNDESLAENDER[""]))
        cb = ttk.Combobox(t1, textvariable=self.region_var, values=list(BUNDESLAENDER.values()), state="readonly", width=30)
        cb.grid(row=16, column=1, columnspan=3, sticky="w", pady=2)
        ttk.Label(t1, text="Tageswechsel um:").grid(row=18, column=0, sticky="w", pady=(8, 2))
        self.boundary_var = tk.StringVar(value=str(s.get("day_boundary_hour", 5)))
        ttk.Spinbox(t1, from_=0, to=12, width=5, textvariable=self.boundary_var).grid(row=18, column=1, sticky="w", pady=(8, 2))
        ttk.Label(t1, text="Uhr – Arbeit bis dahin zählt zum Vortag; eine Pause über\n"
                           "diese Uhrzeit hinaus beendet den Arbeitstag", style="Muted.TLabel",
                  justify="left").grid(row=18, column=2, columnspan=2, sticky="w", pady=(8, 2))
        if _holidays is None:
            cb.state(["disabled"])
            ttk.Label(t1, text="(Paket „holidays“ nicht installiert)", style="Muted.TLabel").grid(row=17, column=1, columnspan=3, sticky="w")
        self.update_week_sum()

        # ---------------- Allgemein
        t2 = ttk.Frame(nb, padding=12)
        nb.add(t2, text="Allgemein")
        self.autostart_var = tk.BooleanVar(value=bool(s.get("launch_with_windows")))
        self.autowork_var = tk.BooleanVar(value=bool(s.get("auto_start_work")))
        self.autowork_wd_var = tk.BooleanVar(value=bool(s.get("auto_start_only_workdays", True)))
        self.shutdown_var = tk.StringVar(value=SHUTDOWN_MODES.get(s.get("shutdown_mode", "pause"), SHUTDOWN_MODES["pause"]))
        self.lock_var = tk.BooleanVar(value=bool(s.get("pause_on_lock")))
        self.notify_var = tk.BooleanVar(value=bool(s.get("notify_target_reached", True)))
        ttk.Label(t2, text="Start & Ende", style="Header.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(t2, text="ZeitTracker nach der Windows-Anmeldung automatisch starten",
                        variable=self.autostart_var).grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(t2, text="Zeiterfassung beim Start sofort beginnen (z. B. direkt nach der Anmeldung)",
                        variable=self.autowork_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(t2, text="nur an Arbeitstagen (nicht an Wochenenden, Feiertagen, Urlaub …)",
                        variable=self.autowork_wd_var).grid(row=3, column=0, columnspan=3, sticky="w", padx=(22, 0))
        sc = ttk.Frame(t2)
        sc.grid(row=4, column=0, columnspan=3, sticky="w", pady=2)
        sf = ttk.Frame(sc)
        sf.pack(anchor="w")
        ttk.Label(sf, text="Beim Herunterfahren / Abmelden:").pack(side="left")
        ttk.Combobox(sf, textvariable=self.shutdown_var, values=list(SHUTDOWN_MODES.values()),
                     state="readonly", width=52).pack(side="left", padx=6)
        gf = ttk.Frame(sc)
        gf.pack(anchor="w", pady=(4, 0), padx=(22, 0))
        ttk.Label(gf, text="Neustart innerhalb von").pack(side="left")
        self.grace_var = tk.StringVar(value=str(s.get("restart_grace_minutes", 10)))
        ttk.Spinbox(gf, from_=0, to=120, width=5, textvariable=self.grace_var).pack(side="left", padx=4)
        ttk.Label(gf, text="Minuten → Arbeitszeit läuft einfach weiter (keine Pause, kein Feierabend)").pack(side="left")
        ttk.Checkbutton(t2, text="Beim Sperren des Bildschirms (Win+L) pausieren, beim Entsperren fortsetzen",
                        variable=self.lock_var).grid(row=5, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(t2, text="Hinweise", style="Header.TLabel").grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.notify_personio_var = tk.BooleanVar(value=bool(s.get("notify_personio_changes", True)))
        self.notify_od_var = tk.BooleanVar(value=bool(s.get("notify_onedrive_download", True)))
        nf = ttk.Frame(t2)
        nf.grid(row=7, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(nf, text="Benachrichtigen, wenn das Tagessoll erreicht ist",
                        variable=self.notify_var).pack(anchor="w")
        ttk.Checkbutton(nf, text="Benachrichtigen, wenn Personio neue oder geänderte Abwesenheiten liefert",
                        variable=self.notify_personio_var).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(nf, text="Benachrichtigen, wenn ein neuerer Stand von OneDrive geladen wurde",
                        variable=self.notify_od_var).pack(anchor="w", pady=(2, 0))
        ttk.Label(t2, text="PC aus/Standby länger als").grid(row=8, column=0, sticky="w", pady=(10, 2))
        self.gap_var = tk.StringVar(value=str(s.get("sleep_gap_minutes", 15)))
        ttk.Spinbox(t2, from_=2, to=240, width=5, textvariable=self.gap_var).grid(row=8, column=1, sticky="w", padx=4, pady=(10, 2))
        ttk.Label(t2, text="Minuten → als Pause bzw. Feierabend werten").grid(row=8, column=2, sticky="w", pady=(10, 2))
        ttk.Button(t2, text="Datenordner öffnen", command=lambda: winutil.open_folder(self.store.path.parent)).grid(row=9, column=0, sticky="w", pady=(16, 0))
        ttk.Label(t2, text=str(self.store.path), style="Muted.TLabel").grid(row=10, column=0, columnspan=3, sticky="w")

        # ---------------- Personio
        t3 = ttk.Frame(nb, padding=12)
        nb.add(t3, text="Personio (API)")
        self.p_enabled = tk.BooleanVar(value=bool(p.get("enabled")))
        ttk.Checkbutton(t3, text="Personio-Anbindung aktivieren", variable=self.p_enabled,
                        command=self.update_personio_state).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(t3, style="Muted.TLabel", justify="left", text=(
            "API-Zugangsdaten erstellst du (bzw. dein HR-Admin) in Personio unter\n"
            "Einstellungen › Integrationen › API-Zugangsdaten. Benötigte Rechte: Personen lesen,\n"
            "Abwesenheiten lesen, Anwesenheiten lesen/schreiben (nur für den Export).")).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))
        self.p_widgets = []

        def row(r, label, widget):
            ttk.Label(t3, text=label).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)
            self.p_widgets.append(widget)

        self.p_client = tk.StringVar(value=p.get("client_id", ""))
        self.p_secret = tk.StringVar(value=personio.get_secret(self.store))
        self.p_email = tk.StringVar(value=p.get("email", ""))
        row(2, "Client-ID:", ttk.Entry(t3, textvariable=self.p_client, width=46))
        row(3, "Client-Secret:", ttk.Entry(t3, textvariable=self.p_secret, width=46, show="•"))
        row(4, "Deine E-Mail in Personio:", ttk.Entry(t3, textvariable=self.p_email, width=46))
        self.lbl_pid = ttk.Label(t3, text=f"Personio-ID: {p.get('person_id') or '–'}", style="Muted.TLabel")
        self.lbl_pid.grid(row=5, column=1, sticky="w")

        self.p_import = tk.BooleanVar(value=bool(p.get("import_absences", True)))
        self.p_pending = tk.BooleanVar(value=bool(p.get("include_pending")))
        self.p_export = tk.BooleanVar(value=bool(p.get("export_attendance")))
        c1 = ttk.Checkbutton(t3, text="Urlaubs- und Krankheitstage aus Personio übernehmen", variable=self.p_import)
        c2 = ttk.Checkbutton(t3, text="auch noch nicht genehmigte Abwesenheiten übernehmen", variable=self.p_pending)
        c3 = ttk.Checkbutton(t3, text="Arbeitszeiten an Personio übertragen (Anwesenheiten)", variable=self.p_export)
        c1.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        c2.grid(row=7, column=0, columnspan=3, sticky="w", padx=(22, 0))
        c3.grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.p_widgets += [c1, c2, c3]
        ttk.Label(t3, text="Übertragen ab Datum:").grid(row=9, column=0, sticky="w", padx=(22, 0))
        try:
            ef = parse_date(p.get("export_from") or date.today().isoformat())
        except ValueError:
            ef = date.today()
        self.e_export_from = date_entry(t3, ef)
        self.e_export_from.grid(row=9, column=1, sticky="w", pady=2)
        self.p_widgets.append(self.e_export_from)
        ttk.Label(t3, text="Automatisch synchronisieren alle").grid(row=10, column=0, sticky="w", pady=(8, 0))
        self.p_interval = tk.StringVar(value=str(p.get("auto_sync_minutes", 60)))
        sp = ttk.Spinbox(t3, from_=0, to=1440, increment=15, width=6, textvariable=self.p_interval)
        sp.grid(row=10, column=1, sticky="w", pady=(8, 0))
        ttk.Label(t3, text="Minuten (0 = nur manuell)", style="Muted.TLabel").grid(row=10, column=2, sticky="w", pady=(8, 0))
        self.p_widgets.append(sp)
        bf = ttk.Frame(t3)
        bf.grid(row=11, column=0, columnspan=3, sticky="w", pady=(12, 0))
        b1 = ttk.Button(bf, text="Verbindung testen", command=self.test_personio)
        b2 = ttk.Button(bf, text="Speichern & jetzt synchronisieren", command=self.save_and_sync)
        b1.pack(side="left")
        b2.pack(side="left", padx=6)
        self.p_widgets += [b1, b2]
        last = p.get("last_sync")
        self.lbl_p_status = ttk.Label(t3, style="Muted.TLabel", wraplength=px(480), justify="left", text=(
            f"Letzte Synchronisation: {parse_dt(last):%d.%m.%Y %H:%M}\n{p.get('last_result', '')}" if last else "Noch nicht synchronisiert."))
        self.lbl_p_status.grid(row=12, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.update_personio_state()

        # ---------------- Personio-Kalender-Abo (ohne API)
        ic = s.get("ical", {})
        t4 = ttk.Frame(nb, padding=12)
        nb.add(t4, text="Personio (Kalender-Abo)")
        self.i_enabled = tk.BooleanVar(value=bool(ic.get("enabled")))
        ttk.Checkbutton(t4, text="Abwesenheiten über das Personio-Kalender-Abo übernehmen",
                        variable=self.i_enabled, command=self.update_ical_state).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(t4, style="Muted.TLabel", justify="left", text=(
            "Funktioniert auch ohne API-Zugang. Den Link erzeugst du selbst in Personio:\n"
            "Abwesenheit & Anwesenheit › Team-Kalender › ⋮ › Abonnieren › „Abwesenheit“ sowie\n"
            "Urlaub, Bildungsurlaub usw. auswählen › Link erzeugen › kopieren.\n"
            "Wer den Link kennt, sieht die Einträge – er wird daher verschlüsselt in Windows gespeichert.")
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))
        self.i_widgets = []
        self.i_url = tk.StringVar(value=personio_ical.get_url(self.store))
        self.i_name = tk.StringVar(value=ic.get("name_filter", ""))
        self.i_interval = tk.StringVar(value=str(ic.get("auto_sync_minutes", 180)))
        ttk.Label(t4, text="Kalender-Link:").grid(row=2, column=0, sticky="w", pady=2)
        e_url = ttk.Entry(t4, textvariable=self.i_url, width=56, show="•")
        e_url.grid(row=2, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Label(t4, text="Nur Einträge mit Namen:").grid(row=3, column=0, sticky="w", pady=2)
        e_name = ttk.Entry(t4, textvariable=self.i_name, width=30)
        e_name.grid(row=3, column=1, sticky="w", pady=2)
        ttk.Label(t4, text="genau wie in Personio, z. B. Max Mustermann", style="Muted.TLabel").grid(row=3, column=2, sticky="w")
        ttk.Label(t4, text="„[Abwesenheit]“ ohne Art:").grid(row=4, column=0, sticky="w", pady=2)
        self.i_generic = tk.StringVar(value=GENERIC_KIND_LABELS.get(ic.get("generic_kind", "sonstiges"), GENERIC_KIND_LABELS["sonstiges"]))
        cb_g = ttk.Combobox(t4, textvariable=self.i_generic, values=list(GENERIC_KIND_LABELS.values()), state="readonly", width=28)
        cb_g.grid(row=4, column=1, sticky="w", pady=2)
        ttk.Label(t4, text="gilt nur, wenn kein [Urlaub] o. ä. für den Tag existiert", style="Muted.TLabel").grid(row=4, column=2, sticky="w")
        ttk.Label(t4, text="Automatisch abrufen alle").grid(row=5, column=0, sticky="w", pady=2)
        sp_i = ttk.Spinbox(t4, from_=0, to=1440, increment=30, width=6, textvariable=self.i_interval)
        sp_i.grid(row=5, column=1, sticky="w", pady=2)
        ttk.Label(t4, text="Minuten (0 = nur manuell)", style="Muted.TLabel").grid(row=5, column=2, sticky="w")
        ib = ttk.Frame(t4)
        ib.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        b_load = ttk.Button(ib, text="Speichern & jetzt laden", command=self.save_and_load_ical)
        b_load.pack(side="left")
        self.i_widgets += [e_url, e_name, cb_g, sp_i, b_load]
        last_i = ic.get("last_sync")
        self.lbl_i_status = ttk.Label(t4, style="Muted.TLabel", wraplength=px(520), justify="left", text=(
            (f"Zuletzt geladen: {parse_dt(last_i):%d.%m.%Y %H:%M}\n{ic.get('last_result', '')}" if last_i
             else "Noch nicht geladen.")))
        self.lbl_i_status.grid(row=7, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(t4, style="Muted.TLabel", justify="left", text=(
            "Übernommen werden Urlaub, Krankheit und sonstige Abwesenheiten an Arbeitstagen.\n"
            "Manuell eingetragene Tage haben Vorrang. Arbeitszeiten können über das Kalender-Abo\n"
            "nicht an Personio übertragen werden – dafür ist der API-Zugang nötig.")
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.update_ical_state()

        # ---------------- OneDrive
        od = s.get("onedrive", {})
        t5 = ttk.Frame(nb, padding=12)
        nb.add(t5, text="OneDrive")
        self.od_enabled = tk.BooleanVar(value=bool(od.get("enabled")))
        ttk.Checkbutton(t5, text="Daten automatisch auf OneDrive sichern und abgleichen",
                        variable=self.od_enabled).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(t5, style="Muted.TLabel", justify="left", text=(
            "Anmelden mit deinem privaten Microsoft-Konto – die Daten liegen in deinem OneDrive unter\n"
            "Apps › ZeitTracker. Der ZeitTracker sieht nur diesen App-Ordner, nicht deine anderen Dateien.")
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))
        ttk.Label(t5, text="Konto:").grid(row=3, column=0, sticky="w", pady=2)
        self.lbl_od_account = ttk.Label(t5, text="–")
        self.lbl_od_account.grid(row=3, column=1, sticky="w", pady=2)
        ob = ttk.Frame(t5)
        ob.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(ob, text="Bei Microsoft anmelden…", command=self.od_login).pack(side="left")
        ttk.Button(ob, text="Abmelden", command=self.od_logout).pack(side="left", padx=6)
        ttk.Button(ob, text="Jetzt abgleichen", command=self.od_sync_now).pack(side="left")
        ttk.Label(t5, text="Automatisch abgleichen alle").grid(row=5, column=0, sticky="w", pady=(10, 2))
        self.od_minutes = tk.StringVar(value=str(od.get("sync_minutes", 15)))
        ttk.Spinbox(t5, from_=0, to=240, increment=5, width=6, textvariable=self.od_minutes).grid(row=5, column=1, sticky="w", pady=(10, 2))
        ttk.Label(t5, text="Minuten (zusätzlich beim Start, bei Feierabend und beim Beenden)",
                  style="Muted.TLabel").grid(row=5, column=2, sticky="w", pady=(10, 2))
        self.lbl_od_status = ttk.Label(t5, style="Muted.TLabel", wraplength=px(560), justify="left")
        self.lbl_od_status.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.od_conflict_frame = ttk.Frame(t5)
        self.od_conflict_frame.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(self.od_conflict_frame, text="Stand von OneDrive übernehmen",
                   command=lambda: self.od_resolve("remote")).pack(side="left")
        ttk.Button(self.od_conflict_frame, text="Lokalen Stand hochladen",
                   command=lambda: self.od_resolve("local")).pack(side="left", padx=6)
        self.od_refresh_status()

        # ---------------- Buttons
        bb = ttk.Frame(self, padding=(PAD, 0, PAD, PAD))
        bb.pack(fill="x")
        ttk.Button(bb, text="Speichern", command=self.save).pack(side="right")
        ttk.Button(bb, text="Abbrechen", command=self.close).pack(side="right", padx=6)

    # ----------------------------------------------------------------
    def update_week_sum(self):
        total = 0
        for av, hv in self.day_vars:
            if av.get():
                try:
                    total += parse_hm(hv.get())
                except ValueError:
                    pass
        self.lbl_week.config(text=f"Wochenstunden: {fmt_hm(total)} h")

    def update_personio_state(self):
        state = ["!disabled"] if self.p_enabled.get() else ["disabled"]
        for w in self.p_widgets:
            try:
                w.state(state)
            except (AttributeError, tk.TclError):
                w.configure(state="normal" if self.p_enabled.get() else "disabled")

    def test_personio(self):
        cid, sec, mail = self.p_client.get().strip(), self.p_secret.get().strip(), self.p_email.get().strip()
        self.lbl_p_status.config(text="Teste Verbindung …")

        def done(result, error):
            if not self.winfo_exists():
                return
            if error:
                self.lbl_p_status.config(text=f"❌ {error}")
            else:
                self.lbl_pid.config(text=f"Personio-ID: {result}")
                self.lbl_p_status.config(text=f"✓ Verbindung erfolgreich. Person gefunden (ID {result}).")
                self._found_pid = result
        self.app.run_bg(lambda: personio.test_connection(cid, sec, mail), done)

    def collect(self) -> dict | None:
        try:
            days = {}
            for i, (av, hv) in enumerate(self.day_vars):
                mins = parse_hm(hv.get() or "0")
                if not 0 <= mins <= 24 * 60:
                    raise ValueError(f"Stunden für {WEEKDAYS[i]} ungültig.")
                days[str(i)] = {"active": bool(av.get()) and mins > 0, "hours": round(mins / 60, 4)}
            init_bal = parse_hm(self.init_bal_var.get() or "0")
            vac = float(self.vac_var.get().replace(",", ".") or 0)
            carry = float(self.carry_var.get().replace(",", ".") or 0)
            gap = int(self.gap_var.get())
            grace = int(self.grace_var.get() or 0)
            boundary = int(self.boundary_var.get() or 0)
            if not 0 <= boundary <= 12:
                raise ValueError("Tageswechsel muss zwischen 0 und 12 Uhr liegen.")
            interval = int(self.p_interval.get() or 0)
        except ValueError as e:
            messagebox.showerror("Ungültige Eingabe", f"Bitte Eingaben prüfen.\n{e}", parent=self)
            return None
        region = next((k for k, v in BUNDESLAENDER.items() if v == self.region_var.get()), "")
        return {"days": days, "init_bal": init_bal, "vac": vac, "carry": carry, "gap": max(2, gap),
                "interval": max(0, interval), "region": region, "grace": max(0, grace), "boundary": boundary}

    def save(self, close: bool = True) -> bool:
        v = self.collect()
        if v is None:
            return False
        st = self.store
        with st.lock:
            s = st.settings
            s["tracking_start"] = self.e_start.get_date().isoformat()
            s["initial_balance_minutes"] = v["init_bal"]
            s["vacation_days_per_year"] = v["vac"]
            s["vacation_carryover"] = v["carry"]
            s["holiday_region"] = v["region"]
            s["holiday_region_asked"] = True
            st._holiday_cache.clear()
            s["launch_with_windows"] = bool(self.autostart_var.get())
            s["auto_start_work"] = bool(self.autowork_var.get())
            s["auto_start_only_workdays"] = bool(self.autowork_wd_var.get())
            s["restart_grace_minutes"] = v["grace"]
            s["day_boundary_hour"] = v["boundary"]
            s["shutdown_mode"] = next((k for k, v in SHUTDOWN_MODES.items() if v == self.shutdown_var.get()), "pause")
            s["pause_on_lock"] = bool(self.lock_var.get())
            s["notify_target_reached"] = bool(self.notify_var.get())
            s["notify_personio_changes"] = bool(self.notify_personio_var.get())
            s["notify_onedrive_download"] = bool(self.notify_od_var.get())
            s["sleep_gap_minutes"] = v["gap"]
            p = s["personio"]
            new_cid = self.p_client.get().strip()
            new_mail = self.p_email.get().strip()
            if new_cid != p.get("client_id") or new_mail != p.get("email"):
                p["person_id"] = ""
            if getattr(self, "_found_pid", None):
                p["person_id"] = self._found_pid
            p["enabled"] = bool(self.p_enabled.get())
            p["client_id"] = new_cid
            p["email"] = new_mail
            p["import_absences"] = bool(self.p_import.get())
            p["include_pending"] = bool(self.p_pending.get())
            p["export_attendance"] = bool(self.p_export.get())
            p["export_from"] = self.e_export_from.get_date().isoformat()
            p["auto_sync_minutes"] = v["interval"]
            ic = s.setdefault("ical", {})
            ic["enabled"] = bool(self.i_enabled.get())
            ic["name_filter"] = self.i_name.get().strip()
            ic["generic_kind"] = next((k for k, v in GENERIC_KIND_LABELS.items() if v == self.i_generic.get()), "sonstiges")
            try:
                ic["auto_sync_minutes"] = max(0, int(self.i_interval.get() or 0))
            except ValueError:
                ic["auto_sync_minutes"] = 180
            personio_ical.set_url(st, self.i_url.get().strip())
            od = s.setdefault("onedrive", {})
            od["enabled"] = bool(self.od_enabled.get())
            try:
                od["sync_minutes"] = max(0, int(self.od_minutes.get() or 0))
            except ValueError:
                od["sync_minutes"] = 15
            secret = self.p_secret.get().strip()
            if secret:
                personio.set_secret(st, secret)
            st.set_schedule(v["days"], self.e_valid_from.get_date() if self.valid_from_var.get() else None)
        try:
            winutil.set_autostart(bool(self.autostart_var.get()))
        except OSError as e:
            messagebox.showwarning("Autostart", f"Autostart konnte nicht gesetzt werden: {e}", parent=self)
        self.app.refresh_all()
        if close:
            self.close()
        return True

    def save_and_sync(self):
        if self.save(close=False):
            self.lbl_p_status.config(text="Synchronisiere …")
            self.app.sync_personio(manual=True, on_done=self._sync_done, sources=["api"])

    # ---------------------------------------------------------------- OneDrive
    def _od_save_cfg(self) -> bool:
        od = self.store.settings.setdefault("onedrive", {})
        od["enabled"] = bool(self.od_enabled.get())
        try:
            od["sync_minutes"] = max(0, int(self.od_minutes.get() or 0))
        except ValueError:
            od["sync_minutes"] = 15
        self.store.save()
        return True

    def od_refresh_status(self):
        import onedrive as od_mod
        try:
            st = od_mod.SyncState(self.store.path.parent / "onedrive_state.json")
            txt = st["last_result"] or "Noch nicht abgeglichen."
            if st["last_sync"]:
                txt = f"Letzter Abgleich: {parse_dt(st['last_sync']):%d.%m.%Y %H:%M}\n{txt}"
            conflict = bool(st["conflict"])
        except Exception:
            txt, conflict = "", False
        self.lbl_od_status.config(text=txt)
        if conflict:
            self.od_conflict_frame.grid()
        else:
            self.od_conflict_frame.grid_remove()
        acc = self.store.settings.get("onedrive", {}).get("account")
        self.lbl_od_account.config(text=acc or "nicht angemeldet")

    def od_login(self):
        self._od_save_cfg()
        self.lbl_od_status.config(text="Browser wird geöffnet – bitte bei Microsoft anmelden …")

        def job():
            self.app.onedrive()
            return self.app.od_auth.login_interactive()

        def done(result, error):
            if not self.winfo_exists():
                return
            if error:
                self.lbl_od_status.config(text=f"❌ {error}")
                return
            with self.store.lock:
                self.store.settings.setdefault("onedrive", {})["account"] = result
                self.store.save()
            self.lbl_od_account.config(text=result)
            self.lbl_od_status.config(text=f"✓ Angemeldet als {result}. Erster Abgleich läuft …")
            if self.od_enabled.get():
                self.app.sync_onedrive(manual=True, on_done=lambda t: self.winfo_exists() and self.od_refresh_status())
        self.app.run_bg(job, done)

    def od_logout(self):
        try:
            self.app.onedrive()
            self.app.od_auth.logout()
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("OneDrive", str(e), parent=self)
        with self.store.lock:
            self.store.settings.setdefault("onedrive", {}).pop("account", None)
            self.store.save()
        self.od_refresh_status()

    def od_sync_now(self):
        self._od_save_cfg()
        self.lbl_od_status.config(text="Gleiche ab …")
        self.app.sync_onedrive(manual=True, on_done=lambda t: self.winfo_exists() and self.od_refresh_status())

    def od_resolve(self, use: str):
        what = ("den Stand von OneDrive übernehmen? Der lokale Stand wird als Sicherung aufbewahrt." if use == "remote"
                else "diesen lokalen Stand auf OneDrive hochladen? Der OneDrive-Stand wird als Sicherung aufbewahrt.")
        if not messagebox.askyesno("OneDrive", f"Wirklich {what}", parent=self):
            return

        def done(result, error):
            self.app.refresh_all()
            if self.winfo_exists():
                self.od_refresh_status()
                if error:
                    self.lbl_od_status.config(text=f"❌ {error}")
        self.app.run_bg(lambda: self.app.onedrive().resolve(use), done)

    def update_ical_state(self):
        st = ["!disabled"] if self.i_enabled.get() else ["disabled"]
        for w in self.i_widgets:
            w.state(st)

    def save_and_load_ical(self):
        if not self.save(close=False):
            return
        self.lbl_i_status.config(text="Lade Kalender …")

        def done(text):
            if self.winfo_exists():
                self.lbl_i_status.config(text=text)
        self.app.sync_personio(manual=True, on_done=done, sources=["ical"])

    def _sync_done(self, text):
        if self.winfo_exists():
            self.lbl_p_status.config(text=text)
            self.lbl_pid.config(text=f"Personio-ID: {self.store.settings['personio'].get('person_id') or '–'}")


# =========================================================================== Saldo-Verlauf

class BalanceWindow(BaseWindow):
    key = "balance"
    CHART_H = 270

    def __init__(self, app, **kw):
        super().__init__(app, "Saldo-Verlauf")
        self.geometry(f"{px(1000)}x{px(760)}")
        self.minsize(px(760), px(560))
        today = date.today()
        self.series = None
        self.points: list = []

        top = ttk.Frame(self, padding=(PAD, PAD, PAD, 0))
        top.pack(fill="x")
        ttk.Label(top, text="Von:").pack(side="left")
        self.e_from = date_entry(top, date(today.year, 1, 1))
        self.e_from.pack(side="left", padx=(4, 10))
        ttk.Label(top, text="Bis:").pack(side="left")
        self.e_to = date_entry(top, today)
        self.e_to.pack(side="left", padx=(4, 10))
        ttk.Button(top, text="Anzeigen", command=self.refresh).pack(side="left")
        for ev in ("<<DateEntrySelected>>", "<Return>"):
            self.e_from.bind(ev, lambda e: self.refresh())
            self.e_to.bind(ev, lambda e: self.refresh())

        quick = ttk.Frame(self, padding=(PAD, 6, PAD, 0))
        quick.pack(fill="x")
        ttk.Label(quick, text="Schnellwahl:", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        for label, fn in (("Dieser Monat", self.q_month), ("Letzter Monat", self.q_last_month),
                          ("Dieses Jahr", lambda: self.q_year(0)), ("Letztes Jahr", lambda: self.q_year(-1)),
                          ("Letzte 12 Monate", self.q_12m), ("Gesamt", self.q_all)):
            ttk.Button(quick, text=label, command=fn).pack(side="left", padx=2)

        self.lbl_sum = ttk.Label(self, padding=(PAD, 10, PAD, 4), style="Big.TLabel", justify="left")
        self.lbl_sum.pack(fill="x")

        self.canvas = tk.Canvas(self, height=px(self.CHART_H), background="white", highlightthickness=1,
                                highlightbackground="#cccccc")
        self.canvas.pack(fill="x", padx=PAD)
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete("hover"))

        mid = ttk.Frame(self, padding=(PAD, 8, PAD, 0))
        mid.pack(fill="x")
        ttk.Label(mid, text="Tabelle nach:").pack(side="left")
        self.group_var = tk.StringVar(value="Monat")
        for g in ("Tag", "Woche", "Monat"):
            ttk.Radiobutton(mid, text=g, value=g, variable=self.group_var, command=self.fill_table).pack(side="left", padx=4)
        ttk.Button(mid, text="CSV exportieren…", command=self.export_csv).pack(side="right")

        tf = ttk.Frame(self, padding=PAD)
        tf.pack(fill="both", expand=True)
        cols = ("arbeit", "soll", "diff", "saldo")
        self.tree = ttk.Treeview(tf, columns=cols, selectmode="browse")
        self.tree.heading("#0", text="Zeitraum")
        self.tree.column("#0", width=px(260))
        for c, h in zip(cols, ("Gearbeitet", "Soll", "Differenz", "Saldo am Ende")):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=px(120), anchor="e")
        self.tree.tag_configure("neg", foreground="#b71c1c")
        self.tree.tag_configure("pos", foreground="#1b5e20")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.rows: list = []
        self.refresh()

    # ---------------------------------------------------------------- Schnellwahl
    def _set(self, a: date, b: date):
        self.e_from.set_date(a)
        self.e_to.set_date(b)
        self.refresh()

    def q_month(self):
        t = date.today()
        self._set(t.replace(day=1), t)

    def q_last_month(self):
        first = date.today().replace(day=1)
        last = first - timedelta(days=1)
        self._set(last.replace(day=1), last)

    def q_year(self, delta: int):
        y = date.today().year + delta
        self._set(date(y, 1, 1), min(date(y, 12, 31), date.today()))

    def q_12m(self):
        t = date.today()
        self._set(date(t.year - 1, t.month, 1) if t.month > 1 else date(t.year - 1, 1, 1), t)

    def q_all(self):
        self._set(self.store.tracking_start, date.today())

    # ---------------------------------------------------------------- Daten
    def refresh(self):
        a, b = self.e_from.get_date(), self.e_to.get_date()
        if b < a:
            a, b = b, a
        self.series = self.store.balance_series(a, b)
        days = self.series["days"]
        if not days:
            self.lbl_sum.config(text="Im gewählten Zeitraum liegen keine erfassten Tage "
                                     f"(Erfassung seit {fmt_date(self.store.tracking_start)}).")
        else:
            worked = sum(d["worked"] for d in days)
            target = sum(d["target"] for d in days)
            bals = [d["balance"] for d in days]
            i_min, i_max = bals.index(min(bals)), bals.index(max(bals))
            running = " (heute noch laufend)" if days[-1]["date"] == date.today() else ""
            self.lbl_sum.config(text=(
                f"{fmt_date(days[0]['date'])} – {fmt_date(days[-1]['date'])}{running}\n"
                f"Saldo zu Beginn: {fmt_hm(self.series['start_balance'], True)} h   ·   "
                f"gearbeitet {fmt_hm(worked)} h   ·   Soll {fmt_hm(target)} h   ·   "
                f"Differenz {fmt_hm(worked - target, True)} h   ·   Saldo am Ende: {fmt_hm(bals[-1], True)} h\n"
                f"Höchststand {fmt_hm(bals[i_max], True)} h am {fmt_date(days[i_max]['date'])}   ·   "
                f"Tiefststand {fmt_hm(bals[i_min], True)} h am {fmt_date(days[i_min]['date'])}"))
        self.draw()
        self.fill_table()

    def fill_table(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        if not self.series or not self.series["days"]:
            return
        mode = self.group_var.get()
        groups: dict = {}
        order = []
        for d in self.series["days"]:
            dt_ = d["date"]
            if mode == "Tag":
                key, label = dt_, f"{WEEKDAYS_SHORT[dt_.weekday()]}, {fmt_date(dt_)}"
            elif mode == "Woche":
                iy, iw, _ = dt_.isocalendar()
                mon = dt_ - timedelta(days=dt_.weekday())
                key, label = (iy, iw), f"KW {iw}/{iy}  ({mon:%d.%m.} – {mon + timedelta(days=6):%d.%m.})"
            else:
                key, label = (dt_.year, dt_.month), f"{MONTHS[dt_.month - 1]} {dt_.year}"
            if key not in groups:
                groups[key] = {"label": label, "worked": 0.0, "target": 0.0, "balance": 0.0}
                order.append(key)
            g = groups[key]
            g["worked"] += d["worked"]
            g["target"] += d["target"]
            g["balance"] = d["balance"]
        for key in order:
            g = groups[key]
            diff = g["worked"] - g["target"]
            if mode == "Tag" and g["worked"] == 0 and g["target"] == 0:
                tag = ""
            else:
                tag = "neg" if diff < -0.5 else ("pos" if diff > 0.5 else "")
            self.rows.append((g["label"], g["worked"], g["target"], diff, g["balance"]))
            self.tree.insert("", "end", text=g["label"], tags=(tag,), values=(
                fmt_hm(g["worked"]), fmt_hm(g["target"]), fmt_hm(diff, True), fmt_hm(g["balance"], True)))
        kids = self.tree.get_children()
        if kids:
            self.tree.see(kids[-1])

    # ---------------------------------------------------------------- Diagramm
    def draw(self):
        c = self.canvas
        c.delete("all")
        self.points = []
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or not self.series or not self.series["days"]:
            return
        days = self.series["days"]
        vals = [self.series["start_balance"]] + [d["balance"] for d in days]
        dates = [days[0]["date"] - timedelta(days=1)] + [d["date"] for d in days]
        L, R, T, B = px(70), px(16), px(16), px(30)
        lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
        if hi - lo < 60:
            hi, lo = hi + 30, lo - 30
        pad = (hi - lo) * 0.08
        lo, hi = lo - pad, hi + pad
        n = len(vals)
        X = lambda i: L + (w - L - R) * (i / max(1, n - 1))
        Y = lambda v: T + (h - T - B) * (hi - v) / (hi - lo)

        # Rasterlinien + Beschriftung y (in ganzen Stunden)
        span_h = (hi - lo) / 60
        step_h = next((s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200) if span_h / s <= 6), 500)
        v = (int(lo / 60 / step_h) - 1) * step_h * 60
        while v <= hi:
            if v >= lo:
                y = Y(v)
                c.create_line(L, y, w - R, y, fill="#eeeeee")
                c.create_text(L - px(6), y, text=fmt_hm(v, True) + " h", anchor="e", fill="#666666", font=("Segoe UI", 8))
            v += step_h * 60
        y0 = Y(0)
        c.create_line(L, y0, w - R, y0, fill="#888888", width=1)

        # x-Beschriftung
        span = (dates[-1] - dates[0]).days
        last_x = -999
        for i, d in enumerate(dates):
            if span <= 14:
                mark, text = True, f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m.}"
            elif span <= 45:
                mark, text = d.weekday() == 0, f"{d:%d.%m.}"
            elif span <= 420:
                mark, text = d.day == 1, f"{MONTHS[d.month - 1][:3]} {d:%y}"
            else:
                mark, text = d.day == 1 and d.month in (1, 4, 7, 10), \
                    (str(d.year) if d.month == 1 else f"{MONTHS[d.month - 1][:3]}")
            if mark and X(i) - last_x > px(40):
                x = X(i)
                c.create_line(x, T, x, h - B, fill="#f3f3f3")
                c.create_text(x, h - B + px(4), text=text, anchor="n", fill="#666666", font=("Segoe UI", 8))
                last_x = x

        # Fläche + Linie (grün über 0, rot unter 0)
        self.points = [(X(i), Y(v_), dates[i], v_) for i, v_ in enumerate(vals)]
        poly = [L, y0] + [p for x, y, _, _ in self.points for p in (x, y)] + [self.points[-1][0], y0]
        c.create_polygon(*poly, fill="#e3eef9", outline="")
        c.create_line(L, y0, w - R, y0, fill="#888888")
        for (x1, y1, _, v1), (x2, y2, _, v2) in zip(self.points, self.points[1:]):
            col = "#2e7d32" if (v1 + v2) / 2 >= 0 else "#c62828"
            c.create_line(x1, y1, x2, y2, fill=col, width=px(2))
        x, y, _, v_ = self.points[-1]
        c.create_oval(x - px(3), y - px(3), x + px(3), y + px(3), fill="#1565c0", outline="")

    def on_motion(self, e):
        c = self.canvas
        c.delete("hover")
        if not self.points:
            return
        i = min(range(len(self.points)), key=lambda k: abs(self.points[k][0] - e.x))
        x, y, d, v = self.points[i]
        c.create_line(x, px(10), x, c.winfo_height() - px(30), fill="#1565c0", dash=(2, 2), tags="hover")
        c.create_oval(x - px(4), y - px(4), x + px(4), y + px(4), outline="#1565c0", width=px(2), tags="hover")
        day = self.series["days"][i - 1] if i > 0 else None
        txt = f"{WEEKDAYS_SHORT[d.weekday()]}, {fmt_date(d)}:  Saldo {fmt_hm(v, True)} h"
        if day:
            txt += f"  (Tag {fmt_hm(day['diff'], True)} h)"
        else:
            txt += "  (vor dem Zeitraum)"
        anchor = "nw" if x < c.winfo_width() * 0.6 else "ne"
        tid = c.create_text(x + (px(8) if anchor == "nw" else -px(8)), px(14), text=txt, anchor=anchor,
                            font=("Segoe UI", 9, "bold"), tags="hover")
        bx = c.bbox(tid)
        rid = c.create_rectangle(bx[0] - px(4), bx[1] - px(2), bx[2] + px(4), bx[3] + px(2), fill="white", outline="#1565c0", tags="hover")
        c.tag_lower(rid, tid)

    # ---------------------------------------------------------------- Export
    def export_csv(self):
        if not self.rows:
            return
        a, b = self.series["start"], self.series["end"]
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv",
                                            initialfile=f"Saldo-{a:%Y%m%d}-{b:%Y%m%d}-{self.group_var.get()}.csv",
                                            filetypes=[("CSV-Datei", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["Zeitraum", "Gearbeitet (h)", "Soll (h)", "Differenz (h)", "Saldo am Ende (h)"])
            w.writerow([f"Saldo vor {fmt_date(a)}", "", "", "", fmt_hm(self.series["start_balance"], True)])
            for label, worked, target, diff, bal in self.rows:
                w.writerow([label, fmt_hm(worked), fmt_hm(target), fmt_hm(diff, True), fmt_hm(bal, True)])
        messagebox.showinfo("Export", f"Gespeichert:\n{path}", parent=self)


def ask_holiday_region(app) -> None:
    """Beim ersten Start nach dem Bundesland fragen, damit Feiertage automatisch eingerechnet werden."""
    store = app.store
    if _holidays is None:
        return
    dlg = tk.Toplevel(app.root)
    dlg.title("ZeitTracker – Feiertage")
    dlg.resizable(False, False)
    dlg.attributes("-topmost", True)
    f = ttk.Frame(dlg, padding=16)
    f.pack(fill="both", expand=True)
    ttk.Label(f, text="In welchem Bundesland arbeitest du?", style="Header.TLabel").pack(anchor="w")
    ttk.Label(f, style="Muted.TLabel", justify="left", text=(
        "Gesetzliche Feiertage werden dann automatisch erkannt: An Feiertagen entfällt das Soll,\n"
        "Arbeit an einem Feiertag zählt komplett als Überstunden. Später änderbar unter\n"
        "Einstellungen › Arbeitszeit.")).pack(anchor="w", pady=(4, 10))
    var = tk.StringVar(value="")
    cb = ttk.Combobox(f, textvariable=var, values=[v for k, v in BUNDESLAENDER.items() if k], state="readonly", width=34)
    cb.pack(anchor="w")

    def done(save: bool):
        with store.lock:
            if save:
                code = next((k for k, v in BUNDESLAENDER.items() if v == var.get() and k), "")
                if not code:
                    messagebox.showinfo("Bundesland", "Bitte ein Bundesland auswählen.", parent=dlg)
                    return
                store.settings["holiday_region"] = code
                store._holiday_cache.clear()
            store.settings["holiday_region_asked"] = True
            store.touch()
        dlg.destroy()
        app.refresh_all()
    bf = ttk.Frame(f)
    bf.pack(fill="x", pady=(12, 0))
    ttk.Button(bf, text="Übernehmen", command=lambda: done(True)).pack(side="right")
    ttk.Button(bf, text="Keine Feiertage", command=lambda: done(False)).pack(side="right", padx=6)
    dlg.protocol("WM_DELETE_WINDOW", lambda: done(False))
    dlg.focus_force()


WINDOWS = {
    "overview": OverviewWindow,
    "history": HistoryWindow,
    "absences": AbsenceWindow,
    "settings": SettingsWindow,
    "balance": BalanceWindow,
}
