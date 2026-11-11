"""Tests of the fake NZBGet (``nzbgetsim``): the integration tests are only as good as it is."""

import base64
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nzbgetsim as S  # noqa: E402
import nzbtools  # noqa: E402


def rpc(sim, method, *params, user=S.CONTROL_USER, password=S.CONTROL_PASSWORD):
    """One JSON-RPC call made the way any client would; returns the decoded answer."""
    body = json.dumps({"method": method, "params": list(params)}).encode("utf-8")
    request = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % sim.port, data=body)
    request.add_header("Content-Type", "application/json")
    login = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    request.add_header("Authorization", "Basic " + login)
    with urllib.request.urlopen(request, timeout=10) as reply:
        return json.loads(reply.read().decode("utf-8"))


def result(sim, method, *params):
    return rpc(sim, method, *params)["result"]


class SimBase(S.SimTestCase):
    def setUp(self):
        self.clock = S.SimClock()
        self.sim = self.make_sim(clock=self.clock)

    def post(self, tag, **kwargs):
        kwargs.setdefault("now", self.clock.now)
        return nzbtools.make_post(tag, **kwargs)

    POST_KEYS = ("data_files", "segments", "par_files", "par_segments", "age_hours", "size", "par_size", "group",
                 "skip")

    def add(self, tag="p", name=None, **kwargs):
        run = kwargs.pop("run", False)
        post = self.post(tag, **{key: kwargs.pop(key) for key in self.POST_KEYS if key in kwargs})
        return self.sim.add(post, name or "Release " + tag, run=run, **kwargs)


class ParseTests(SimBase):
    def test_names_sizes_and_kinds_of_the_files(self):
        nzb_id = self.add("p", data_files=2, par_files=3)
        files = self.sim.entry(nzb_id).files
        self.assertEqual([item["name"] for item in files],
                         ["data.part01.rar", "data.part02.rar", "data.par2", "data.vol001+01.par2",
                          "data.vol002+02.par2"])
        self.assertEqual([item["par"] for item in files], [False, False, True, True, True])
        self.assertEqual([item["vol"] for item in files], [False, False, False, True, True])
        self.assertTrue(all(item["size"] > 0 and item["posted"] > 0 for item in files))
        self.assertEqual(len({item["id"] for item in files}), 5)

    def test_ids_are_not_reused_and_the_file_is_renamed(self):
        first = self.add("a")
        second = self.add("b")
        self.assertEqual((first, second), (1, 2))
        entry = self.sim.entry(first)
        self.assertTrue(entry.queued_file.endswith(".nzb.queued"))
        self.assertTrue(os.path.isfile(entry.queued_file))
        self.assertFalse(os.path.exists(entry.filename))

    def test_same_name_twice_gets_a_second_queued_file(self):
        first = self.add("a", name="Same")
        second = self.add("b", name="Same")
        self.assertNotEqual(self.sim.entry(first).queued_file, self.sim.entry(second).queued_file)
        self.assertTrue(self.sim.entry(second).queued_file.endswith(".2.queued"))


class ParPausingTests(SimBase):
    def paused(self, nzb_id):
        return self.sim.entry(nzb_id).paused_files

    def test_volumes_are_paused_when_a_plain_par_file_exists(self):
        nzb_id = self.add("p", par_files=3)
        self.assertEqual(self.paused(nzb_id), ["data.vol001+01.par2", "data.vol002+02.par2"])

    def test_without_a_plain_par_file_only_the_smallest_volume_stays(self):
        post = nzbtools.Post("v")
        for number, size in enumerate((900, 300, 600)):
            post.add_file('"data.vol%03d+%02d.par2"' % (number, number), 1, size, 1700000000, "g", True)
        post.add_file('"data.rar"', 1, 100, 1700000000, "g", False)
        nzb_id = self.sim.add(post, "V", run=False)
        self.assertEqual(self.paused(nzb_id), ["data.vol000+00.par2", "data.vol002+02.par2"])

    def test_par_check_force_pauses_nothing(self):
        self.sim.set_nzbget("ParCheck", "force")
        nzb_id = self.add("p", par_files=3)
        self.assertEqual(self.paused(nzb_id), [])

    def test_group_pause_extra_pars_and_resume(self):
        nzb_id = self.add("p", par_files=3, paused=True)
        self.assertEqual(self.sim.status(nzb_id), "PAUSED")
        self.sim.user_resume(nzb_id)
        self.assertEqual(self.paused(nzb_id), [])
        self.assertEqual(self.sim.status(nzb_id), "QUEUED")
        self.sim.user_pause(nzb_id)
        self.assertEqual(self.sim.status(nzb_id), "PAUSED")


