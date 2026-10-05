"""Grafische Oberfläche."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from . import __version__, scheduler
from .browsers import CATALOG, FIREFOX_FAMILY, detect_browsers, running_process_names
from .cleaner import CHROMIUM_LAYOUT, FIREFOX_LAYOUT, count_cookies
from .cli import format_size
from .config import (
    Configuration,
    clamp,
    data_directory,
    load_configuration,
    load_state,
    normalize_domain,
    save_configuration,
)
from .engine import AGE_MODE, FULL_MODE, RunReport, next_full_reset_date, read_history, run_manual
from .widgets import DARK, LIGHT, Button, Card, Slider, Switch, configure_styles, darken_title_bar, font, label, system_prefers_dark

WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
UNIT_LABELS = {"days": "Tage", "weeks": "Wochen", "months": "Monate"}
MODE_LABELS = {AGE_MODE: "Altersbereinigung", FULL_MODE: "Vollständiger Neustart"}
STATUS_LABELS = {"ok": "Bereinigt", "locked": "Gesperrt", "missing": "Nicht gefunden", "error": "Fehler"}


def enable_sharp_rendering() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


class Page(tk.Frame):
    title = ""
    subtitle = ""

    def __init__(self, application: "Application"):
        super().__init__(application.content, background=application.palette["background"])
        self.application = application
        self.palette = application.palette
        self.configuration = application.configuration
        heading = tk.Frame(self, background=self.palette["background"])
        heading.pack(fill="x", pady=(0, 14))
        tk.Label(heading, text=self.title, font=font(20, "bold"), background=self.palette["background"], foreground=self.palette["text"]).pack(anchor="w")
        tk.Label(heading, text=self.subtitle, font=font(10), background=self.palette["background"], foreground=self.palette["muted"]).pack(anchor="w")

    def watch(self, *variables: tk.Variable) -> None:
        for variable in variables:
            variable.trace_add("write", lambda *arguments: self.application.changed())

    def apply(self, configuration: Configuration) -> None:
        pass

    def refresh(self) -> None:
        pass


def spinbox(master, variable, low, high, width=5, step=1, wrap=False, digits=0) -> ttk.Spinbox:
    """Zahlenfeld mit Pfeilen, in das sich auch direkt tippen lässt; ungültige Eingaben werden beim Verlassen korrigiert."""
    text = tk.StringVar()

    def formatted(value: int) -> str:
        return f"{value:0{digits}d}"

    def current() -> int:
        try:
            return min(max(int(variable.get()), low), high)
        except (tk.TclError, ValueError):
            return low

    def from_variable(*arguments) -> None:
        if text.get() != formatted(current()):
            text.set(formatted(current()))

    def from_text(*arguments) -> None:
        content = text.get()
        if content.isdigit() and low <= int(content) <= high:
            value = int(content)
            variable.set(formatted(value) if isinstance(variable, tk.StringVar) else value)

    def commit(event=None) -> None:
        from_text()
        text.set(formatted(current()))

    def allowed(proposed: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= len(str(high)))

    box = ttk.Spinbox(
        master, from_=low, to=high, increment=step, wrap=wrap, textvariable=text, width=width,
        format=f"%0{digits}.0f" if digits else "%.0f", validate="key", validatecommand=(master.register(allowed), "%P"),
    )
    from_variable()
    variable.trace_add("write", from_variable)
    text.trace_add("write", from_text)
    box.bind("<FocusOut>", commit)
    box.bind("<Return>", commit)
    return box


def safe_integer(variable: tk.Variable, default: int) -> int:
    try:
        return int(variable.get())
    except (tk.TclError, ValueError):
        return default


class OverviewPage(Page):
    title = "Übersicht"
    subtitle = "Wählen Sie die Browser aus und starten Sie die Bereinigung."

    def __init__(self, application: "Application"):
        super().__init__(application)
        self.messages: queue.Queue = queue.Queue()
        self.switches: dict[str, tk.BooleanVar] = {}
        self.count_labels: dict[str, tk.Label] = {}
        self.running_labels: dict[str, tk.Label] = {}
        self.busy = False

        browser_card = Card(self, self.palette, "Browser", "Erkannte Browser mit gespeicherten Cookies. Aktivierte Browser werden bereinigt.")
        browser_card.pack(fill="x")
        self.detected = detect_browsers()
        if not self.detected:
            label(browser_card.body, self.palette, "Es wurden keine unterstützten Browser gefunden.", muted=True).pack(anchor="w")
        if self.detected:
            self._all_browsers_row(browser_card.body)
        for definition, profiles in self.detected.items():
            self._browser_row(browser_card.body, definition, profiles)
        self._sync_all_browsers()

        action_card = Card(self, self.palette, "Bereinigung")
        action_card.pack(fill="x", pady=(14, 0))
        row = tk.Frame(action_card.body, background=self.palette["surface"])
        row.pack(fill="x")
        self.preview_button = Button(row, self.palette, "Vorschau", lambda: self.start(AGE_MODE, True))
        self.clean_button = Button(row, self.palette, "Jetzt bereinigen", lambda: self.start(AGE_MODE, False), primary=True)
        self.reset_button = Button(row, self.palette, "Vollständiger Neustart", lambda: self.start(FULL_MODE, False), danger=True)
        for button in (self.clean_button, self.preview_button, self.reset_button):
            button.pack(side="left", padx=(0, 10))
        self.summary = label(action_card.body, self.palette, "Die Vorschau zeigt, was gelöscht würde, ohne etwas zu verändern.", muted=True)
        self.summary.pack(anchor="w", pady=(12, 0))

        result_card = Card(self, self.palette, "Ergebnis")
        result_card.pack(fill="both", expand=True, pady=(14, 0))
        columns = ("browser", "profile", "cookies", "site", "size", "status")
        self.tree = ttk.Treeview(result_card.body, columns=columns, show="headings", height=5, selectmode="none")
        for column, heading, width, anchor in (
            ("browser", "Browser", 160, "w"),
            ("profile", "Profil", 150, "w"),
            ("cookies", "Cookies", 110, "e"),
            ("site", "Websitedaten", 130, "e"),
            ("size", "Speicher", 100, "e"),
            ("status", "Status", 170, "w"),
        ):
            self.tree.heading(column, text=heading, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor, stretch=column in ("status", "profile"))
        self.tree.pack(fill="both", expand=True)

        threading.Thread(target=self._count_all, daemon=True).start()
        self.after(100, self._poll)

    def _all_browsers_row(self, master) -> None:
        row = tk.Frame(master, background=self.palette["surface"])
        row.pack(fill="x", pady=4)
        self.all_browsers = tk.BooleanVar()
        self.syncing_all_browsers = False
        Switch(row, self.palette, self.all_browsers, command=self._toggle_all_browsers).pack(side="left")
        label(row, self.palette, "Alle Browser", weight="bold").pack(side="left", padx=(12, 0))
        tk.Frame(master, height=1, background=self.palette["border"]).pack(fill="x", pady=(6, 4))

    def _toggle_all_browsers(self) -> None:
        self.syncing_all_browsers = True
        for variable in self.switches.values():
            variable.set(self.all_browsers.get())
        self.syncing_all_browsers = False

    def _sync_all_browsers(self) -> None:
        if self.detected and not self.syncing_all_browsers:
            self.all_browsers.set(all(variable.get() for variable in self.switches.values()))

    def _browser_row(self, master, definition, profiles) -> None:
        row = tk.Frame(master, background=self.palette["surface"])
        row.pack(fill="x", pady=4)
        variable = tk.BooleanVar(value=definition.key in self.configuration.browsers)
        self.switches[definition.key] = variable
        Switch(row, self.palette, variable).pack(side="left")
        self.watch(variable)
        variable.trace_add("write", lambda *arguments: self._sync_all_browsers())
        label(row, self.palette, definition.name, weight="bold").pack(side="left", padx=(12, 0))
        detail = f"{len(profiles)} Profil" + ("e" if len(profiles) != 1 else "")
        label(row, self.palette, detail, muted=True, size=9).pack(side="left", padx=(10, 0))
        running = label(row, self.palette, "", size=9)
        running.pack(side="left", padx=(10, 0))
        self.running_labels[definition.key] = running
        count = label(row, self.palette, "…", muted=True, size=9)
        count.pack(side="right")
        self.count_labels[definition.key] = count

    def _count_all(self) -> None:
        running = running_process_names()
        for definition, profiles in self.detected.items():
            layout = FIREFOX_LAYOUT if definition.family == FIREFOX_FAMILY else CHROMIUM_LAYOUT
            counts = [count_cookies(profile.cookie_database, layout) for profile in profiles]
            known = [count for count in counts if count is not None]
            self.messages.put(("count", definition.key, sum(known) if known else None, definition.process_name in running))

    def _poll(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                if message[0] == "count":
                    _, key, total, is_running = message
                    self.count_labels[key].configure(text="–" if total is None else f"{total:,} Cookies".replace(",", "."))
                    if is_running:
                        self.running_labels[key].configure(text="● läuft", foreground=self.palette["warning"])
                elif message[0] == "report":
                    self._show_report(message[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def apply(self, configuration: Configuration) -> None:
        configuration.browsers = [key for key, variable in self.switches.items() if variable.get()]

    def start(self, mode: str, dry_run: bool) -> None:
        if self.busy:
            return
        self.application.commit()
        if not self.configuration.browsers:
            messagebox.showinfo("Kein Browser ausgewählt", "Aktivieren Sie mindestens einen Browser.")
            return
        if not dry_run:
            question = (
                "Alle Cookies außer den Ausnahmen werden gelöscht. Fortfahren?"
                if mode == FULL_MODE
                else f"Cookies, die seit {self.configuration.age.max_age_days} Tagen nicht verwendet wurden, werden gelöscht. Fortfahren?"
            )
            if not messagebox.askyesno("Bereinigung starten", question, icon="warning"):
                return
        self.busy = True
        for button in (self.preview_button, self.clean_button, self.reset_button):
            button.set_enabled(False)
        self.summary.configure(text="Läuft …", foreground=self.palette["muted"])
        threading.Thread(target=self._work, args=(mode, dry_run), daemon=True).start()

    def _work(self, mode: str, dry_run: bool) -> None:
        self.messages.put(("report", run_manual(self.configuration, mode, dry_run)))

    def _show_report(self, report: RunReport) -> None:
        self.busy = False
        for button in (self.preview_button, self.clean_button, self.reset_button):
            button.set_enabled(True)
        self.tree.delete(*self.tree.get_children())
        for profile in report.profiles:
            status = STATUS_LABELS.get(profile.status, profile.status)
            if report.dry_run and profile.status == "ok":
                status = "Vorschau"
            self.tree.insert(
                "", "end",
                values=(
                    profile.browser, profile.profile,
                    f"{profile.cookies_deleted} von {profile.cookies_total}" if profile.status == "ok" else "–",
                    profile.site_data_folders or "–",
                    format_size(profile.bytes_freed) if profile.bytes_freed else "–",
                    status,
                ),
            )
        verb = "würden gelöscht" if report.dry_run else "gelöscht"
        text = f"{report.cookies_deleted} Cookies und {report.site_data_folders} Websitedaten-Ordner ({format_size(report.bytes_freed)}) {verb}."
        colour = self.palette["text"]
        if report.blocked_profiles:
            text += f" {report.blocked_profiles} Profil(e) gesperrt – Browser schließen und erneut starten."
            colour = self.palette["warning"]
        self.summary.configure(text=text, foreground=colour)


class RulesPage(Page):
    title = "Regeln"
    subtitle = "Legen Sie fest, wann und was gelöscht wird."

    def __init__(self, application: "Application"):
        super().__init__(application)
        configuration = self.configuration
        columns = tk.Frame(self, background=self.palette["background"])
        columns.pack(fill="both", expand=True)
        columns.columnconfigure((0, 1), weight=1, uniform="columns")
        left = tk.Frame(columns, background=self.palette["background"])
        right = tk.Frame(columns, background=self.palette["background"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        self._age_card(left)
        self._reset_card(left)
        self._exceptions_card(right)
        self._options_card(right)

    def _toggle_row(self, master, text, variable) -> tk.Frame:
        row = tk.Frame(master, background=self.palette["surface"])
        row.pack(fill="x")
        Switch(row, self.palette, variable).pack(side="left")
        label(row, self.palette, text, weight="bold").pack(side="left", padx=(12, 0))
        self.watch(variable)
        return row

    def _age_card(self, master) -> None:
        age = self.configuration.age
        card = Card(master, self.palette, "Altersbereinigung", "Löscht Cookies, die über einen bestimmten Zeitraum nicht verwendet wurden.")
        card.pack(fill="x")
        self.age_enabled = tk.BooleanVar(value=age.enabled)
        self._toggle_row(card.body, "Aktiv", self.age_enabled)
        row = tk.Frame(card.body, background=self.palette["surface"])
        row.pack(fill="x", pady=(14, 0))
        self.age_days = tk.IntVar(value=min(age.max_age_days, 365))
        Slider(row, self.palette, self.age_days, 1, 365).pack(side="left", fill="x", expand=True)
        spinbox(row, self.age_days, 1, 365).pack(side="left", padx=(14, 6))
        label(row, self.palette, "Tage").pack(side="left")
        self.watch(self.age_days)

    def _reset_card(self, master) -> None:
        rule = self.configuration.full_reset
        card = Card(master, self.palette, "Vollständiger Neustart", "Löscht in regelmäßigen Abständen alle Cookies außer den Ausnahmen. Verpasste Termine werden nachgeholt.")
        card.pack(fill="x", pady=(14, 0))
        self.reset_enabled = tk.BooleanVar(value=rule.enabled)
        self._toggle_row(card.body, "Aktiv", self.reset_enabled)

        interval = tk.Frame(card.body, background=self.palette["surface"])
        interval.pack(fill="x", pady=(14, 0))
        label(interval, self.palette, "Alle").pack(side="left")
        self.reset_every = tk.IntVar(value=rule.every)
        spinbox(interval, self.reset_every, 1, 365, width=4).pack(side="left", padx=8)
        self.reset_unit = tk.StringVar(value=UNIT_LABELS[rule.unit])
        unit = ttk.Combobox(interval, textvariable=self.reset_unit, values=list(UNIT_LABELS.values()), state="readonly", width=9)
        unit.pack(side="left")
        unit.bind("<<ComboboxSelected>>", lambda event: self._unit_changed())

        self.detail_row = tk.Frame(card.body, background=self.palette["surface"])
        self.detail_row.pack(fill="x", pady=(10, 0))
        self.reset_weekday = tk.StringVar(value=WEEKDAYS[rule.weekday])
        self.reset_day = tk.IntVar(value=rule.day_of_month)
        self.weekday_box = ttk.Combobox(self.detail_row, textvariable=self.reset_weekday, values=WEEKDAYS, state="readonly", width=12)
        self.day_box = spinbox(self.detail_row, self.reset_day, 1, 28, width=4)
        self.detail_prefix = label(self.detail_row, self.palette, "am")
        self.detail_suffix = label(self.detail_row, self.palette, "des Monats")
        self.next_reset = label(card.body, self.palette, "", muted=True, size=9)
        self.next_reset.pack(anchor="w", pady=(10, 0))
        self._unit_changed(save=False)
        self.watch(self.reset_enabled, self.reset_every, self.reset_unit, self.reset_weekday, self.reset_day)
        for variable in (self.reset_enabled, self.reset_every, self.reset_unit, self.reset_weekday, self.reset_day):
            variable.trace_add("write", lambda *arguments: self._update_next_reset())

    def _selected_unit(self) -> str:
        return next((key for key, text in UNIT_LABELS.items() if text == self.reset_unit.get()), "months")

    def _unit_changed(self, save: bool = True) -> None:
        for widget in (self.detail_prefix, self.weekday_box, self.day_box, self.detail_suffix):
            widget.pack_forget()
        unit = self._selected_unit()
        if unit == "weeks":
            self.detail_prefix.pack(side="left")
            self.weekday_box.pack(side="left", padx=8)
        elif unit == "months":
            self.detail_prefix.pack(side="left")
            self.day_box.pack(side="left", padx=8)
            self.detail_suffix.pack(side="left")
        self._update_next_reset()

    def _update_next_reset(self) -> None:
        if not self.reset_enabled.get():
            self.next_reset.configure(text="Deaktiviert")
            return
        probe = Configuration().full_reset
        probe.unit = self._selected_unit()
        probe.every = safe_integer(self.reset_every, 1)
        probe.weekday = WEEKDAYS.index(self.reset_weekday.get()) if self.reset_weekday.get() in WEEKDAYS else 6
        probe.day_of_month = safe_integer(self.reset_day, 1)
        last = load_state().last_full_reset or datetime.now()
        following = next_full_reset_date(probe, last.date())
        self.next_reset.configure(text=f"Nächster Neustart: {WEEKDAYS[following.weekday()]}, {following:%d.%m.%Y}")

    def _exceptions_card(self, master) -> None:
        card = Card(master, self.palette, "Ausnahmen", "Cookies dieser Domains (inklusive Subdomains) werden nie gelöscht, damit Anmeldungen erhalten bleiben.")
        card.pack(fill="both", expand=True)
        entry_row = tk.Frame(card.body, background=self.palette["surface"])
        entry_row.pack(fill="x")
        self.domain_entry = tk.StringVar()
        entry = ttk.Entry(entry_row, textvariable=self.domain_entry)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda event: self._add_domain())
        Button(entry_row, self.palette, "Hinzufügen", self._add_domain).pack(side="left", padx=(8, 0))
        self.domain_message = label(card.body, self.palette, "z. B. github.com", muted=True, size=9)
        self.domain_message.pack(anchor="w", pady=(6, 6))

        list_row = tk.Frame(card.body, background=self.palette["surface"])
        list_row.pack(fill="both", expand=True)
        self.domain_list = tk.Listbox(
            list_row, height=4, font=font(10), activestyle="none", borderwidth=0, highlightthickness=1,
            highlightbackground=self.palette["border"], highlightcolor=self.palette["accent"],
            background=self.palette["background"], foreground=self.palette["text"],
            selectbackground=self.palette["accent"], selectforeground=self.palette["accent_text"],
        )
        scrollbar = ttk.Scrollbar(list_row, orient="vertical", command=self.domain_list.yview)
        self.domain_list.configure(yscrollcommand=scrollbar.set)
        self.domain_list.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        for domain in self.configuration.keep_domains:
            self.domain_list.insert("end", domain)
        self.domain_list.bind("<Delete>", lambda event: self._remove_domain())
        Button(card.body, self.palette, "Auswahl entfernen", self._remove_domain).pack(anchor="w", pady=(10, 0))

    def _add_domain(self) -> None:
        domain = normalize_domain(self.domain_entry.get())
        if domain is None:
            self.domain_message.configure(text="Keine gültige Domain.", foreground=self.palette["danger"])
            return
        if domain in self.domain_list.get(0, "end"):
            self.domain_message.configure(text="Bereits vorhanden.", foreground=self.palette["warning"])
            return
        entries = sorted([*self.domain_list.get(0, "end"), domain])
        self.domain_list.delete(0, "end")
        for entry in entries:
            self.domain_list.insert("end", entry)
        self.domain_entry.set("")
        self.domain_message.configure(text=f"{domain} hinzugefügt.", foreground=self.palette["success"])
        self.application.changed()

    def _remove_domain(self) -> None:
        for index in reversed(self.domain_list.curselection()):
            self.domain_list.delete(index)
        self.application.changed()

    def _options_card(self, master) -> None:
        card = Card(master, self.palette, "Weitere Optionen")
        card.pack(fill="x", pady=(14, 0))
        self.site_data = tk.BooleanVar(value=self.configuration.include_site_data)
        self._toggle_row(card.body, "Websitedaten mitlöschen", self.site_data)
        label(card.body, self.palette, "Local Storage und IndexedDB von Websites ohne verbleibende Cookies (Firefox-Familie).", muted=True, size=9, wraplength=330, justify="left").pack(anchor="w", pady=(4, 14))

        self.backups = tk.BooleanVar(value=self.configuration.create_backups)
        self._toggle_row(card.body, "Sicherung vor dem Löschen", self.backups)
        keep_row = tk.Frame(card.body, background=self.palette["surface"])
        keep_row.pack(fill="x", pady=(8, 0))
        label(keep_row, self.palette, "Kopien pro Profil behalten:", muted=True, size=9).pack(side="left")
        self.backups_to_keep = tk.IntVar(value=self.configuration.backups_to_keep)
        spinbox(keep_row, self.backups_to_keep, 1, 50, width=4).pack(side="left", padx=8)
        self.watch(self.backups_to_keep)

    def apply(self, configuration: Configuration) -> None:
        configuration.age.enabled = self.age_enabled.get()
        configuration.age.max_age_days = safe_integer(self.age_days, 21)
        rule = configuration.full_reset
        rule.enabled = self.reset_enabled.get()
        rule.unit = self._selected_unit()
        rule.every = clamp(safe_integer(self.reset_every, 1), 1, {"days": 365, "weeks": 52, "months": 12}[rule.unit], 1)
        rule.weekday = WEEKDAYS.index(self.reset_weekday.get()) if self.reset_weekday.get() in WEEKDAYS else 6
        rule.day_of_month = safe_integer(self.reset_day, 1)
        configuration.keep_domains = list(self.domain_list.get(0, "end"))
        configuration.include_site_data = self.site_data.get()
        configuration.create_backups = self.backups.get()
        configuration.backups_to_keep = safe_integer(self.backups_to_keep, 5)


class SchedulePage(Page):
    title = "Zeitplan"
    subtitle = "Die Bereinigung läuft automatisch im Hintergrund, ohne dass dieses Fenster geöffnet sein muss."

    def __init__(self, application: "Application"):
        super().__init__(application)
        rule = self.configuration.schedule
        card = Card(self, self.palette, "Automatische Ausführung", "Richtet eine Aufgabe in der Windows-Aufgabenplanung ein. Jeder Lauf wählt selbst, ob Altersbereinigung oder vollständiger Neustart fällig ist.")
        card.pack(fill="x")
        toggle = tk.Frame(card.body, background=self.palette["surface"])
        toggle.pack(fill="x")
        self.enabled = tk.BooleanVar(value=rule.enabled)
        Switch(toggle, self.palette, self.enabled).pack(side="left")
        label(toggle, self.palette, "Täglich automatisch ausführen", weight="bold").pack(side="left", padx=(12, 0))

        time_row = tk.Frame(card.body, background=self.palette["surface"])
        time_row.pack(fill="x", pady=(16, 0))
        label(time_row, self.palette, "Uhrzeit").pack(side="left")
        hour, minute = rule.time.split(":")
        self.hour = tk.StringVar(value=hour)
        self.minute = tk.StringVar(value=minute)
        spinbox(time_row, self.hour, 0, 23, width=4, wrap=True, digits=2).pack(side="left", padx=(12, 4))
        label(time_row, self.palette, ":").pack(side="left")
        spinbox(time_row, self.minute, 0, 59, width=4, step=5, wrap=True, digits=2).pack(side="left", padx=(4, 0))

        logon_row = tk.Frame(card.body, background=self.palette["surface"])
        logon_row.pack(fill="x", pady=(16, 0))
        self.run_at_logon = tk.BooleanVar(value=rule.run_at_logon)
        Switch(logon_row, self.palette, self.run_at_logon).pack(side="left")
        label(logon_row, self.palette, "Zusätzlich bei jeder Windows-Anmeldung").pack(side="left", padx=(12, 0))
        label(card.body, self.palette, "War der Rechner zur eingestellten Uhrzeit aus, holt Windows den Lauf ohnehin nach, sobald er wieder läuft. Dieser Schalter führt zusätzlich bei jeder Anmeldung aus.", muted=True, size=9, wraplength=720, justify="left").pack(anchor="w", pady=(4, 0))

        self.status = label(card.body, self.palette, "", size=9)
        self.status.pack(anchor="w", pady=(18, 0))
        for variable in (self.enabled, self.hour, self.minute, self.run_at_logon):
            variable.trace_add("write", lambda *arguments: self.application.schedule_changed())
        self.refresh_status()

    def apply(self, configuration: Configuration) -> None:
        rule = configuration.schedule
        rule.enabled = self.enabled.get()
        rule.time = f"{safe_integer(self.hour, 12):02d}:{safe_integer(self.minute, 0):02d}"
        rule.run_at_logon = self.run_at_logon.get()

    def refresh_status(self, error: str = "") -> None:
        if error:
            self.status.configure(text=f"Fehler: {error}", foreground=self.palette["danger"])
            return
        installed = scheduler.is_installed()
        text = "● In der Windows-Aufgabenplanung eingerichtet" if installed else "○ Nicht eingerichtet"
        self.status.configure(text=text, foreground=self.palette["success"] if installed else self.palette["muted"])


class LogPage(Page):
    title = "Protokoll"
    subtitle = "Die letzten Bereinigungen, neueste zuerst."

    def __init__(self, application: "Application"):
        super().__init__(application)
        card = Card(self, self.palette, "Verlauf", f"Gespeichert in {data_directory()}")
        card.pack(fill="both", expand=True)
        columns = ("time", "mode", "cookies", "site", "size")
        self.tree = ttk.Treeview(card.body, columns=columns, show="headings", selectmode="none")
        for column, heading, width, anchor in (
            ("time", "Zeitpunkt", 170, "w"),
            ("mode", "Art", 190, "w"),
            ("cookies", "Cookies", 100, "e"),
            ("site", "Websitedaten", 120, "e"),
            ("size", "Speicher", 100, "e"),
        ):
            self.tree.heading(column, text=heading, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor)
        scrollbar = ttk.Scrollbar(card.body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        self.empty = label(card.body, self.palette, "Noch keine Bereinigung durchgeführt.", muted=True)

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        reports = read_history()
        for report in reports:
            self.tree.insert(
                "", "end",
                values=(
                    f"{report.started:%d.%m.%Y %H:%M}", MODE_LABELS.get(report.mode, report.mode),
                    report.cookies_deleted, report.site_data_folders, format_size(report.bytes_freed),
                ),
            )
        if reports:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.4, anchor="center")


class Application(tk.Tk):
    PAGES = (("overview", "Übersicht", OverviewPage), ("rules", "Regeln", RulesPage), ("schedule", "Zeitplan", SchedulePage), ("log", "Protokoll", LogPage))

    def __init__(self) -> None:
        enable_sharp_rendering()
        super().__init__()
        self.configuration = load_configuration()
        self.palette = DARK if system_prefers_dark() else LIGHT
        self.title("Cookie-Bereinigung")
        self.geometry("1060x860")
        self.minsize(980, 820)
        self.configure(background=self.palette["background"])
        configure_styles(self, self.palette)
        if self.palette is DARK:
            darken_title_bar(self)
        self.save_job: str | None = None
        self.schedule_job: str | None = None
        self.navigation: dict[str, tk.Label] = {}

        sidebar = tk.Frame(self, background=self.palette["sidebar"], width=210)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="Cookie-\nBereinigung", justify="left", font=font(15, "bold"), background=self.palette["sidebar"], foreground=self.palette["text"]).pack(anchor="w", padx=22, pady=(28, 26))
        self.content = tk.Frame(self, background=self.palette["background"])
        self.content.pack(side="left", fill="both", expand=True, padx=28, pady=(26, 24))
        self.pages: dict[str, Page] = {}
        for key, text, page_class in self.PAGES:
            self.pages[key] = page_class(self)
            item = tk.Label(sidebar, text=text, anchor="w", font=font(11), padx=22, pady=10, cursor="hand2", background=self.palette["sidebar"], foreground=self.palette["muted"])
            item.pack(fill="x")
            item.bind("<Button-1>", lambda event, target=key: self.show(target))
            item.bind("<Enter>", lambda event, target=key: self._hover(target, True))
            item.bind("<Leave>", lambda event, target=key: self._hover(target, False))
            self.navigation[key] = item
        tk.Label(sidebar, text=f"Version {__version__}", font=font(9), background=self.palette["sidebar"], foreground=self.palette["muted"]).pack(side="bottom", anchor="w", padx=22, pady=18)
        self.current = ""
        self.show("overview")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _hover(self, key: str, inside: bool) -> None:
        if key != self.current:
            self.navigation[key].configure(background=self.palette["hover"] if inside else self.palette["sidebar"])

    def show(self, key: str) -> None:
        if self.current:
            self.pages[self.current].pack_forget()
            self.navigation[self.current].configure(background=self.palette["sidebar"], foreground=self.palette["muted"], font=font(11))
        self.current = key
        self.navigation[key].configure(background=self.palette["surface"], foreground=self.palette["accent"], font=font(11, "bold"))
        self.pages[key].refresh()
        self.pages[key].pack(fill="both", expand=True)

    def commit(self) -> None:
        if self.save_job:
            self.after_cancel(self.save_job)
            self.save_job = None
        for page in self.pages.values():
            page.apply(self.configuration)
        save_configuration(self.configuration)

    def changed(self) -> None:
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(400, self.commit)

    def schedule_changed(self) -> None:
        self.changed()
        if self.schedule_job:
            self.after_cancel(self.schedule_job)
        self.schedule_job = self.after(900, self._apply_schedule)

    def _apply_schedule(self) -> None:
        self.schedule_job = None
        self.commit()
        page: SchedulePage = self.pages["schedule"]  # type: ignore[assignment]
        try:
            if self.configuration.schedule.enabled:
                scheduler.install(self.configuration.schedule)
            else:
                scheduler.remove()
            page.refresh_status()
        except scheduler.SchedulerError as error:
            page.refresh_status(str(error))

    def close(self) -> None:
        self.commit()
        self.destroy()


def launch() -> None:
    Application().mainloop()
