"""Kommandozeile."""

from __future__ import annotations

import argparse
import sys

from . import __version__, scheduler
from .browsers import CATALOG, detect_browsers
from .config import load_configuration
from .engine import AGE_MODE, FULL_MODE, RunReport, run_manual, run_scheduled

MODE_LABELS = {AGE_MODE: "Altersbereinigung", FULL_MODE: "Vollständiger Neustart"}


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def print_report(report: RunReport) -> None:
    prefix = "Vorschau – " if report.dry_run else ""
    print(f"{prefix}{MODE_LABELS[report.mode]}")
    for profile in report.profiles:
        line = f"  {profile.browser:<22} {profile.profile:<20}"
        if profile.status == "ok":
            line += f" {profile.cookies_deleted:>6} von {profile.cookies_total} Cookies"
            if profile.site_data_folders:
                line += f", {profile.site_data_folders} Websitedaten-Ordner ({format_size(profile.bytes_freed)})"
            if profile.message:
                line += f"  [{profile.message}]"
        else:
            line += f"  {profile.message}"
        print(line)
    print(
        f"Gesamt: {report.cookies_deleted} Cookies, {report.site_data_folders} Websitedaten-Ordner, "
        f"{format_size(report.bytes_freed)} Speicher"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cookie_cleaner", description="Automatische Cookie-Bereinigung für Windows-Browser.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("gui", help="Oberfläche öffnen (Standard)")
    commands.add_parser("browsers", help="Erkannte Browser und Profile auflisten")

    run = commands.add_parser("run", help="Fällige Bereinigung gemäß Einstellungen ausführen")
    run.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts löschen")
    run.add_argument("--full", action="store_true", help="Vollständigen Neustart erzwingen")
    run.add_argument("--age", action="store_true", help="Altersbereinigung erzwingen")

    schedule = commands.add_parser("schedule", help="Zeitplan in der Windows-Aufgabenplanung verwalten")
    schedule.add_argument("action", choices=("install", "remove", "status"))
    return parser


def _command_browsers() -> int:
    detected = detect_browsers()
    configured = set(load_configuration().browsers)
    for definition in CATALOG:
        profiles = detected.get(definition)
        if profiles:
            marker = "aktiv" if definition.key in configured else "inaktiv"
            print(f"{definition.name:<22} {marker:<8} {', '.join(profile.name for profile in profiles)}")
    return 0


def _command_run(arguments: argparse.Namespace) -> int:
    configuration = load_configuration()
    if not configuration.browsers:
        print("Keine Browser ausgewählt. Einstellungen in der Oberfläche vornehmen.", file=sys.stderr)
        return 2
    forced = FULL_MODE if arguments.full else AGE_MODE if arguments.age else None
    if forced or arguments.dry_run:
        report = run_manual(configuration, forced or AGE_MODE, arguments.dry_run)
    else:
        report = run_scheduled(configuration)
        if report is None:
            print("Nichts zu tun: keine Regel aktiv.")
            return 0
    print_report(report)
    return 1 if report.blocked_profiles else 0


def _command_schedule(action: str) -> int:
    try:
        if action == "install":
            scheduler.install(load_configuration().schedule)
            print("Zeitplan eingerichtet.")
        elif action == "remove":
            scheduler.remove()
            print("Zeitplan entfernt.")
        else:
            print("eingerichtet" if scheduler.is_installed() else "nicht eingerichtet")
    except scheduler.SchedulerError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command in (None, "gui"):
        from .gui import launch

        launch()
        return 0
    if arguments.command == "browsers":
        return _command_browsers()
    if arguments.command == "run":
        return _command_run(arguments)
    return _command_schedule(arguments.action)