class StatusTests(SimBase):
    def test_queue_status_words(self):
        nzb_id = self.add("p", paused=True)
        self.assertEqual(self.sim.status(nzb_id), "PAUSED")
        entry = self.sim.entry(nzb_id)
        entry.files[0]["paused"] = False
        self.assertEqual(self.sim.status(nzb_id), "QUEUED")  # one file may start: not paused as a whole
        entry.active = 2
        self.assertEqual(self.sim.status(nzb_id), "DOWNLOADING")

    def test_history_status_text_follows_nzbget(self):
        entry = S.Entry(1, "x", "")
        self.assertEqual(entry.text_status, "SUCCESS/ALL")
        for delete, text in (("HEALTH", "FAILURE/HEALTH"), ("MANUAL", "DELETED/MANUAL"), ("DUPE", "DELETED/DUPE"),
                             ("BAD", "FAILURE/BAD"), ("GOOD", "DELETED/GOOD"), ("COPY", "DELETED/COPY"),
                             ("SCAN", "FAILURE/SCAN")):
            entry.delete_status = delete
            self.assertEqual(entry.text_status, text)
        entry.mark_status = "BAD"
        self.assertEqual(entry.text_status, "FAILURE/BAD")  # a mark overrides the delete status
        entry.mark_status = "GOOD"
        self.assertEqual(entry.text_status, "SUCCESS/GOOD")
        entry.mark_status = "SUCCESS"
        self.assertEqual(entry.text_status, "SUCCESS/MARK")

    def test_sonarr_judgement(self):
        base = {"DeleteStatus": "NONE", "MarkStatus": "NONE", "ParStatus": "NONE", "UnpackStatus": "NONE",
                "MoveStatus": "NONE", "ScriptStatus": "NONE"}

        def judged(**changes):
            item = dict(base)
            item.update(changes)
            return S.sonarr_status(item)

        self.assertEqual(judged(), "completed")
        self.assertEqual(judged(ParStatus="SUCCESS", UnpackStatus="SUCCESS", ScriptStatus="SUCCESS"), "completed")
        self.assertEqual(judged(ParStatus="FAILURE"), "failed")
        self.assertEqual(judged(UnpackStatus="SPACE"), "warning")
        self.assertEqual(judged(UnpackStatus="FAILURE"), "failed")
        self.assertEqual(judged(MoveStatus="FAILURE"), "warning")
        self.assertEqual(judged(ScriptStatus="FAILURE"), "failed")
        for delete in ("HEALTH", "DUPE", "SCAN", "COPY", "BAD"):
            self.assertEqual(judged(DeleteStatus=delete), "failed", delete)
        self.assertEqual(judged(DeleteStatus="GOOD"), "warning")
        self.assertIsNone(judged(DeleteStatus="MANUAL"))  # deleted by hand: Sonarr ignores it...
        self.assertEqual(judged(DeleteStatus="MANUAL", MarkStatus="BAD"), "failed")  # ...unless it is marked bad


