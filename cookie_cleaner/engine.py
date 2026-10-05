"""Ablaufsteuerung: Auswahl der Bereinigungsart und Ausführung über alle Profile."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping

from . import cleaner
from .browsers import CATALOG, FIREFOX_FAMILY, Profile, find_profiles
from .config import Configuration, FullResetRule, State, data_directory, load_state, save_state

AGE_MODE = "age"
FULL_MODE = "all"
HISTORY_LIMIT = 200


@dataclass
class ProfileReport:
    browser: str
    profile: str
    status: str
    message: str = ""
    cookies_total: int = 0
    cookies_deleted: int = 0
    site_data_folders: int = 0
    bytes_freed: int = 0


@dataclass
class RunReport:
    started: datetime
    mode: str
    dry_run: bool
    profiles: list[ProfileReport] = field(default_factory=list)

    @property
    def cookies_deleted(self) -> int:
        return sum(profile.cookies_deleted for profile in self.profiles)

    @property
    def site_data_folders(self) -> int:
        return sum(profile.site_data_folders for profile in self.profiles)

    @property
    def bytes_freed(self) -> int:
        return sum(profile.bytes_freed for profile in self.profiles)

    @property
    def blocked_profiles(self) -> int:
        return sum(profile.status == "locked" for profile in self.profiles)


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return day.replace(year=index // 12, month=index % 12 + 1, day=1)


def next_full_reset_date(rule: FullResetRule, last: date) -> date:
    """Frühester Tag des nächsten vollständigen Neustarts nach dem Neustart vom Tag `last`."""
    if rule.unit == "days":
        return last + timedelta(days=rule.every)
    following = last + timedelta(days=1)
    if rule.unit == "weeks":
        following += timedelta(days=(rule.weekday - following.weekday()) % 7)
        return following + timedelta(weeks=rule.every - 1)
    if following.day > rule.day_of_month:
        following = _add_months(following, 1)
    following = following.replace(day=rule.day_of_month)
    return _add_months(following, rule.every - 1).replace(day=rule.day_of_month)


def select_mode(configuration: Configuration, state: State, now: datetime) -> str | None:
    rule = configuration.full_reset
    if rule.enabled and state.last_full_reset is not None:
        if now.date() >= next_full_reset_date(rule, state.last_full_reset.date()):
            return FULL_MODE
    return AGE_MODE if configuration.age.enabled else None


def build_decision(configuration: Configuration, mode: str, now: datetime) -> cleaner.Decision:
    cutoff = now.timestamp() - configuration.age.max_age_days * 86400
    keep = configuration.keep_domains

    def decide(host: str, accessed: float) -> bool:
        if cleaner.is_kept(host, keep):
            return False
        return mode == FULL_MODE or accessed < cutoff

    return decide


def _clean_profile(
    profile: Profile, configuration: Configuration, decide: cleaner.Decision, dry_run: bool
) -> ProfileReport:
    is_firefox = profile.browser.family == FIREFOX_FAMILY
    layout = cleaner.FIREFOX_LAYOUT if is_firefox else cleaner.CHROMIUM_LAYOUT
    backup = None
    if configuration.create_backups:
        label = f"{profile.browser.key}_{profile.directory.name}"
        backup = cleaner.BackupPlan(data_directory() / "backups", label, configuration.backups_to_keep)

    outcome = cleaner.process_database(profile.cookie_database, layout, decide, dry_run, backup)
    report = ProfileReport(
        profile.browser.name,
        profile.name,
        outcome.status,
        outcome.message,
        outcome.total,
        outcome.deleted,
    )
    if outcome.status == "ok" and is_firefox and configuration.include_site_data:
        site_data = cleaner.clean_site_data(
            profile.directory, outcome.surviving_hosts, configuration.keep_domains, dry_run
        )
        report.site_data_folders = site_data.folders
        report.bytes_freed = site_data.bytes_freed
    return report


def run_cleanup(
    configuration: Configuration,
    mode: str,
    dry_run: bool,
    now: datetime | None = None,
    environment: Mapping[str, str] | None = None,
) -> RunReport:
    now = now or datetime.now()
    decide = build_decision(configuration, mode, now)
    report = RunReport(now, mode, dry_run)
    for definition in CATALOG:
        if definition.key not in configuration.browsers:
            continue
        for profile in find_profiles(definition, environment):
            report.profiles.append(_clean_profile(profile, configuration, decide, dry_run))
    return report


def run_scheduled(configuration: Configuration, now: datetime | None = None) -> RunReport | None:
    """Wählt die fällige Bereinigungsart, führt sie aus und schreibt Zustand und Protokoll."""
    now = now or datetime.now()
    state = load_state()
    if configuration.full_reset.enabled and state.last_full_reset is None:
        state.last_full_reset = now
        save_state(state)

    mode = select_mode(configuration, state, now)
    if mode is None:
        return None
    report = run_cleanup(configuration, mode, dry_run=False, now=now)
    if mode == FULL_MODE and report.blocked_profiles == 0:
        state.last_full_reset = now
        save_state(state)
    append_history(report)
    return report


def run_manual(configuration: Configuration, mode: str, dry_run: bool) -> RunReport:
    report = run_cleanup(configuration, mode, dry_run)
    if dry_run:
        return report
    if mode == FULL_MODE and report.blocked_profiles == 0:
        save_state(State(report.started))
    append_history(report)
    return report


def append_history(report: RunReport) -> None:
    path = data_directory() / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "started": report.started.isoformat(timespec="seconds"),
        "mode": report.mode,
        "profiles": [asdict(profile) for profile in report.profiles],
    }
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()[-(HISTORY_LIMIT - 1):]
    lines.append(json.dumps(entry, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_history(path: Path | None = None) -> list[RunReport]:
    path = path or data_directory() / "history.jsonl"
    reports = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return reports
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            reports.append(
                RunReport(
                    datetime.fromisoformat(entry["started"]),
                    entry["mode"],
                    False,
                    [ProfileReport(**profile) for profile in entry["profiles"]],
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return reports
