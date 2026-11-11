"""Tests for Completer/manifest.json: what NZBGet needs, and that it agrees with the code and the documents."""

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
PACKAGE = os.path.join(ROOT, "Completer")
sys.path.insert(0, PACKAGE)
sys.path.insert(0, HERE)

import completer  # noqa: E402
from completer import settings  # noqa: E402


def read_text(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class ManifestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(read_text("Completer", "manifest.json"))
        cls.options = dict((option["name"], option) for option in cls.manifest["options"])
        cls.commands = dict((command["name"], command) for command in cls.manifest["commands"])


class StructureTests(ManifestCase):
    def test_required_keys_are_strings(self):
        for key in ("author", "main", "homepage", "about", "version", "name", "displayName", "kind", "license",
                    "taskTime", "queueEvents"):
            self.assertIn(key, self.manifest, key)
            self.assertIsInstance(self.manifest[key], str, key)

    def test_required_keys_are_not_empty_except_none(self):
        for key in ("author", "main", "homepage", "about", "version", "name", "displayName", "kind", "license",
                    "taskTime", "queueEvents"):
            self.assertTrue(self.manifest[key].strip(), key)

    def test_arrays(self):
        for key in ("description", "requirements", "options", "commands"):
            self.assertIn(key, self.manifest, key)
            self.assertIsInstance(self.manifest[key], list, key)
            self.assertTrue(self.manifest[key], key)
        for key in ("description", "requirements"):
            for line in self.manifest[key]:
                self.assertIsInstance(line, str)

    def test_name_main_kind(self):
        self.assertEqual(self.manifest["name"], "Completer")
        self.assertEqual(self.manifest["name"], settings.NAME)
        self.assertEqual(self.manifest["main"], "main.py")
        self.assertTrue(os.path.isfile(os.path.join(PACKAGE, self.manifest["main"])))
        self.assertEqual(self.manifest["kind"], "SCAN/QUEUE/SCHEDULER")

    def test_queue_events(self):
        self.assertEqual(self.manifest["queueEvents"], "NZB_ADDED")

    def test_license_and_homepage(self):
        self.assertEqual(self.manifest["license"], "MIT")
        self.assertTrue(self.manifest["homepage"].startswith("https://"))

    def test_version_matches_the_package(self):
        self.assertEqual(self.manifest["version"], completer.__version__)
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_version_matches_the_top_of_the_changelog(self):
        match = re.search(r"^## \[(\d[^\]]*)\]", read_text("CHANGELOG.md"), re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), self.manifest["version"])

    def test_version_is_in_the_readme_if_it_names_one(self):
        # the README must not announce a different version than the manifest
        for found in re.findall(r"Completer[- ]v?(\d+\.\d+\.\d+)", read_text("README.md")):
            self.assertEqual(found, self.manifest["version"])


class TaskTimeTests(ManifestCase):
    def test_entries_are_accepted_by_nzbget(self):
        entries = self.manifest["taskTime"].split(";")
        for entry in entries:
            self.assertEqual(entry, entry.strip(), entry)
            match = re.match(r"^(\*|\*:(\d\d))$", entry)
            self.assertIsNotNone(match, entry)
            if match.group(2) is not None:
                self.assertTrue(0 <= int(match.group(2)) <= 59, entry)

    def test_runs_at_start_and_every_five_minutes(self):
        entries = self.manifest["taskTime"].split(";")
        self.assertIn("*", entries)
        self.assertEqual(sorted(e for e in entries if e != "*"), ["*:%02d" % m for m in range(0, 60, 5)])
        self.assertEqual(len(entries), len(set(entries)))