class RpcTests(SimBase):
    def test_login_is_required(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            rpc(self.sim, "listgroups", 0, password="wrong")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.assertEqual(self.sim.calls, [])

    def test_bad_requests_are_refused_without_a_call_record(self):
        request = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % self.sim.port, data=b"not json")
        login = base64.b64encode(("%s:%s" % (S.CONTROL_USER, S.CONTROL_PASSWORD)).encode()).decode()
        request.add_header("Authorization", "Basic " + login)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertEqual(self.sim.calls, [])

    def test_listgroups_shows_parameters_and_status(self):
        nzb_id = self.add("p", paused=True, category="TV")
        groups = result(self.sim, "listgroups", 0)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual((group["NZBID"], group["NZBName"], group["Category"], group["Status"], group["Kind"]),
                         (nzb_id, "Release p", "TV", "PAUSED", "NZB"))
        names = {item["Name"]: item["Value"] for item in group["Parameters"]}
        self.assertEqual(names["Completer:"], "yes")

    def test_history_lists_deleted_downloads_newest_first(self):
        first, second = self.add("a"), self.add("b")
        self.sim.user_delete(first)
        self.sim.user_delete(second)
        history = result(self.sim, "history", False)
        self.assertEqual([item["NZBID"] for item in history], [second, first])
        self.assertEqual(history[0]["DeleteStatus"], "MANUAL")
        self.assertEqual(history[0]["Status"], "DELETED/MANUAL")
        self.assertEqual(result(self.sim, "listgroups", 0), [])

    def test_a_deleted_download_shows_up_in_the_history_after_the_delay(self):
        self.sim.history_delay = 2
        nzb_id = self.add("a")
        self.sim.user_delete(nzb_id)
        self.assertEqual(result(self.sim, "history", False), [])
        self.assertEqual(result(self.sim, "history", False), [])
        self.assertEqual([item["NZBID"] for item in result(self.sim, "history", False)], [nzb_id])

    def test_status_reports_the_download_rate(self):
        self.sim.download_rate = 12345
        self.assertEqual(result(self.sim, "status")["DownloadRate"], 12345)

    def test_down_and_up_again_on_the_same_port(self):
        port = self.sim.port
        self.sim.rpc_down()
        with self.assertRaises(urllib.error.URLError):
            rpc(self.sim, "status")
        self.sim.rpc_up()
        self.assertEqual(self.sim.port, port)
        self.assertIn("DownloadRate", result(self.sim, "status"))

    def test_faults(self):
        self.sim.fault("http500", times=1)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            rpc(self.sim, "status")
        self.assertEqual(caught.exception.code, 500)
        caught.exception.close()
        self.sim.fault("error", method="status")
        self.assertEqual(rpc(self.sim, "status")["error"]["code"], 3)
        self.sim.fault("garbage", times=1)
        with self.assertRaises(ValueError):
            rpc(self.sim, "status")
        self.sim.fault("drop", times=1)
        with self.assertRaises(Exception):
            rpc(self.sim, "status")
        self.assertIn("result", rpc(self.sim, "status"))  # all faults used up

    def test_a_fault_for_one_method_leaves_the_others_alone(self):
        self.sim.fault("http500", method="history", times=None)
        self.assertEqual(result(self.sim, "listgroups", 0), [])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            rpc(self.sim, "history", False)
        caught.exception.close()


