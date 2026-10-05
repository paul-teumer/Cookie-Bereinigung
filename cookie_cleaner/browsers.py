"""Erkennung installierter Browser und ihrer Profile."""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

FIREFOX_FAMILY = "firefox"
CHROMIUM_FAMILY = "chromium"

CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class BrowserDefinition:
    key: str
    name: str
    family: str
    base_variable: str
    relative_path: str
    process_name: str


@dataclass(frozen=True)
class Profile:
    browser: BrowserDefinition
    name: str
    directory: Path
    cookie_database: Path


CATALOG: tuple[BrowserDefinition, ...] = (
    BrowserDefinition("firefox", "Mozilla Firefox", FIREFOX_FAMILY, "APPDATA", "Mozilla/Firefox", "firefox.exe"),
    BrowserDefinition("librewolf", "LibreWolf", FIREFOX_FAMILY, "APPDATA", "librewolf", "librewolf.exe"),
    BrowserDefinition("waterfox", "Waterfox", FIREFOX_FAMILY, "APPDATA", "Waterfox", "waterfox.exe"),
    BrowserDefinition("floorp", "Floorp", FIREFOX_FAMILY, "APPDATA", "Floorp", "floorp.exe"),
    BrowserDefinition("zen", "Zen Browser", FIREFOX_FAMILY, "APPDATA", "zen", "zen.exe"),
    BrowserDefinition("palemoon", "Pale Moon", FIREFOX_FAMILY, "APPDATA", "Moonchild Productions/Pale Moon", "palemoon.exe"),
    BrowserDefinition("chrome", "Google Chrome", CHROMIUM_FAMILY, "LOCALAPPDATA", "Google/Chrome/User Data", "chrome.exe"),
    BrowserDefinition("chrome-beta", "Google Chrome Beta", CHROMIUM_FAMILY, "LOCALAPPDATA", "Google/Chrome Beta/User Data", "chrome.exe"),
    BrowserDefinition("chrome-canary", "Google Chrome Canary", CHROMIUM_FAMILY, "LOCALAPPDATA", "Google/Chrome SxS/User Data", "chrome.exe"),
    BrowserDefinition("edge", "Microsoft Edge", CHROMIUM_FAMILY, "LOCALAPPDATA", "Microsoft/Edge/User Data", "msedge.exe"),
    BrowserDefinition("brave", "Brave", CHROMIUM_FAMILY, "LOCALAPPDATA", "BraveSoftware/Brave-Browser/User Data", "brave.exe"),
    BrowserDefinition("vivaldi", "Vivaldi", CHROMIUM_FAMILY, "LOCALAPPDATA", "Vivaldi/User Data", "vivaldi.exe"),
    BrowserDefinition("opera", "Opera", CHROMIUM_FAMILY, "APPDATA", "Opera Software/Opera Stable", "opera.exe"),
    BrowserDefinition("opera-gx", "Opera GX", CHROMIUM_FAMILY, "APPDATA", "Opera Software/Opera GX Stable", "opera.exe"),
    BrowserDefinition("chromium", "Chromium", CHROMIUM_FAMILY, "LOCALAPPDATA", "Chromium/User Data", "chromium.exe"),
    BrowserDefinition("yandex", "Yandex Browser", CHROMIUM_FAMILY, "LOCALAPPDATA", "Yandex/YandexBrowser/User Data", "browser.exe"),
    BrowserDefinition("thorium", "Thorium", CHROMIUM_FAMILY, "LOCALAPPDATA", "Thorium/User Data", "thorium.exe"),
    BrowserDefinition("epic", "Epic Privacy Browser", CHROMIUM_FAMILY, "LOCALAPPDATA", "Epic Privacy Browser/User Data", "epic.exe"),
)


def definition_by_key(key: str) -> BrowserDefinition | None:
    return next((definition for definition in CATALOG if definition.key == key), None)


def _root_directory(definition: BrowserDefinition, environment: Mapping[str, str]) -> Path | None:
    base = environment.get(definition.base_variable)
    if not base:
        return None
    return Path(base) / definition.relative_path


def _firefox_profiles(definition: BrowserDefinition, root: Path) -> list[Profile]:
    candidates: dict[Path, str] = {}

    parser = configparser.ConfigParser(interpolation=None)
    initialization_file = root / "profiles.ini"
    if initialization_file.exists():
        try:
            parser.read(initialization_file, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            pass
        for section in parser.sections():
            if not section.lower().startswith("profile"):
                continue
            path_text = parser.get(section, "Path", fallback=None)
            if not path_text:
                continue
            is_relative = parser.get(section, "IsRelative", fallback="1") == "1"
            directory = root / path_text if is_relative else Path(path_text)
            candidates[directory] = parser.get(section, "Name", fallback=directory.name)

    profiles_folder = root / "Profiles"
    if profiles_folder.is_dir():
        for directory in profiles_folder.iterdir():
            candidates.setdefault(directory, directory.name)

    profiles = []
    seen: set[Path] = set()
    for directory, name in candidates.items():
        database = directory / "cookies.sqlite"
        resolved = directory.resolve()
        if database.exists() and resolved not in seen:
            seen.add(resolved)
            profiles.append(Profile(definition, name, directory, database))
    return profiles


def _chromium_profile_names(root: Path) -> dict[str, str]:
    try:
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        cache = state["profile"]["info_cache"]
        return {folder: entry.get("name", folder) for folder, entry in cache.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _chromium_cookie_database(directory: Path) -> Path | None:
    for candidate in (directory / "Network" / "Cookies", directory / "Cookies"):
        if candidate.exists():
            return candidate
    return None


def _chromium_profiles(definition: BrowserDefinition, root: Path) -> list[Profile]:
    names = _chromium_profile_names(root)
    # Der Unterordner "Network" gehört zum Profil selbst (Opera nutzt das Wurzelverzeichnis als Profil).
    children = (child for child in root.iterdir() if child.is_dir() and child.name != "Network")
    directories = [root] + sorted(children)
    profiles = []
    for directory in directories:
        database = _chromium_cookie_database(directory)
        if database is None:
            continue
        name = names.get(directory.name, directory.name if directory != root else definition.name)
        profiles.append(Profile(definition, name, directory, database))
    return profiles


def find_profiles(
    definition: BrowserDefinition, environment: Mapping[str, str] | None = None
) -> list[Profile]:
    root = _root_directory(definition, environment if environment is not None else os.environ)
    if root is None or not root.is_dir():
        return []
    if definition.family == FIREFOX_FAMILY:
        return _firefox_profiles(definition, root)
    return _chromium_profiles(definition, root)


def detect_browsers(
    environment: Mapping[str, str] | None = None,
) -> dict[BrowserDefinition, list[Profile]]:
    """Installierte Browser mit mindestens einem Profil samt Cookie-Datenbank."""
    detected = {}
    for definition in CATALOG:
        profiles = find_profiles(definition, environment)
        if profiles:
            detected[definition] = profiles
    return detected


def running_process_names() -> set[str]:
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set()
    for line in completed.stdout.splitlines():
        parts = line.split('","')
        if parts:
            names.add(parts[0].strip('"').lower())
    return names
