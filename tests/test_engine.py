import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from cookie_cleaner import browsers, scheduler
from cookie_cleaner.config import Configuration, FullResetRule, ScheduleRule, State, normalize_domain
from cookie_cleaner.engine import AGE_MODE, FULL_MODE, next_full_reset_date, select_mode


class NextResetTest(unittest.TestCase):
    def test_every_n_days(self):
        rule = FullResetRule(enabled=True, every=10, unit="days")
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 5)), date(2026, 10, 15))

    def test_weekly_on_sunday(self):
        rule = FullResetRule(enabled=True, every=1, unit="weeks", weekday=6)
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 5)), date(2026, 10, 11))
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 11)), date(2026, 10, 18))

    def test_every_two_weeks(self):
        rule = FullResetRule(enabled=True, every=2, unit="weeks", weekday=0)
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 5)), date(2026, 10, 19))

    def test_monthly_on_first(self):
        rule = FullResetRule(enabled=True, every=1, unit="months", day_of_month=1)
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 5)), date(2026, 11, 1))
        self.assertEqual(next_full_reset_date(rule, date(2026, 12, 31)), date(2027, 1, 1))

    def test_monthly_later_day_in_same_month(self):
        rule = FullResetRule(enabled=True, every=1, unit="months", day_of_month=20)
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 5)), date(2026, 10, 20))

    def test_quarterly(self):
        rule = FullResetRule(enabled=True, every=3, unit="months", day_of_month=1)
        self.assertEqual(next_full_reset_date(rule, date(2026, 10, 5)), date(2027, 1, 1))


class SelectModeTest(unittest.TestCase):
    def setUp(self):
        self.configuration = Configuration(
            full_reset=FullResetRule(enabled=True, every=1, unit="weeks", weekday=6)
        )

    def test_full_reset_takes_precedence_when_due(self):
        state = State(datetime(2026, 10, 5, 12, 0))
        self.assertEqual(select_mode(self.configuration, state, datetime(2026, 10, 11, 8, 0)), FULL_MODE)

    def test_age_cleanup_when_reset_not_due(self):
        state = State(datetime(2026, 10, 5, 12, 0))
        self.assertEqual(select_mode(self.configuration, state, datetime(2026, 10, 10, 8, 0)), AGE_MODE)

    def test_nothing_when_all_rules_disabled(self):
        self.configuration.age.enabled = False
        self.assertIsNone(select_mode(self.configuration, State(None), datetime(2026, 10, 10)))


class ConfigurationTest(unittest.TestCase):
    def test_values_are_clamped_and_domains_normalized(self):
        configuration = Configuration.from_dict(
            {
                "age": {"max_age_days": 99999},
                "full_reset": {"unit": "years", "every": 0},
                "keep_domains": ["https://www.GitHub.com/x", "kein domain", "*.bank.de"],
                "schedule": {"time": "25:99"},
            }
        )
        self.assertEqual(configuration.age.max_age_days, 3650)
        self.assertEqual((configuration.full_reset.unit, configuration.full_reset.every), ("months", 1))
        self.assertEqual(configuration.keep_domains, ["bank.de", "www.github.com"])
        self.assertEqual(configuration.schedule.time, "12:00")

    def test_normalize_domain_rejects_garbage(self):
        self.assertIsNone(normalize_domain("localhost"))
        self.assertIsNone(normalize_domain("a b.de"))
        self.assertEqual(normalize_domain(".Example.COM"), "example.com")


class BrowserDetectionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.environment = {"APPDATA": str(self.root / "roaming"), "LOCALAPPDATA": str(self.root / "local")}

    def test_firefox_profiles_from_profiles_ini(self):
        base = self.root / "roaming" / "Mozilla" / "Firefox"
        (base / "Profiles" / "abc.default").mkdir(parents=True)
        (base / "Profiles" / "abc.default" / "cookies.sqlite").write_bytes(b"")
        (base / "profiles.ini").write_text("[Profile0]\nName=Standard\nIsRelative=1\nPath=Profiles/abc.default\n")
        profiles = browsers.find_profiles(browsers.definition_by_key("firefox"), self.environment)
        self.assertEqual([(profile.name, profile.directory.name) for profile in profiles], [("Standard", "abc.default")])

    def test_chromium_profiles_with_names_from_local_state(self):
        base = self.root / "local" / "Google" / "Chrome" / "User Data"
        (base / "Default" / "Network").mkdir(parents=True)
        (base / "Default" / "Network" / "Cookies").write_bytes(b"")
        (base / "Profile 1").mkdir()
        (base / "Profile 1" / "Cookies").write_bytes(b"")
        (base / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {"name": "Privat"}}}}))
        profiles = browsers.find_profiles(browsers.definition_by_key("chrome"), self.environment)
        self.assertEqual(sorted(profile.name for profile in profiles), ["Privat", "Profile 1"])

    def test_opera_uses_root_as_profile(self):
        base = self.root / "roaming" / "Opera Software" / "Opera Stable" / "Network"
        base.mkdir(parents=True)
        (base / "Cookies").write_bytes(b"")
        profiles = browsers.find_profiles(browsers.definition_by_key("opera"), self.environment)
        self.assertEqual(len(profiles), 1)

    def test_missing_browser_yields_nothing(self):
        self.assertEqual(browsers.detect_browsers(self.environment), {})


class SchedulerXmlTest(unittest.TestCase):
    def test_xml_is_well_formed_and_contains_triggers(self):
        from xml.etree import ElementTree

        xml = scheduler.build_task_xml(
            ScheduleRule(enabled=True, time="08:30", run_at_logon=True),
            "PC\\Nutzer & Co",
            Path("C:/Python/pythonw.exe"),
            Path("C:/Projekte/Cookie-Bereinigung"),
            today=date(2026, 10, 5),
        )
        root = ElementTree.fromstring(xml.split("?>", 1)[1])
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", namespace).text, "2026-10-05T08:30:00")
        self.assertIsNotNone(root.find("t:Triggers/t:LogonTrigger", namespace))

    def test_logon_trigger_optional(self):
        xml = scheduler.build_task_xml(
            ScheduleRule(enabled=True, run_at_logon=False), "PC\\Nutzer", Path("p.exe"), Path("."), date(2026, 1, 1)
        )
        self.assertNotIn("LogonTrigger", xml)


if __name__ == "__main__":
    unittest.main()
