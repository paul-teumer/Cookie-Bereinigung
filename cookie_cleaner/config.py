"""Persistente Einstellungen und Laufzeitzustand."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

UNITS = ("days", "weeks", "months")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


def data_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Cookie-Bereinigung"


def clamp(value: object, low: int, high: int, default: int) -> int:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_domain(text: str) -> str | None:
    """Gibt die bereinigte Domain zurück oder None, wenn die Eingabe keine gültige Domain ist."""
    domain = text.strip().lower().removeprefix("*.").lstrip(".")
    domain = re.sub(r"^[a-z][a-z0-9+.-]*://", "", domain).split("/")[0]
    return domain if DOMAIN_PATTERN.match(domain) else None


@dataclass
class AgeRule:
    enabled: bool = True
    max_age_days: int = 21

    def __post_init__(self) -> None:
        self.max_age_days = clamp(self.max_age_days, 1, 3650, 21)


@dataclass
class FullResetRule:
    enabled: bool = False
    every: int = 1
    unit: str = "months"
    weekday: int = 6
    day_of_month: int = 1

    def __post_init__(self) -> None:
        if self.unit not in UNITS:
            self.unit = "months"
        upper = {"days": 365, "weeks": 52, "months": 12}[self.unit]
        self.every = clamp(self.every, 1, upper, 1)
        self.weekday = clamp(self.weekday, 0, 6, 6)
        self.day_of_month = clamp(self.day_of_month, 1, 28, 1)


@dataclass
class ScheduleRule:
    enabled: bool = False
    time: str = "12:00"
    run_at_logon: bool = True

    def __post_init__(self) -> None:
        if not TIME_PATTERN.match(str(self.time)):
            self.time = "12:00"


@dataclass
class Configuration:
    browsers: list[str] = field(default_factory=list)
    age: AgeRule = field(default_factory=AgeRule)
    full_reset: FullResetRule = field(default_factory=FullResetRule)
    keep_domains: list[str] = field(default_factory=list)
    include_site_data: bool = True
    create_backups: bool = True
    backups_to_keep: int = 5
    schedule: ScheduleRule = field(default_factory=ScheduleRule)

    def __post_init__(self) -> None:
        self.backups_to_keep = clamp(self.backups_to_keep, 1, 50, 5)
        normalized = (normalize_domain(str(entry)) for entry in self.keep_domains)
        self.keep_domains = sorted({domain for domain in normalized if domain})

    @classmethod
    def from_dict(cls, data: dict) -> "Configuration":
        def section(name: str, rule_class: type):
            known = rule_class.__dataclass_fields__.keys()
            values = data.get(name, {})
            return rule_class(**{key: value for key, value in values.items() if key in known}) if isinstance(values, dict) else rule_class()

        return cls(
            browsers=[str(key) for key in data.get("browsers", [])],
            age=section("age", AgeRule),
            full_reset=section("full_reset", FullResetRule),
            keep_domains=list(data.get("keep_domains", [])),
            include_site_data=bool(data.get("include_site_data", True)),
            create_backups=bool(data.get("create_backups", True)),
            backups_to_keep=data.get("backups_to_keep", 5),
            schedule=section("schedule", ScheduleRule),
        )


@dataclass
class State:
    last_full_reset: datetime | None = None


def _write_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def load_configuration(directory: Path | None = None) -> Configuration:
    path = (directory or data_directory()) / "config.json"
    try:
        return Configuration.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return Configuration()


def save_configuration(configuration: Configuration, directory: Path | None = None) -> None:
    path = (directory or data_directory()) / "config.json"
    _write_atomically(path, json.dumps(asdict(configuration), indent=2, ensure_ascii=False))


def load_state(directory: Path | None = None) -> State:
    path = (directory or data_directory()) / "state.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_full_reset")
        return State(datetime.fromisoformat(value) if value else None)
    except (OSError, ValueError, TypeError, AttributeError):
        return State()


def save_state(state: State, directory: Path | None = None) -> None:
    path = (directory or data_directory()) / "state.json"
    stamp = state.last_full_reset.isoformat() if state.last_full_reset else None
    _write_atomically(path, json.dumps({"last_full_reset": stamp}))
