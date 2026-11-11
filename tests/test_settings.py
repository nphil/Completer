"""Tests for completer.settings: reading NZBGet's environment, clamping, servers and the control address."""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import log, settings  # noqa: E402
from completer.settings import Control, Settings, load_servers  # noqa: E402


class CleanState(unittest.TestCase):
    def setUp(self):
        log.forget_secrets()
        self.out = io.StringIO()
        log.set_stream(self.out)
        self.addCleanup(log.set_stream, None)
        self.addCleanup(log.forget_secrets)


class NamesTests(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(settings.normalise("Completer:Token"), "COMPLETER_TOKEN")
        self.assertEqual(settings.normalise("Completer_Token"), "COMPLETER_TOKEN")
        self.assertEqual(settings.normalise("a b-c.d"), "A_B_C_D")
        self.assertEqual(settings.normalise("Server1.Host"), "SERVER1_HOST")
        self.assertEqual(settings.normalise(""), "")

    def test_parameter_both_spellings(self):
        self.assertEqual(settings.parameter({"NZBPR_Completer:": "x"}, "Completer:"), "x")
        self.assertEqual(settings.parameter({"NZBPR_COMPLETER_": "y"}, "Completer:"), "y")
        self.assertEqual(settings.parameter({"NZBPR_Completer_State": "waiting"}, "Completer_State"), "waiting")
        self.assertEqual(settings.parameter({"NZBPR_COMPLETER_STATE": "waiting"}, "Completer_State"), "waiting")

    def test_exact_spelling_wins(self):
        env = {"NZBPR_Completer_State": "exact", "NZBPR_COMPLETER_STATE": "normalised"}
        self.assertEqual(settings.parameter(env, "Completer_State"), "exact")

    def test_defaults(self):
        self.assertEqual(settings.parameter({}, "Completer_State"), "")
        self.assertEqual(settings.parameter({}, "Completer_State", "none"), "none")
        self.assertEqual(settings.own_option({}, "Mode", "DryRun"), "DryRun")
        self.assertEqual(settings.nzbget_option({}, "TempDir", "/x"), "/x")

    def test_empty_value_is_not_the_default(self):
        self.assertEqual(settings.own_option({"NZBPO_Mode": ""}, "Mode", "DryRun"), "")

    def test_own_option_and_nzbget_option_prefixes(self):
        env = {"NZBPO_Mode": "Enforce", "NZBOP_Mode": "other", "NZBPR_Mode": "third"}
        self.assertEqual(settings.own_option(env, "Mode"), "Enforce")
        self.assertEqual(settings.nzbget_option(env, "Mode"), "other")
        self.assertEqual(settings.parameter(env, "Mode"), "third")

    def test_option_normalised_spelling(self):
        self.assertEqual(settings.own_option({"NZBPO_MODE": "Off"}, "Mode"), "Off")
        self.assertEqual(settings.nzbget_option({"NZBOP_TEMPDIR": "/t"}, "TempDir"), "/t")

    def test_is_yes(self):
        for word in ("yes", "YES", " Yes ", "true", "True", "on", "ON", "1"):
            self.assertTrue(settings.is_yes(word), word)
        for word in ("no", "false", "off", "0", "", "maybe", "y", "2"):
            self.assertFalse(settings.is_yes(word), word)

    def test_is_yes_non_string(self):
        self.assertTrue(settings.is_yes(1))
        self.assertFalse(settings.is_yes(None))


class ParametersTests(unittest.TestCase):
    def test_exactly_the_nine_completer_parameters(self):
        self.assertEqual(
            set(settings.PARAMETERS),
            {"Completer_Token", "Completer_State", "Completer_Since", "Completer_Checks", "Completer_Next",
             "Completer_File", "Completer_Sum", "Completer_Info", "Completer_Verdict"})
        self.assertEqual(len(settings.PARAMETERS), 9)
        self.assertEqual(len(set(settings.PARAMETERS)), 9)

    def test_every_name_is_in_completers_own_namespace(self):
        for name in settings.PARAMETERS:
            self.assertTrue(name.startswith("Completer_"), name)

    def test_constants_match_the_tuple(self):
        self.assertEqual(
            set(settings.PARAMETERS),
            {settings.TOKEN, settings.STATE, settings.SINCE, settings.CHECKS, settings.NEXT, settings.FILE,
             settings.SUM, settings.INFO, settings.VERDICT})

    def test_held_states(self):
        self.assertEqual(set(settings.HELD), {settings.PENDING, settings.WAITING})


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings({})
        self.assertEqual(s.mode, "dryrun")
        self.assertEqual(s.categories, frozenset())
        self.assertEqual(s.connections, 2)
        self.assertEqual(s.fresh_seconds, 7200)
        self.assertEqual(s.max_articles, 1200)
        self.assertEqual(s.time_limit, 90)
        self.assertEqual(s.max_fill, 1.0)
        self.assertEqual(s.ignore_servers, frozenset())
        self.assertFalse(s.verbose)
        self.assertFalse(s.par_check_force)
        self.assertFalse(s.scheduler_active)
        self.assertEqual(s.temp_dir, "")
        self.assertEqual(s.cert_store, "")

    def test_mode(self):
        for text, expected in (("DryRun", "dryrun"), ("dryrun", "dryrun"), ("Enforce", "enforce"),
                               ("ENFORCE", "enforce"), (" enforce ", "enforce"), ("Off", "off"),
                               ("OFF", "off"), ("off", "off"), ("", "dryrun"), ("Always", "dryrun"),
                               ("enforced", "dryrun"), ("1", "dryrun")):
            self.assertEqual(Settings({"NZBPO_Mode": text}).mode, expected, text)

    def test_options_read_in_either_spelling(self):
        self.assertEqual(Settings({"NZBPO_MODE": "Enforce"}).mode, "enforce")
        self.assertEqual(Settings({"NZBPO_CONNECTIONS": "5"}).connections, 5)

    def test_connections_clamped(self):
        for text, expected in (("1", 1), ("8", 8), ("0", 1), ("-3", 1), ("9", 8), ("100", 8), ("4", 4),
                               ("3.9", 3), (" 6 ", 6), ("abc", 2), ("", 2)):
            self.assertEqual(Settings({"NZBPO_Connections": text}).connections, expected, text)

    def test_fresh_hours_clamped(self):
        for text, expected in (("0", 0), ("1", 3600), ("2", 7200), ("72", 259200), ("73", 259200),
                               ("1000", 259200), ("-1", 0), ("0.5", 1800), ("1.5", 5400), ("x", 7200),
                               ("", 7200)):
            self.assertEqual(Settings({"NZBPO_FreshHours": text}).fresh_seconds, expected, text)

    def test_max_articles_clamped(self):
        for text, expected in (("100", 100), ("99", 100), ("0", 100), ("-5", 100), ("20000", 20000),
                               ("20001", 20000), ("99999", 20000), ("500", 500), ("zzz", 1200), ("", 1200)):
            self.assertEqual(Settings({"NZBPO_MaxArticles": text}).max_articles, expected, text)

    def test_time_limit_clamped(self):
        for text, expected in (("10", 10), ("9", 10), ("1", 10), ("0", 10), ("300", 300), ("301", 300),
                               ("5000", 300), ("60", 60), ("fast", 90), ("", 90)):
            self.assertEqual(Settings({"NZBPO_TimeLimit": text}).time_limit, expected, text)

    def test_max_fill_percent_clamped(self):
        for text, expected in (("0", 0.0), ("100", 1.0), ("101", 1.0), ("150", 1.0), ("-5", 0.0),
                               ("50", 0.5), ("12.5", 0.125), ("lots", 1.0), ("", 1.0)):
            self.assertAlmostEqual(Settings({"NZBPO_MaxFillPercent": text}).max_fill, expected, msg=text)

    def test_numbers_never_leave_their_range(self):
        for text in ("-1e9", "1e9", "1e308", "-1e308", "nan", "0x10", "٣", "1_0", "  ", "+4"):
            s = Settings({"NZBPO_Connections": text, "NZBPO_FreshHours": text, "NZBPO_MaxArticles": text,
                          "NZBPO_TimeLimit": text, "NZBPO_MaxFillPercent": text})
            self.assertTrue(1 <= s.connections <= 8, text)
            self.assertTrue(0 <= s.fresh_seconds <= 72 * 3600, text)
            self.assertTrue(100 <= s.max_articles <= 20000, text)
            self.assertTrue(10 <= s.time_limit <= 300, text)
            self.assertTrue(0.0 <= s.max_fill <= 1.0, text)

    def test_infinite_numbers_fall_back_instead_of_crashing(self):
        # "inf"/"1e999" read as a float infinity; they must give the default or the limit, never an exception
        for text in ("inf", "-inf", "1e999", "Infinity"):
            s = Settings({"NZBPO_Connections": text, "NZBPO_MaxArticles": text, "NZBPO_TimeLimit": text,
                          "NZBPO_FreshHours": text, "NZBPO_MaxFillPercent": text})
            self.assertTrue(1 <= s.connections <= 8, text)
            self.assertTrue(100 <= s.max_articles <= 20000, text)
            self.assertTrue(10 <= s.time_limit <= 300, text)

    def test_verbose(self):
        self.assertTrue(Settings({"NZBPO_Verbose": "yes"}).verbose)
        self.assertTrue(Settings({"NZBPO_Verbose": "Yes"}).verbose)
        self.assertFalse(Settings({"NZBPO_Verbose": "no"}).verbose)
        self.assertFalse(Settings({"NZBPO_Verbose": "banana"}).verbose)

    def test_categories_parsing(self):
        s = Settings({"NZBPO_Categories": " TV , Movies;; anime ,, "})
        self.assertEqual(s.categories, frozenset({"tv", "movies", "anime"}))

    def test_wants(self):
        s = Settings({"NZBPO_Categories": "TV, Movies"})
        self.assertTrue(s.wants("tv"))
        self.assertTrue(s.wants("TV"))
        self.assertTrue(s.wants(" Movies "))
        self.assertFalse(s.wants("music"))
        self.assertFalse(s.wants(""))
        self.assertFalse(s.wants(None))

    def test_wants_everything_when_empty(self):
        for text in ("", " ", ",", " ; , "):
            s = Settings({"NZBPO_Categories": text})
            self.assertTrue(s.wants("anything"), text)
            self.assertTrue(s.wants(""))
            self.assertTrue(s.wants(None))

    def test_ignore_servers_ranges(self):
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "3, 7-11"}).ignore_servers,
                         frozenset({3, 7, 8, 9, 10, 11}))
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "1;2 3,4"}).ignore_servers, frozenset({1, 2, 3, 4}))
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "5"}).ignore_servers, frozenset({5}))
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "4-4"}).ignore_servers, frozenset({4}))

    def test_ignore_servers_garbage_ignored(self):
        s = Settings({"NZBPO_IgnoreServers": "abc, 5, 0, 9-3, -2, 1.5, x-y, 12abc, 6"})
        self.assertEqual(s.ignore_servers, frozenset({5, 6}))
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "all"}).ignore_servers, frozenset())

    def test_ignore_servers_zero_is_not_a_server(self):
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "0, 0-3"}).ignore_servers, frozenset())

    def test_ignore_servers_huge_range_refused(self):
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "1-100000"}).ignore_servers, frozenset())
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "1-5000, 2"}).ignore_servers, frozenset({2}))
        self.assertEqual(len(Settings({"NZBPO_IgnoreServers": "1-1000"}).ignore_servers), 1000)
        self.assertEqual(Settings({"NZBPO_IgnoreServers": "1-1001"}).ignore_servers, frozenset())

    def test_par_check_force(self):
        self.assertTrue(Settings({"NZBOP_ParCheck": "force"}).par_check_force)
        self.assertTrue(Settings({"NZBOP_ParCheck": " Force "}).par_check_force)
        self.assertFalse(Settings({"NZBOP_ParCheck": "auto"}).par_check_force)
        self.assertFalse(Settings({"NZBOP_ParCheck": "manual"}).par_check_force)
        self.assertFalse(Settings({"NZBOP_ParCheck": "always"}).par_check_force)
        self.assertFalse(Settings({}).par_check_force)

    def test_scheduler_active(self):
        for text in ("Completer", "Completer,Other", "Other, Completer", "Other;Completer;More",
                     " Completer ", "A,  Completer  ,B"):
            self.assertTrue(Settings({"NZBOP_Extensions": text}).scheduler_active, text)
        for text in ("", "CompleterX", "xCompleter", "Completer2", "Other,CompleterX", "My Completer",
                     "Completer.py", "Complete", "Other"):
            self.assertFalse(Settings({"NZBOP_Extensions": text}).scheduler_active, text)

    def test_scheduler_inactive_when_extensions_missing(self):
        self.assertFalse(Settings({}).scheduler_active)

    def test_nzbget_paths(self):
        s = Settings({"NZBOP_TempDir": " /var/tmp/nzb ", "NZBOP_CertStore": "/etc/certs.pem"})
        self.assertEqual(s.temp_dir, "/var/tmp/nzb")
        self.assertEqual(s.cert_store, "/etc/certs.pem")