class EditQueueTests(SimBase):
    """The sim carries out every edit, and writes the ones Completer must never make to ``violations``."""

    def setUp(self):
        SimBase.setUp(self)
        self.sim.expect_violations = True  # these tests send forbidden edits on purpose
        self.nzb_id = self.add("p", paused=True, par_files=2)
        self.sim.entry(self.nzb_id).set("Completer_Token", "tok")

    def violations(self):
        found, self.sim.violations = self.sim.violations, []
        return found

    def test_the_allowed_edits_are_clean_and_work(self):
        self.assertTrue(result(self.sim, "editqueue", "GroupResume", "", [self.nzb_id]))
        self.assertEqual(self.sim.entry(self.nzb_id).paused_files, [])
        self.assertTrue(result(self.sim, "editqueue", "GroupPauseExtraPars", "", [self.nzb_id]))
        self.assertEqual(self.sim.entry(self.nzb_id).paused_files, ["data.vol001+01.par2"])
        self.assertTrue(result(self.sim, "editqueue", "GroupSetParameter", "Completer_State=passed", [self.nzb_id]))
        self.assertEqual(self.sim.param(self.nzb_id, "Completer_State"), "passed")
        self.assertEqual(self.violations(), [])

    def test_group_delete_moves_to_history_and_history_edits_work(self):
        self.assertTrue(result(self.sim, "editqueue", "GroupDelete", "", [self.nzb_id]))
        self.assertEqual(self.sim.where(self.nzb_id), "history")
        self.assertEqual(self.sim.status(self.nzb_id), "DELETED/MANUAL")
        self.assertTrue(result(self.sim, "editqueue", "HistoryMarkBad", "", [self.nzb_id]))
        self.assertEqual(self.sim.status(self.nzb_id), "FAILURE/BAD")
        self.assertEqual(self.sim.sonarr_view(self.nzb_id), "failed")
        self.assertTrue(result(self.sim, "editqueue", "HistorySetParameter", "Completer_State=failed",
                               [self.nzb_id]))
        self.assertEqual(self.sim.param(self.nzb_id, "Completer_State"), "failed")
        self.assertEqual(self.violations(), [])

    def test_an_edit_of_something_that_is_not_there_is_false(self):
        self.assertFalse(result(self.sim, "editqueue", "GroupResume", "", [999]))
        self.assertTrue(any("not in the queue" in text for text in self.violations()))

    def test_file_delete_is_a_violation_and_really_deletes(self):
        file_id = self.sim.entry(self.nzb_id).files[0]["id"]
        self.assertTrue(result(self.sim, "editqueue", "FileDelete", "", [file_id]))
        self.assertEqual(len(self.sim.entry(self.nzb_id).files), 4)
        self.assertTrue(any("FileDelete is not a command" in text for text in self.violations()))

    def test_lists_and_wrong_shapes_are_violations(self):
        other = self.add("q", paused=True)
        self.sim.entry(other).set("Completer_Token", "tok2")
        result(self.sim, "editqueue", "GroupResume", "", [self.nzb_id, other])
        self.assertTrue(any("not exactly one positive integer id" in text for text in self.violations()))
        result(self.sim, "editqueue", "GroupResume", "", [])
        self.assertTrue(any("not exactly one positive integer id" in text for text in self.violations()))
        result(self.sim, "editqueue", "GroupResume", "", [str(self.nzb_id)])
        self.assertTrue(any("not exactly one positive integer id" in text for text in self.violations()))
        result(self.sim, "editqueue", "GroupResume", "", [0])
        self.assertTrue(any("not exactly one positive integer id" in text for text in self.violations()))

    def test_a_download_without_a_token_must_not_be_edited(self):
        other = self.add("q", paused=True)
        result(self.sim, "editqueue", "GroupResume", "", [other])
        self.assertTrue(any("carries no Completer token" in text for text in self.violations()))

    def test_only_completer_parameters_may_be_written(self):
        result(self.sim, "editqueue", "GroupSetParameter", "Other=1", [self.nzb_id])
        self.assertTrue(any("not a parameter of Completer" in text for text in self.violations()))
        result(self.sim, "editqueue", "GroupSetParameter", "*Unpack:=no", [self.nzb_id])
        self.assertTrue(any("not a parameter of Completer" in text for text in self.violations()))

    def test_other_methods_and_commands_are_violations(self):
        result(self.sim, "pausedownload")
        self.assertTrue(any("not one Completer uses" in text for text in self.violations()))
        self.assertTrue(self.sim.download_paused)
        result(self.sim, "editqueue", "GroupPause", "", [self.nzb_id])
        self.assertTrue(any("GroupPause is not a command" in text for text in self.violations()))

    def test_allowed_ids_limits_the_reach_of_an_edit(self):
        self.sim.allowed_ids = {self.nzb_id + 1}
        result(self.sim, "editqueue", "GroupResume", "", [self.nzb_id])
        self.assertTrue(any("outside the downloads" in text for text in self.violations()))

    def test_a_clean_sim_has_no_violations_and_unclean_ones_would_fail_the_case(self):
        self.assertEqual(self.sim.violations, [])


