import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from cookie_cleaner import cleaner
from cookie_cleaner.engine import AGE_MODE, FULL_MODE
from cookie_cleaner.config import Configuration
from cookie_cleaner.engine import build_decision
from datetime import datetime

DAY = 86400


def create_firefox_database(path: Path, cookies: list[tuple[str, float]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, lastAccessed INTEGER)")
    connection.executemany(
        "INSERT INTO moz_cookies (host, name, lastAccessed) VALUES (?, 'c', ?)",
        [(host, int(accessed * 1_000_000)) for host, accessed in cookies],
    )
    connection.commit()
    connection.close()


def create_chromium_database(path: Path, cookies: list[tuple[str, float]]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, last_access_utc INTEGER)")
    connection.executemany(
        "INSERT INTO cookies VALUES (?, 'c', ?)",
        [(host, int(accessed * 1_000_000) + cleaner.CHROMIUM_EPOCH_OFFSET_MICROSECONDS) for host, accessed in cookies],
    )
    connection.commit()
    connection.close()


def remaining_hosts(path: Path, table: str, column: str) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in connection.execute(f"SELECT {column} FROM {table}"))
    finally:
        connection.close()


class DomainMatchingTest(unittest.TestCase):
    def test_kept_matches_subdomains_and_leading_dot(self):
        self.assertTrue(cleaner.is_kept(".github.com", ["github.com"]))
        self.assertTrue(cleaner.is_kept("gist.github.com", ["github.com"]))
        self.assertFalse(cleaner.is_kept("notgithub.com", ["github.com"]))

    def test_related_domains_in_both_directions(self):
        self.assertTrue(cleaner.domains_related(".example.com", "www.example.com"))
        self.assertTrue(cleaner.domains_related("www.example.com", "example.com"))
        self.assertFalse(cleaner.domains_related("example.com", "example.org"))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        now = time.time()
        self.cookies = [("old.example", now - 40 * DAY), ("new.example", now - DAY), (".keep.example", now - 90 * DAY)]
        configuration = Configuration(keep_domains=["keep.example"])
        self.decide = build_decision(configuration, AGE_MODE, datetime.now())
        self.decide_all = build_decision(configuration, FULL_MODE, datetime.now())

    def test_firefox_age_cleanup_keeps_recent_and_whitelisted(self):
        database = self.directory / "cookies.sqlite"
        create_firefox_database(database, self.cookies)
        outcome = cleaner.process_database(database, cleaner.FIREFOX_LAYOUT, self.decide, False, None)
        self.assertEqual((outcome.status, outcome.total, outcome.deleted), ("ok", 3, 1))
        self.assertEqual(remaining_hosts(database, "moz_cookies", "host"), [".keep.example", "new.example"])

    def test_chromium_epoch_conversion(self):
        database = self.directory / "Cookies"
        create_chromium_database(database, self.cookies)
        outcome = cleaner.process_database(database, cleaner.CHROMIUM_LAYOUT, self.decide, False, None)
        self.assertEqual(outcome.deleted, 1)
        self.assertEqual(remaining_hosts(database, "cookies", "host_key"), [".keep.example", "new.example"])

    def test_full_reset_spares_only_whitelist(self):
        database = self.directory / "cookies.sqlite"
        create_firefox_database(database, self.cookies)
        outcome = cleaner.process_database(database, cleaner.FIREFOX_LAYOUT, self.decide_all, False, None)
        self.assertEqual(outcome.deleted, 2)
        self.assertEqual(remaining_hosts(database, "moz_cookies", "host"), [".keep.example"])

    def test_dry_run_changes_nothing(self):
        database = self.directory / "cookies.sqlite"
        create_firefox_database(database, self.cookies)
        outcome = cleaner.process_database(database, cleaner.FIREFOX_LAYOUT, self.decide_all, True, None)
        self.assertEqual(outcome.deleted, 2)
        self.assertEqual(len(remaining_hosts(database, "moz_cookies", "host")), 3)

    def test_locked_database_is_reported_and_untouched(self):
        database = self.directory / "Cookies"
        create_chromium_database(database, self.cookies)
        blocker = sqlite3.connect(database, isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            outcome = cleaner.process_database(database, cleaner.CHROMIUM_LAYOUT, self.decide_all, False, None)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assertEqual(outcome.status, "locked")
        self.assertEqual(len(remaining_hosts(database, "cookies", "host_key")), 3)

    def test_backup_created_and_pruned(self):
        database = self.directory / "cookies.sqlite"
        backups = self.directory / "backups"
        plan = cleaner.BackupPlan(backups, "test", keep=2)
        for attempt in range(3):
            create_firefox_database(database, self.cookies) if attempt == 0 else None
            connection = sqlite3.connect(database)
            connection.execute("INSERT INTO moz_cookies (host, name, lastAccessed) VALUES ('x.example', 'c', 0)")
            connection.commit()
            connection.close()
            cleaner.process_database(database, cleaner.FIREFOX_LAYOUT, self.decide, False, plan)
            time.sleep(1.1)
        self.assertEqual(len(list(backups.glob("test_*.sqlite"))), 2)
        restored = sorted(backups.glob("test_*.sqlite"))[-1]
        self.assertIn("x.example", remaining_hosts(restored, "moz_cookies", "host"))


class SiteDataTest(unittest.TestCase):
    def setUp(self):
        self.profile = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.profile, ignore_errors=True))
        storage = self.profile / "storage" / "default"
        for name in ("https+++stale.example", "https+++alive.example", "https+++keep.example", "moz-extension+++abc"):
            folder = storage / name / "ls"
            folder.mkdir(parents=True)
            (folder / "data.sqlite").write_bytes(b"x" * 100)

    def test_removes_only_stale_http_origins(self):
        outcome = cleaner.clean_site_data(self.profile, {".alive.example"}, ["keep.example"], dry_run=False)
        names = sorted(path.name for path in (self.profile / "storage" / "default").iterdir())
        self.assertEqual(names, ["https+++alive.example", "https+++keep.example", "moz-extension+++abc"])
        self.assertEqual((outcome.folders, outcome.bytes_freed), (1, 100))

    def test_dry_run_keeps_everything(self):
        outcome = cleaner.clean_site_data(self.profile, set(), [], dry_run=True)
        self.assertEqual(outcome.folders, 3)
        self.assertEqual(len(list((self.profile / "storage" / "default").iterdir())), 4)


if __name__ == "__main__":
    unittest.main()