def server_env(number, **fields):
    env = {}
    for key, value in fields.items():
        env["NZBOP_SERVER%d_%s" % (number, key.upper())] = value
    return env


class LoadServersTests(CleanState):
    def test_every_field_parsed(self):
        env = server_env(1, name="Main", host="news.invalid", port="563", username="someone", password="pw-1234",
                         encryption="yes", certverification="minimal", ipversion="ipv4", connections="20",
                         level="1", group="2", active="yes", optional="yes", retention="1500", joingroup="yes")
        (server,) = load_servers(env)
        self.assertEqual(server.id, 1)
        self.assertEqual(server.name, "Main")
        self.assertEqual(server.host, "news.invalid")
        self.assertEqual(server.port, 563)
        self.assertEqual(server.username, "someone")
        self.assertEqual(server.password, "pw-1234")
        self.assertTrue(server.tls)
        self.assertEqual(server.cert, "minimal")
        self.assertEqual(server.ip_version, "ipv4")
        self.assertEqual(server.connections, 20)
        self.assertEqual(server.level, 1)
        self.assertEqual(server.group, 2)
        self.assertTrue(server.active)
        self.assertTrue(server.optional)
        self.assertEqual(server.retention, 1500)
        self.assertTrue(server.join_group)
        self.assertEqual(server.label(), "server 1 (Main)")

    def test_defaults_for_a_bare_host(self):
        (server,) = load_servers(server_env(4, host="news.invalid"))
        self.assertEqual(server.id, 4)
        self.assertEqual(server.port, 119)
        self.assertEqual(server.username, "")
        self.assertEqual(server.password, "")
        self.assertFalse(server.tls)
        self.assertEqual(server.cert, "strict")
        self.assertEqual(server.ip_version, "auto")
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.level, 0)
        self.assertEqual(server.group, 0)
        self.assertTrue(server.active)
        self.assertFalse(server.optional)
        self.assertEqual(server.retention, 0)
        self.assertFalse(server.join_group)
        self.assertEqual(server.label(), "server 4")

    def test_numbering_follows_nzbget_and_is_sorted(self):
        env = {}
        for number in (10, 2, 7):
            env.update(server_env(number, host="s%d.invalid" % number))
        self.assertEqual([s.id for s in load_servers(env)], [2, 7, 10])

    def test_entries_without_host_skipped_but_numbers_kept(self):
        env = {}
        env.update(server_env(1, host="a.invalid"))
        env.update(server_env(2, host="", name="Empty"))
        env.update(server_env(3, host="   "))
        env.update(server_env(4, name="NoHostKey", port="119"))
        env.update(server_env(5, host=" c.invalid "))
        servers = load_servers(env)
        self.assertEqual([s.id for s in servers], [1, 5])
        self.assertEqual(servers[1].host, "c.invalid")

    def test_no_servers(self):
        self.assertEqual(load_servers({}), [])
        self.assertEqual(load_servers({"NZBOP_ServerX_Host": "a.invalid", "NZBPO_SERVER1_HOST": "a.invalid"}), [])

    def test_cert_check_no_turns_verification_off_everywhere(self):
        env = {"NZBOP_CertCheck": "no"}
        env.update(server_env(1, host="a.invalid", certverification="strict"))
        env.update(server_env(2, host="b.invalid", certverification="minimal"))
        env.update(server_env(3, host="c.invalid"))
        self.assertEqual([s.cert for s in load_servers(env)], ["none", "none", "none"])

    def test_cert_check_yes_keeps_the_per_server_value(self):
        env = {"NZBOP_CertCheck": "yes"}
        env.update(server_env(1, host="a.invalid", certverification="none"))
        env.update(server_env(2, host="b.invalid", certverification="minimal"))
        env.update(server_env(3, host="c.invalid", certverification="STRICT"))
        env.update(server_env(4, host="d.invalid", certverification="whatever"))
        self.assertEqual([s.cert for s in load_servers(env)], ["none", "minimal", "strict", "strict"])

    def test_bad_values_fall_back(self):
        env = server_env(1, host="a.invalid", ipversion="ipv9", certverification="", port="x", connections="-4",
                         level="500", retention="oops")
        (server,) = load_servers(env)
        self.assertEqual(server.ip_version, "auto")
        self.assertEqual(server.cert, "strict")
        self.assertEqual(server.port, 119)
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.level, 99)
        self.assertEqual(server.retention, 0)

    def test_port_is_clamped(self):
        (server,) = load_servers(server_env(1, host="a.invalid", port="70000"))
        self.assertEqual(server.port, 65535)

    def test_ip_version_case_insensitive(self):
        (server,) = load_servers(server_env(1, host="a.invalid", ipversion=" IPv6 "))
        self.assertEqual(server.ip_version, "ipv6")

    def test_inactive_and_optional_flags(self):
        env = {}
        env.update(server_env(1, host="a.invalid", active="no"))
        env.update(server_env(2, host="b.invalid", active="yes", optional="no"))
        env.update(server_env(3, host="c.invalid", active="garbage", optional="garbage"))
        env.update(server_env(4, host="d.invalid", optional="yes"))
        one, two, three, four = load_servers(env)
        self.assertFalse(one.active)
        self.assertTrue(two.active)
        self.assertFalse(two.optional)
        self.assertTrue(three.active)      # unreadable: NZBGet's default, active
        self.assertFalse(three.optional)   # unreadable: not optional
        self.assertTrue(four.optional)

    def test_inactive_servers_are_still_listed(self):
        # whether to ask them is up to the caller; the list is NZBGet's configuration
        (server,) = load_servers(server_env(1, host="a.invalid", active="no"))
        self.assertFalse(server.active)

    def test_name_is_cleaned_and_short(self):
        (server,) = load_servers(server_env(1, host="a.invalid", name="x\ny" + "z" * 100))
        self.assertNotIn("\n", server.name)
        self.assertLessEqual(len(server.name), 40)

    def test_password_is_registered_as_a_secret(self):
        load_servers(server_env(1, host="a.invalid", username="x", password="correct-horse-battery"))
        log.error("530 login failed for correct-horse-battery")
        self.assertIn("login failed for ***", self.out.getvalue())
        self.assertNotIn("correct-horse", self.out.getvalue())

    def test_long_username_is_registered_too(self):
        load_servers(server_env(1, host="a.invalid", username="alice-account", password="p"))
        log.info("user alice-account logged in")
        self.assertNotIn("alice-account", self.out.getvalue())

    def test_username_of_five_characters_is_registered(self):
        load_servers(server_env(1, host="a.invalid", username="abcde"))
        self.assertEqual(log.clean("abcde"), "***")

    def test_short_username_is_not_registered(self):
        load_servers(server_env(1, host="a.invalid", username="abcd", password="x"))
        self.assertEqual(log.clean("abcd x"), "abcd x")

    def test_every_servers_password_is_registered(self):
        env = {}
        env.update(server_env(1, host="a.invalid", password="first-secret"))
        env.update(server_env(2, host="b.invalid", password="second-secret"))
        load_servers(env)
        self.assertEqual(log.clean("first-secret second-secret"), "*** ***")

    def test_skipped_entry_registers_nothing(self):
        load_servers(server_env(1, host="", password="ghost-secret"))
        self.assertEqual(log.clean("ghost-secret"), "ghost-secret")

    def test_credentials_are_not_in_the_label(self):
        (server,) = load_servers(server_env(1, host="a.invalid", username="alice-account", password="p4ssw0rd!"))
        self.assertNotIn("p4ssw0rd", server.label())
        self.assertNotIn("alice-account", server.label())