class SnapshotTests(SimBase):
    def test_snapshots_are_equal_until_something_changes(self):
        ids = [self.add(tag, paused=True) for tag in "abc"]
        before = self.sim.snapshot()
        self.assertEqual(before, self.sim.snapshot())
        self.assertEqual(set(before), set(ids))
        self.sim.user_resume(ids[1])
        after = self.sim.snapshot()
        self.assertNotEqual(before[ids[1]], after[ids[1]])
        self.assertEqual(before[ids[0]], after[ids[0]])
        self.assertEqual(before[ids[2]], after[ids[2]])
        self.assertEqual(set(self.sim.snapshot({ids[0]})), {ids[0]})


class EnvironmentTests(SimBase):
    def test_both_spellings_of_parameters_and_options_are_exported(self):
        nzb_id = self.add("p", category="TV")
        env = self.sim.queue_env(self.sim.entry(nzb_id))
        self.assertEqual(env["NZBPR_Completer:"], "yes")
        self.assertEqual(env["NZBPR_COMPLETER_"], "yes")
        self.assertEqual(env["NZBPR_*Unpack:"], "yes")
        self.assertEqual(env["NZBPR__UNPACK_"], "yes")
        self.assertEqual(env["NZBNA_EVENT"], "NZB_ADDED")
        self.assertEqual(env["NZBNA_NZBID"], str(nzb_id))
        self.assertEqual(env["NZBNA_CATEGORY"], "TV")
        self.assertEqual(env["NZBOP_TempDir"], self.sim.temp_dir)
        self.assertEqual(env["NZBOP_TEMPDIR"], self.sim.temp_dir)
        self.assertEqual(env["NZBOP_ControlPort"], str(self.sim.port))
        self.assertEqual(env["NZBPO_Mode"], "DryRun")
        self.assertEqual(env["NZBOP_Extensions"], "Completer")

    def test_a_parameter_of_the_extension_is_exported_without_its_prefix_too(self):
        nzb_id = self.add("p", params={"Completer:Extra": "7"})
        env = self.sim.queue_env(self.sim.entry(nzb_id))
        self.assertEqual(env["NZBPR_Extra"], "7")

    def test_servers_appear_in_both_spellings(self):
        news = self.make_news()
        number = self.sim.add_server(news, connections=3)
        env = self.sim.base_env()
        self.assertEqual(number, 1)
        self.assertEqual(env["NZBOP_SERVER1_HOST"], "127.0.0.1")
        self.assertEqual(env["NZBOP_Server1.Host"], "127.0.0.1")
        self.assertEqual(env["NZBOP_SERVER1_CONNECTIONS"], "3")
        self.assertEqual(env["NZBOP_Server1.Connections"], "3")
        self.assertEqual(self.sim.add_server(news), 2)
        self.assertEqual(env["NZBOP_CertCheck"], "yes")

    def test_queue_scripts_are_only_run_for_downloads_that_have_the_extension_switched_on(self):
        news = self.make_news()
        self.sim.add_server(news)
        off = self.add("a", queue_scripts="no", run=True)
        none = self.add("b", queue_scripts=False, run=True)
        on = self.add("c", run=True)
        self.assertEqual(self.sim.runs_of("queue")[0].env["NZBNA_NZBID"], str(on))
        self.assertEqual(len(self.sim.runs_of("queue")), 1)
        self.assertNotIn("Completer:", self.sim.entry(none).param_dict())
        self.assertEqual(self.sim.entry(off).get("Completer:"), "no")

    def test_when_the_extension_is_not_in_the_extensions_list_nothing_runs(self):
        self.sim.set_nzbget("Extensions", "Other")
        self.add("a", run=True)
        self.assertEqual(self.sim.runs, [])