class OptionTests(ManifestCase):
    def test_each_option_is_complete(self):
        for option in self.manifest["options"]:
            for key in ("name", "displayName", "value", "description", "select"):
                self.assertIn(key, option, "%s: %s" % (option.get("name"), key))
            self.assertIsInstance(option["name"], str)
            self.assertTrue(option["name"])
            self.assertIsInstance(option["displayName"], str)
            self.assertTrue(option["displayName"])
            self.assertIsInstance(option["description"], list)
            self.assertTrue(option["description"])
            for line in option["description"]:
                self.assertIsInstance(line, str)
            self.assertIsInstance(option["select"], list)
            self.assertIsInstance(option["value"], (str, int, float))
            self.assertNotIsInstance(option["value"], bool)

    def test_names_are_unique(self):
        names = [option["name"] for option in self.manifest["options"]]
        self.assertEqual(len(names), len(set(names)))

    def test_option_names_are_exactly_the_ones_settings_reads(self):
        source = read_text("Completer", "completer", "settings.py")
        read = set(re.findall(r'own_option\(env, "(\w+)"', source))
        self.assertTrue(read)
        self.assertEqual(set(self.options), read)

    def test_defaults_equal_the_settings_defaults(self):
        defaults = settings.Settings({})
        self.assertEqual(self.options["Mode"]["value"], "DryRun")
        self.assertEqual(defaults.mode, "dryrun")
        self.assertEqual(self.options["Connections"]["value"], 2)
        self.assertEqual(defaults.connections, 2)
        self.assertEqual(self.options["FreshHours"]["value"], 2)
        self.assertEqual(defaults.fresh_seconds, 2 * 3600)
        self.assertEqual(self.options["MaxArticles"]["value"], 1200)
        self.assertEqual(defaults.max_articles, 1200)
        self.assertEqual(self.options["TimeLimit"]["value"], 45)
        self.assertEqual(defaults.time_limit, 45)
        self.assertEqual(self.options["MaxFillPercent"]["value"], 100)
        self.assertEqual(defaults.max_fill, 1.0)
        self.assertEqual(self.options["Categories"]["value"], "")
        self.assertEqual(defaults.categories, frozenset())
        self.assertEqual(self.options["IgnoreServers"]["value"], "")
        self.assertEqual(defaults.ignore_servers, frozenset())
        self.assertEqual(self.options["Verbose"]["value"], "no")
        self.assertFalse(defaults.verbose)

    def test_manifest_values_read_back_as_the_defaults(self):
        env = dict(("NZBPO_" + name, str(option["value"])) for name, option in self.options.items())
        self.assertEqual(vars(settings.Settings(env)), vars(settings.Settings({})))

    def test_selects(self):
        self.assertEqual(self.options["Mode"]["select"], ["DryRun", "Enforce", "Off"])
        self.assertEqual(self.options["Verbose"]["select"], ["no", "yes"])
        for name, option in self.options.items():
            if option["select"]:
                self.assertIn(option["value"], option["select"], name)

    def test_every_select_choice_is_understood_by_settings(self):
        for choice in self.options["Mode"]["select"]:
            self.assertEqual(settings.Settings({"NZBPO_Mode": choice}).mode, choice.lower())
        self.assertTrue(settings.Settings({"NZBPO_Verbose": "yes"}).verbose)
        self.assertFalse(settings.Settings({"NZBPO_Verbose": "no"}).verbose)

    def test_numeric_defaults_are_inside_the_allowed_range(self):
        # the ranges the manifest text promises are the ones Settings enforces
        limits = {"Connections": (1, 8), "FreshHours": (0, 72), "MaxArticles": (100, 20000),
                  "TimeLimit": (10, 300), "MaxFillPercent": (0, 100)}
        for name, (low, high) in limits.items():
            self.assertTrue(low <= self.options[name]["value"] <= high, name)
            text = " ".join(self.options[name]["description"])
            if name != "MaxFillPercent":  # its text names no range, only "100 switches this off"
                self.assertIn("%d to %d" % (low, high), text.replace("(", " ").replace(")", " "), name)


class CommandTests(ManifestCase):
    def test_exactly_the_three_commands(self):
        self.assertEqual(set(self.commands), {"TestServers", "ReleaseAll", "Report"})
        self.assertEqual(len(self.manifest["commands"]), 3)

    def test_each_command_is_complete(self):
        for command in self.manifest["commands"]:
            for key in ("name", "displayName", "action", "description"):
                self.assertIn(key, command, "%s: %s" % (command.get("name"), key))
            self.assertIsInstance(command["action"], str)
            self.assertTrue(command["action"])
            self.assertIsInstance(command["description"], list)
            self.assertTrue(command["description"])

    def test_same_names_as_the_ones_the_app_handles(self):
        source = read_text("Completer", "completer", "app.py")
        block = source[source.index("def command(self)"):]
        block = block[:block.index("handler is None")]
        handled = set(re.findall(r'"(\w+)":\s*commands\.', block))
        self.assertEqual(handled, set(self.commands))

    def test_no_command_shares_a_name_with_an_option(self):
        self.assertFalse(set(self.commands) & set(self.options))


class ReadmeTests(ManifestCase):
    def readme_section(self, heading):
        text = read_text("README.md")
        start = text.index("\n## " + heading)
        end = text.find("\n## ", start + 1)
        return text[start:end if end != -1 else len(text)]

    def test_readme_options_match_the_manifest(self):
        section = self.readme_section("Settings")
        documented = re.findall(r"^\| `(\w+)` \|", section, re.M)
        self.assertEqual(sorted(documented), sorted(self.options))

    def test_readme_defaults_match_the_manifest(self):
        section = self.readme_section("Settings")
        for name, default in re.findall(r"^\| `(\w+)` \| (`[^`]*`|empty) \|", section, re.M):
            expected = self.options[name]["value"]
            if default == "empty":
                self.assertEqual(expected, "", name)
            else:
                self.assertEqual(default.strip("`"), str(expected), name)

    def test_readme_commands_match_the_manifest(self):
        section = self.readme_section("The three buttons")
        documented = re.findall(r"^- \*\*(\w+)\*\*", section, re.M)
        self.assertEqual(sorted(documented), sorted(self.commands))

    def test_every_option_and_command_is_mentioned_in_the_readme(self):
        text = read_text("README.md")
        for name in list(self.options) + list(self.commands):
            self.assertIn(name, text, name)


if __name__ == "__main__":
    unittest.main()
