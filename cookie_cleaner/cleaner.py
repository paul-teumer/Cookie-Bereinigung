"""Löschen von Cookies und Websitedaten in den Profildateien der Browser."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

CHROMIUM_EPOCH_OFFSET_MICROSECONDS = 11_644_473_600 * 1_000_000

ORIGIN_FOLDER_PATTERN = re.compile(r"^([a-z][a-z0-9.+-]*)\+\+\+([^^]+?)(\+\d+)?(\^.*)?$")
ELIGIBLE_SCHEMES = {"http", "https"}

Decision = Callable[[str, float], bool]


@dataclass(frozen=True)
class StoreLayout:
    table: str
    host_column: str
    access_column: str
    epoch_offset_microseconds: int


FIREFOX_LAYOUT = StoreLayout("moz_cookies", "host", "lastAccessed", 0)
CHROMIUM_LAYOUT = StoreLayout("cookies", "host_key", "last_access_utc", CHROMIUM_EPOCH_OFFSET_MICROSECONDS)


@dataclass(frozen=True)
class BackupPlan:
    directory: Path
    label: str
    keep: int


@dataclass
class DatabaseOutcome:
    status: str
    message: str = ""
    total: int = 0
    deleted: int = 0
    surviving_hosts: set[str] = field(default_factory=set)


@dataclass
class SiteDataOutcome:
    folders: int = 0
    bytes_freed: int = 0


def normalize_host(host: str) -> str:
    return host.lstrip(".").lower()


def is_kept(host: str, keep_domains: Iterable[str]) -> bool:
    normalized = normalize_host(host)
    return any(normalized == domain or normalized.endswith("." + domain) for domain in keep_domains)


def domains_related(cookie_host: str, origin_host: str) -> bool:
    cookie_host = normalize_host(cookie_host)
    origin_host = normalize_host(origin_host)
    return (
        origin_host == cookie_host
        or origin_host.endswith("." + cookie_host)
        or cookie_host.endswith("." + origin_host)
    )


def _connect(database: Path, mode: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{database.as_uri()}?mode={mode}", uri=True, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _read_rows(connection: sqlite3.Connection, layout: StoreLayout) -> list[tuple[int, str, int]]:
    query = f"SELECT rowid, {layout.host_column}, {layout.access_column} FROM {layout.table}"
    return [(rowid, host, accessed) for rowid, host, accessed in connection.execute(query)]


def _is_lock_error(error: sqlite3.Error) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _evaluate(
    rows: list[tuple[int, str, int]], layout: StoreLayout, decide: Decision
) -> tuple[list[tuple[int, int]], set[str]]:
    doomed = []
    surviving = set()
    for rowid, host, accessed in rows:
        seconds = (accessed - layout.epoch_offset_microseconds) / 1_000_000
        if decide(host, seconds):
            doomed.append((rowid, accessed))
        else:
            surviving.add(host)
    return doomed, surviving


def backup_database(connection: sqlite3.Connection, plan: BackupPlan) -> None:
    plan.directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = sqlite3.connect(plan.directory / f"{plan.label}_{stamp}.sqlite")
    try:
        connection.backup(target)
    finally:
        target.close()
    for obsolete in sorted(plan.directory.glob(f"{plan.label}_*.sqlite"))[: -plan.keep]:
        obsolete.unlink(missing_ok=True)


def _preview(database: Path, layout: StoreLayout, decide: Decision) -> DatabaseOutcome:
    try:
        connection = _connect(database, "ro")
        note = ""
        try:
            rows = _read_rows(connection, layout)
        finally:
            connection.close()
    except sqlite3.OperationalError as error:
        if not _is_lock_error(error):
            raise
        with tempfile.TemporaryDirectory() as folder:
            snapshot = Path(folder) / database.name
            shutil.copy2(database, snapshot)
            connection = _connect(snapshot, "ro")
            try:
                rows = _read_rows(connection, layout)
            finally:
                connection.close()
        note = "Vorschau aus Kopie, da der Browser die Datenbank sperrt"
    doomed, surviving = _evaluate(rows, layout, decide)
    return DatabaseOutcome("ok", note, len(rows), len(doomed), surviving)


def _apply(
    database: Path, layout: StoreLayout, decide: Decision, backup: BackupPlan | None
) -> DatabaseOutcome:
    connection = _connect(database, "rw")
    try:
        rows = _read_rows(connection, layout)
        doomed, surviving = _evaluate(rows, layout, decide)
        if doomed and backup is not None:
            backup_database(connection, backup)
        # Die Bedingung auf den Zugriffszeitpunkt verhindert, dass ein Cookie gelöscht wird,
        # das der laufende Browser seit dem Einlesen noch verwendet hat.
        connection.executemany(
            f"DELETE FROM {layout.table} WHERE rowid = ? AND {layout.access_column} = ?", doomed
        )
        connection.commit()
        return DatabaseOutcome("ok", "", len(rows), len(doomed), surviving)
    finally:
        connection.close()


def process_database(
    database: Path,
    layout: StoreLayout,
    decide: Decision,
    dry_run: bool,
    backup: BackupPlan | None,
) -> DatabaseOutcome:
    if not database.exists():
        return DatabaseOutcome("missing", "Cookie-Datenbank nicht gefunden")
    try:
        if dry_run:
            return _preview(database, layout, decide)
        return _apply(database, layout, decide, backup)
    except sqlite3.Error as error:
        if _is_lock_error(error):
            return DatabaseOutcome("locked", "Datenbank gesperrt – Browser beenden und erneut versuchen")
        return DatabaseOutcome("error", str(error))
    except OSError as error:
        return DatabaseOutcome("error", str(error))


def count_cookies(database: Path, layout: StoreLayout) -> int | None:
    try:
        return _preview(database, layout, lambda host, accessed: False).total
    except (sqlite3.Error, OSError):
        return None


def _directory_size(directory: Path) -> int:
    return sum(file.stat().st_size for file in directory.rglob("*") if file.is_file())


def clean_site_data(
    profile_directory: Path,
    surviving_hosts: set[str],
    keep_domains: Iterable[str],
    dry_run: bool,
) -> SiteDataOutcome:
    """Entfernt Local-Storage-/IndexedDB-Ordner von Websites ohne verbleibende Cookies."""
    outcome = SiteDataOutcome()
    keep = list(keep_domains)
    for root in (profile_directory / "storage" / "default", profile_directory / "storage" / "permanent"):
        if not root.is_dir():
            continue
        for origin_directory in root.iterdir():
            match = ORIGIN_FOLDER_PATTERN.match(origin_directory.name)
            if not origin_directory.is_dir() or not match or match.group(1) not in ELIGIBLE_SCHEMES:
                continue
            origin_host = match.group(2)
            if is_kept(origin_host, keep) or any(
                domains_related(host, origin_host) for host in surviving_hosts
            ):
                continue

            size = _directory_size(origin_directory)
            if not dry_run:
                # Umbenennen vor dem Löschen: Ist eine Datei im Ordner geöffnet, scheitert
                # die Umbenennung und der Ordner bleibt vollständig erhalten.
                quarantine = origin_directory.with_name(f".delete-{uuid.uuid4().hex}")
                try:
                    origin_directory.rename(quarantine)
                except OSError:
                    continue
                shutil.rmtree(quarantine, ignore_errors=True)
            outcome.folders += 1
            outcome.bytes_freed += size
    return outcome