class RunTests(SimBase):
    def test_runs_are_recorded_and_classified(self):
        news = self.make_news(have=[])
        self.sim.add_server(news)
        run = self.sim.sweep()
        self.assertEqual(run.kind, "sweep")
        self.assertEqual(run.code, 0)
        self.assertEqual(self.sim.runs, [run])
        self.assertEqual(self.sim.runs_of("sweep"), [run])

    def test_a_run_that_prints_something_unclassifiable_is_a_violation(self):
        self.sim.expect_violations = True
        run = S.RunResult("sweep", 0, "hello\n[INFO] fine\n")
        self.sim._inspect(run)
        self.assertEqual(len(self.sim.violations), 1)
        self.assertIn("cannot classify", self.sim.violations[0])
        self.assertEqual(run.messages, [("info", "fine")])
        self.assertTrue(run.says("fin"))

    def test_standard_error_is_a_violation(self):
        self.sim.expect_violations = True
        self.sim._inspect(S.RunResult("sweep", 1, "", "Traceback ..."))
        self.assertTrue(any("standard error" in text for text in self.sim.violations))

    def test_the_command_button_is_run_with_its_name(self):
        news = self.make_news()
        self.sim.add_server(news)
        run = self.sim.command("TestServers")
        self.assertEqual(run.kind, "command")
        self.assertEqual(run.code, 93)


class EventTests(SimBase):
    def test_added_events_are_queued_and_a_deleted_download_is_dropped(self):
        news = self.make_news()
        self.sim.add_server(news)
        first = self.add("a")
        second = self.add("b")
        self.assertEqual([item[1] for item in self.sim.events], [first, second])
        self.sim.user_delete(first)
        results = self.sim.pump()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].env["NZBNA_NZBID"], str(second))
        self.assertEqual(self.sim.events, [])


class WorldTests(S.SimTestCase):
    def test_an_enforce_world_has_its_server_and_a_first_sweep(self):
        sim = self.world(have=["x@test.invalid"])
        self.assertEqual(len(sim.news), 1)
        self.assertEqual(sim.news[0].have, {"x@test.invalid"})
        self.assertEqual(len(sim.runs_of("sweep")), 1)
        self.assertTrue(os.path.exists(os.path.join(sim.temp_dir, "completer-alive")))
        self.assertEqual(sim.option_values["Mode"], "Enforce")

    def test_a_dry_run_world_has_no_first_sweep(self):
        sim = self.world(mode="DryRun")
        self.assertEqual(sim.runs, [])
        self.assertEqual(sim.option_values["Mode"], "DryRun")

    def test_options_and_nzbget_settings_are_passed_on(self):
        sim = self.world(options={"FreshHours": "5"}, nzbget={"ParCheck": "force"}, boot=False)
        env = sim.base_env()
        self.assertEqual(env["NZBPO_FreshHours"], "5")
        self.assertEqual(env["NZBOP_ParCheck"], "force")
        self.assertEqual(sim.runs, [])

    def test_posts_are_dated_by_the_sim_clock(self):
        self.world(mode="Off")
        post = self.post("a", age_hours=10)
        self.assertEqual(post.files[0]["posted"], int(self.sim.now - 10 * 3600))
        self.sim.advance(3600)
        later = self.post("b", age_hours=10)
        self.assertEqual(later.files[0]["posted"], int(self.sim.now - 10 * 3600))

    def test_a_process_world_uses_the_real_clock_and_a_real_process(self):
        sim = self.world(inprocess=False)
        self.assertFalse(sim.inprocess)
        self.assertLess(abs(sim.now - time.time()), 5)
        self.assertEqual(sim.runs_of("sweep")[0].code, 0)
        with self.assertRaises(RuntimeError):
            sim.advance(1)

    def test_servers_added_later_are_kept_in_order(self):
        sim = self.world()
        second = self.make_news()
        self.assertEqual(sim.add_server(second, level=1), 2)
        self.assertEqual(sim.news[1], second)
        self.assertEqual(sim.add_server(second, number=7), 7)
        self.assertEqual(len(sim.news), 2)


if __name__ == "__main__":
    unittest.main()