class ControlTests(CleanState):
    def test_defaults(self):
        control = Control({})
        self.assertEqual(control.host, "127.0.0.1")
        self.assertEqual(control.port, 6789)
        self.assertEqual(control.username, "")
        self.assertEqual(control.password, "")
        self.assertEqual(control.url(), "http://127.0.0.1:6789/jsonrpc")

    def test_wildcard_and_empty_addresses_go_to_loopback(self):
        for text in ("", "0.0.0.0", "  ", " 0.0.0.0 "):
            self.assertEqual(Control({"NZBOP_ControlIP": text}).url(), "http://127.0.0.1:6789/jsonrpc", text)

    def test_ipv6_wildcard_goes_to_ipv6_loopback(self):
        for text in ("::", "[::]"):
            control = Control({"NZBOP_ControlIP": text})
            self.assertEqual(control.host, "::1", text)
            self.assertEqual(control.url(), "http://[::1]:6789/jsonrpc", text)

    def test_ipv6_host_is_bracketed(self):
        self.assertEqual(Control({"NZBOP_ControlIP": "::1"}).url(), "http://[::1]:6789/jsonrpc")
        self.assertEqual(Control({"NZBOP_ControlIP": "fe80::1"}).url(), "http://[fe80::1]:6789/jsonrpc")

    def test_ipv4_and_names_are_not_bracketed(self):
        self.assertEqual(Control({"NZBOP_ControlIP": "127.0.0.1"}).url(), "http://127.0.0.1:6789/jsonrpc")
        self.assertEqual(Control({"NZBOP_ControlIP": "nzb.invalid"}).url(), "http://nzb.invalid:6789/jsonrpc")

    def test_port(self):
        self.assertEqual(Control({"NZBOP_ControlPort": "7000"}).port, 7000)
        self.assertEqual(Control({"NZBOP_ControlPort": "x"}).port, 6789)
        self.assertEqual(Control({"NZBOP_ControlPort": ""}).port, 6789)
        self.assertEqual(Control({"NZBOP_ControlPort": "0"}).port, 1)
        self.assertEqual(Control({"NZBOP_ControlPort": "99999"}).port, 65535)
        self.assertEqual(Control({"NZBOP_ControlPort": "8080"}).url(), "http://127.0.0.1:8080/jsonrpc")

    def test_login(self):
        control = Control({"NZBOP_ControlUsername": "nzbget", "NZBOP_ControlPassword": "tegbzn-secret"})
        self.assertEqual(control.username, "nzbget")
        self.assertEqual(control.password, "tegbzn-secret")

    def test_password_is_registered_as_a_secret(self):
        Control({"NZBOP_ControlPassword": "tegbzn-secret"})
        log.warning("rejected tegbzn-secret")
        self.assertEqual(self.out.getvalue(), "[WARNING] rejected ***\n")

    def test_url_has_no_credentials(self):
        control = Control({"NZBOP_ControlUsername": "nzbget", "NZBOP_ControlPassword": "tegbzn-secret"})
        self.assertNotIn("tegbzn", control.url())
        self.assertNotIn("@", control.url())


if __name__ == "__main__":
    unittest.main()
