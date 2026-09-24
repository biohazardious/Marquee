"""The web front end: job lifecycle, the JSON API, and the token gate."""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from marquee import config as configuration
from marquee.errors import MarqueeError
from marquee.pipeline import SourceOptions
from marquee.web.job import Job
from marquee.web.server import Application, Handler


def wait_for(job, *states, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in states:
            return job.state
        time.sleep(0.02)
    raise AssertionError(f"job stuck in {job.state}, wanted one of {states}")


class TestJob:
    def test_starts_idle(self):
        assert Job().state == "idle"

    def test_plan_runs_and_lands_in_planned(self, config, romset, xml_path, catlist_path):
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        assert wait_for(job, "planned", "error") == "planned"
        assert job.plan.items
        assert job.resolution.xml_version == "0.252"

    def test_events_are_collected_with_a_cursor(self, config, romset, xml_path, catlist_path):
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        everything = job.snapshot(0)["events"]
        assert everything
        assert job.snapshot(everything[-1]["n"])["events"] == []

    def test_snapshot_exposes_the_genre_list(self, config, romset, xml_path, catlist_path):
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        assert "Maze" in job.snapshot(0)["resolution"]["genres"]

    def test_a_chd_folder_with_no_disks_is_called_out(self, config, romset, xml_path,
                                                      catlist_path, tmp_path):
        """311 missing disks read as "not downloaded yet" when the CHD folder was simply
        pointed one level too high; the plan has to say which it is."""
        empty = tmp_path / "no-disks-here"
        empty.mkdir()
        config.chd_dir = str(empty)
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        snapshot = job.snapshot(0)
        assert snapshot["resolution"]["chd_set_found"] is False
        assert snapshot["plan"]["missing_chds"] > 0
        assert any("No CHD set under" in event["text"] for event in snapshot["events"])

    def test_a_chd_folder_that_holds_the_set_is_fine(self, config, romset, xml_path,
                                                     catlist_path):
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        snapshot = job.snapshot(0)
        assert snapshot["resolution"]["chd_set_found"] is True
        assert not any("No CHD set" in event["text"] for event in snapshot["events"])

    def test_copy_after_plan(self, config, romset, xml_path, catlist_path):
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        job.start_copy()
        assert wait_for(job, "done", "error") == "done"
        assert (romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip").exists()
        assert job.snapshot(0)["summary"]["machines"] > 0

    def test_copy_without_a_plan_is_refused(self):
        with pytest.raises(MarqueeError, match="Nothing planned"):
            Job().start_copy()

    def test_errors_land_in_the_error_state(self, config, romset):
        job = Job()
        job.start_plan(config, SourceOptions(xml="/nonexistent/x.xml", offline=True))
        assert wait_for(job, "planned", "error") == "error"
        assert job.error and "mame" in job.error.lower()

    def test_plan_failure_is_reported_to_the_page(self, config, romset):
        job = Job()
        job.start_plan(config, SourceOptions(xml="/nonexistent/x.xml", offline=True))
        wait_for(job, "error")
        assert job.snapshot(0)["error"]

    def test_event_log_is_capped(self):
        job = Job()
        for index in range(3000):
            job.add_event("info", str(index))
        assert len(job.events) <= 2000
        # The tail is what survives, not the head.
        assert job.events[-1]["text"] == "2999"

    def test_cancel_stops_a_running_copy(self, config, romset, xml_path, catlist_path,
                                         monkeypatch):
        """Cancelling once the copy is genuinely under way, not before it starts."""
        from marquee.web import job as job_module

        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")

        original = job_module.pipeline.execute

        def cancel_then_run(*args, **kwargs):
            job.cancel()
            return original(*args, **kwargs)

        monkeypatch.setattr(job_module.pipeline, "execute", cancel_then_run)
        job.start_copy()
        wait_for(job, "done", "error")
        assert job.summary.cancelled is True
        assert any("Stopping" in event["text"] for event in job.events)

    def test_a_fresh_plan_clears_a_previous_cancel(self, config, romset, xml_path,
                                                   catlist_path):
        job = Job()
        job.cancel()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        job.start_copy()
        wait_for(job, "done", "error")
        assert job.summary.cancelled is False


@pytest.fixture
def server(tmp_path, config, romset):
    config.settings_path = str(tmp_path / "settings.ini")
    configuration.write_settings_file(config.settings_path, config)
    app = Application(config.settings_path)
    handler = type("Bound", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, app
    httpd.shutdown()
    httpd.server_close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.loads(response.read())


def post(base, path, payload):
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


class TestApi:
    def test_index_is_served(self, server):
        base, _app = server
        with urllib.request.urlopen(base + "/", timeout=10) as response:
            body = response.read().decode()
        assert "<title>Marquee</title>" in body
        assert 'src="/static/js/app.js"' in body

    def test_state_carries_the_config(self, server, romset):
        base, _app = server
        payload = get(base, "/api/state")
        assert payload["state"] == "idle"
        assert payload["config"]["rom_dir"] == str(romset["rom_dir"])

    def test_browse_lists_directories(self, server, romset):
        base, _app = server
        payload = get(base, "/api/browse?path=" + str(romset["chd_dir"]))
        assert "parentchd" in payload["directories"]
        assert payload["parent"]

    def test_unreadable_folder_is_a_note_not_a_failure(self, server):
        """The picker has to stay usable when someone wanders into /root en route."""
        base, _app = server
        payload = get(base, "/api/browse?path=/root")
        assert payload["directories"] == []
        assert payload["note"]
        assert payload["parent"] == "/"

    def test_browse_reports_whether_a_folder_can_be_written_to(self, server, romset):
        base, _app = server
        assert get(base, "/api/browse?path=" + str(romset["out_dir"]))["writable"] is True

    def test_nonexistent_path_falls_back_to_its_parent(self, server, romset):
        base, _app = server
        payload = get(base, "/api/browse?path=" + str(romset["out_dir"]) + "/nope")
        assert payload["path"] == str(romset["out_dir"])

    def test_plan_then_copy(self, server, romset, xml_path, catlist_path):
        base, app = server
        assert post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path}) == \
            {"started": "plan"}
        assert wait_for(app.job, "planned", "error") == "planned"

        plan = get(base, "/api/state")["plan"]
        assert plan["machines"] > 0 and plan["bytes_human"]
        assert plan["folders"][0]["name"]

        post(base, "/api/copy", {})
        assert wait_for(app.job, "done", "error") == "done"
        assert (romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip").exists()

    def test_clearing_a_path_in_the_form_is_an_error_not_a_silent_fallback(self, server):
        """An empty field must not quietly fall back to the saved settings.ini value."""
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/plan", {"rom_dir": "", "chd_dir": "", "copy_path": ""})
        assert error.value.code == 400
        assert "rom_dir" in json.loads(error.value.read())["error"]

    def test_a_field_the_page_omits_keeps_its_saved_value(self, server, romset,
                                                          xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.config.rom_dir == str(romset["rom_dir"])

    def test_save_with_a_cleared_path_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/save", {"copy_path": ""})
        assert error.value.code == 400

    def test_save_writes_the_settings_file(self, server, romset, tmp_path):
        base, app = server
        payload = post(base, "/api/save", {
            "rom_dir": str(romset["rom_dir"]), "chd_dir": str(romset["chd_dir"]),
            "copy_path": str(romset["out_dir"]), "allow_mature": True,
            "blacklist_genres": ["Casino"], "blacklist_roms": ["pong"],
            "blacklist_categories": ["Maze / Misc.", "Fighter / 2.5D"]})
        assert payload["saved"] == app.settings_path
        again = configuration.read_settings_file(app.settings_path)
        assert again.blacklist_genres == ["Casino"]
        assert again.blacklist_categories == ["Maze / Misc.", "Fighter / 2.5D"]
        assert again.blacklist_roms == ["pong"]
        assert again.allow_mature is True

    def test_unknown_route_is_404(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            get(base, "/api/nope")
        assert error.value.code == 404

    def test_bad_json_body_is_a_400(self, server):
        base, _app = server
        request = urllib.request.Request(base + "/api/plan", data=b"{not json",
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=10)
        assert error.value.code == 400


class TestToken:
    @pytest.fixture
    def guarded(self, tmp_path, config, romset):
        config.settings_path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(config.settings_path, config)
        app = Application(config.settings_path, token="s3cret")
        handler = type("Bound", (Handler,), {"app": app})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()
        httpd.server_close()

    def test_without_a_token_is_refused(self, guarded):
        with pytest.raises(urllib.error.HTTPError) as error:
            get(guarded, "/api/state")
        # 401, not 403: the page tells them apart to decide whether to ask for a key.
        assert error.value.code == 401

    def test_query_token_is_accepted(self, guarded):
        assert get(guarded, "/api/state?token=s3cret")["state"] == "idle"

    def test_header_token_is_accepted(self, guarded):
        request = urllib.request.Request(guarded + "/api/state",
                                         headers={"X-Token": "s3cret"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.loads(response.read())["state"] == "idle"

    def test_wrong_token_is_refused(self, guarded):
        with pytest.raises(urllib.error.HTTPError) as error:
            get(guarded, "/api/state?token=wrong")
        assert error.value.code == 401

    def test_post_is_guarded_too(self, guarded):
        with pytest.raises(urllib.error.HTTPError) as error:
            post(guarded, "/api/plan", {})
        assert error.value.code == 401

    def test_the_shell_loads_without_a_key(self, guarded):
        """The page itself has to be reachable, or the key can never be entered.

        The browser fetches the stylesheet and the modules on its own and a
        <script src> carries no query string, so guarding them made the UI impossible
        to open whenever a key was in force -- which, in a container, is always.
        """
        for path in ("/", "/static/app.css", "/static/js/app.js"):
            with urllib.request.urlopen(guarded + path, timeout=10) as response:
                assert response.status == 200
                assert response.read()

    def test_the_shell_carries_no_data(self, guarded):
        """Serving it unguarded is only safe because there is nothing in it."""
        with urllib.request.urlopen(guarded + "/", timeout=10) as response:
            page = response.read().decode()
        assert "s3cret" not in page
        for leak in ("rom_dir", "copy_path", "/tmp/"):
            assert leak not in page

    def test_health_says_whether_a_key_is_needed(self, guarded):
        # So the page knows to show the unlock screen rather than guessing.
        assert get(guarded, "/api/health")["auth"] is True

    def test_an_open_server_says_so(self, server):
        base, _app = server
        assert get(base, "/api/health")["auth"] is False


class TestSurvey:
    def test_runs_in_the_background_and_lands(self, server, romset):
        base, app = server
        assert post(base, "/api/survey", {})["started"] is True
        deadline = time.time() + 20
        while time.time() < deadline and app.survey["running"]:
            time.sleep(0.02)
        data = get(base, "/api/state")["survey"]["data"]
        assert data["roms"]["files"] == 11
        assert data["roms"]["bytes_human"]
        assert data["chds"]["files"] == 4

    def test_repeat_for_the_same_paths_is_a_noop(self, server, romset):
        base, app = server
        post(base, "/api/survey", {})
        deadline = time.time() + 20
        while time.time() < deadline and app.survey["running"]:
            time.sleep(0.02)
        assert post(base, "/api/survey", {})["started"] is False

    def test_one_folder_for_both_is_surveyed_once(self, server, romset):
        """A flat romset keeps ROMs and CHDs together; walking it twice doubles a
        ten second scan and shows the same figure in two cards."""
        base, app = server
        post(base, "/api/survey", {"rom_dir": str(romset["rom_dir"]),
                                   "chd_dir": str(romset["rom_dir"])})
        deadline = time.time() + 20
        while time.time() < deadline and app.survey["running"]:
            time.sleep(0.02)
        data = app.survey["data"]
        assert "chds" not in data
        assert data["roms"]["files"] == 11

    def test_remote_destination_is_not_walked(self, server, romset):
        base, app = server
        post(base, "/api/survey", {"copy_path": "smb://nas/Share/roms"})
        deadline = time.time() + 20
        while time.time() < deadline and app.survey["running"]:
            time.sleep(0.02)
        assert app.survey["data"]["destination"]["path"] is None


class TestSyncInTheApi:
    def plan_now(self, base, app, xml_path, catlist_path):
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        assert wait_for(app.job, "planned", "error") == "planned"
        return get(base, "/api/state")["plan"]

    def test_plan_reports_the_difference(self, server, romset, xml_path, catlist_path):
        base, app = server
        plan = self.plan_now(base, app, xml_path, catlist_path)
        assert plan["sync"]["new"]["count"] == plan["files"]
        assert plan["sync"]["keep"]["count"] == 0
        assert plan["to_transfer_human"]

    def test_second_plan_after_a_copy_sees_everything_in_place(
            self, server, romset, xml_path, catlist_path):
        base, app = server
        self.plan_now(base, app, xml_path, catlist_path)
        post(base, "/api/copy", {})
        assert wait_for(app.job, "done", "error") == "done"

        plan = self.plan_now(base, app, xml_path, catlist_path)
        assert plan["sync"]["keep"]["count"] == plan["files"]
        assert plan["sync"]["new"]["count"] == 0

    def test_orphans_are_listed_and_can_be_removed(self, server, romset, xml_path,
                                                   catlist_path):
        base, app = server
        self.plan_now(base, app, xml_path, catlist_path)
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")

        stray = romset["out_dir"] / "Old" / "leftover.zip"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"x")

        plan = self.plan_now(base, app, xml_path, catlist_path)
        assert plan["sync"]["orphan"]["count"] == 1
        assert "Old/leftover.zip" in plan["orphan_examples"]

        post(base, "/api/copy", {"delete_orphans": True})
        wait_for(app.job, "done", "error")
        assert not stray.exists()
        assert get(base, "/api/state")["summary"]["deleted"] == 1

    def test_summary_breaks_the_work_down(self, server, romset, xml_path, catlist_path):
        base, app = server
        self.plan_now(base, app, xml_path, catlist_path)
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        summary = get(base, "/api/state")["summary"]
        assert summary["copied"] > 0
        assert summary["skipped"] == 0 and summary["moved"] == 0


class TestMachineRows:
    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        base, app = server
        self.xml, self.catlist = xml_path, catlist_path
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_lists_the_plans_machines(self, planned, romset):
        base, _app = planned
        data = get(base, "/api/machines")
        assert data["total"] > 0
        assert {row["name"] for row in data["rows"]} >= {"goodgame", "dotgame"}

    def test_rows_carry_what_the_table_shows(self, planned, romset):
        base, _app = planned
        row = next(r for r in get(base, "/api/machines")["rows"] if r["name"] == "twodisk")
        assert row["category"] and row["genre"] and row["bytes_human"]
        assert row["chd"] is True
        assert row["status"] == "new"

    def test_search_matches_name_and_title(self, planned, romset):
        base, _app = planned
        assert get(base, "/api/machines?q=dotgame")["total"] == 1
        assert get(base, "/api/machines?q=uncategorised")["total"] == 1

    def test_filter_by_status(self, planned, romset):
        base, app = planned
        # Everything on disk is new; the catalogue also lists what is not on disk.
        assert get(base, "/api/machines?status=new")["total"] == \
            get(base, "/api/machines?have=yes")["total"]
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        plan_again = get(base, "/api/state")["config"]
        post(base, "/api/plan", dict(plan_again, xml=self.xml, catlist=self.catlist))
        wait_for(app.job, "planned", "error")
        assert get(base, "/api/machines?status=keep")["total"] > 0
        assert get(base, "/api/machines?status=new")["total"] == 0

    def test_filter_by_genre(self, planned, romset):
        base, _app = planned
        rows = get(base, "/api/machines?genre=Maze")["rows"]
        assert rows and all(row["genre"] == "Maze" for row in rows)

    def test_sorting_and_paging(self, planned, romset):
        base, _app = planned
        big = get(base, "/api/machines?sort=size&dir=desc")["rows"]
        assert big[0]["bytes"] >= big[-1]["bytes"]
        first = get(base, "/api/machines?limit=2")
        second = get(base, "/api/machines?limit=2&offset=2")
        assert first["rows"] != second["rows"]
        assert second["offset"] == 2

    def test_limit_is_capped(self, planned, romset):
        base, _app = planned
        assert len(get(base, "/api/machines?limit=99999")["rows"]) <= 500

    def test_without_a_plan_it_is_empty_not_an_error(self, server):
        base, _app = server
        assert get(base, "/api/machines") == {"total": 0, "offset": 0, "rows": []}


class TestGenreWeights:
    def test_plan_reports_every_genre_with_its_weight(self, server, romset, xml_path,
                                                      catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        genres = {g["name"]: g for g in get(base, "/api/state")["plan"]["genres"]}
        assert "Maze" in genres and genres["Maze"]["machines"] > 0
        assert genres["Maze"]["bytes_human"]

    def test_excluded_genres_are_still_measured(self, server, romset, xml_path,
                                                catlist_path):
        """Deciding what to leave out needs the weight of what is currently left out."""
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_genres": ["Board Game"]})
        wait_for(app.job, "planned", "error")
        genres = {g["name"]: g for g in get(base, "/api/state")["plan"]["genres"]}
        assert genres["Board Game"]["excluded"] is True
        assert genres["Board Game"]["machines"] == 1
        assert genres["Maze"]["excluded"] is False

    def test_genre_weights_sum_to_the_plan(self, server, romset, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_genres": []})
        wait_for(app.job, "planned", "error")
        plan = get(base, "/api/state")["plan"]
        assert sum(g["bytes"] for g in plan["genres"]) == plan["bytes"]


class TestVersionsEndpoint:
    def test_list_arrives_in_the_background(self, server, monkeypatch):
        from marquee import fetch
        monkeypatch.setattr(fetch, "version_catalogue", lambda: [
            {"version": "0.289", "xml": True, "catlist": True, "usable": True, "reason": None},
            {"version": "0.288", "xml": False, "catlist": True, "usable": False,
             "reason": "no listxml published for this version"}])
        base, app = server
        assert post(base, "/api/versions", {})["started"] is True
        deadline = time.time() + 15
        while time.time() < deadline and app.versions["running"]:
            time.sleep(0.02)
        data = get(base, "/api/state")["versions"]["data"]
        assert [e["version"] for e in data] == ["0.289", "0.288"]
        assert data[1]["usable"] is False and data[1]["reason"]

    def test_a_failure_is_reported_not_raised(self, server, monkeypatch):
        from marquee import fetch

        def explode():
            raise RuntimeError("github is down")

        monkeypatch.setattr(fetch, "version_catalogue", explode)
        base, app = server
        post(base, "/api/versions", {})
        deadline = time.time() + 15
        while time.time() < deadline and app.versions["running"]:
            time.sleep(0.02)
        assert "github is down" in get(base, "/api/state")["versions"]["error"]

    def test_second_call_reuses_the_list(self, server, monkeypatch):
        from marquee import fetch
        monkeypatch.setattr(fetch, "version_catalogue", lambda: [])
        base, app = server
        post(base, "/api/versions", {})
        deadline = time.time() + 15
        while time.time() < deadline and app.versions["running"]:
            time.sleep(0.02)
        app.versions["data"] = [{"version": "0.289"}]
        assert post(base, "/api/versions", {})["started"] is False


class TestDestinationRecord:
    def plan_and_copy(self, base, app, xml_path, catlist_path):
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {"mame_version": "0.252"})
        wait_for(app.job, "done", "error")

    def test_state_has_no_record_before_a_transfer(self, server, romset):
        base, _app = server
        assert get(base, "/api/state")["destination"] is None

    def test_state_reports_the_record_afterwards(self, server, romset, xml_path,
                                                 catlist_path):
        base, app = server
        self.plan_and_copy(base, app, xml_path, catlist_path)
        described = get(base, "/api/state")["destination"]
        assert described["mame_version"] == "0.252"
        assert described["machines"] > 0
        assert described["runs"] == 1

    def test_settings_can_be_imported_back(self, server, romset, xml_path, catlist_path):
        """Point at a destination and get the filters it was built with."""
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_genres": ["Board Game"],
                                 "blacklist_categories": ["Fighter / 2.5D"],
                                 "blacklist_roms": ["nocat"]})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {"mame_version": "0.252"})
        wait_for(app.job, "done", "error")

        imported = post(base, "/api/import", {"copy_path": str(romset["out_dir"])})
        assert imported["mame_version"] == "0.252"
        assert imported["imported"]["blacklist_genres"] == ["Board Game"]
        assert imported["imported"]["blacklist_categories"] == ["Fighter / 2.5D"]
        assert imported["imported"]["blacklist_roms"] == ["nocat"]

    def test_importing_from_a_folder_without_a_record(self, server, tmp_path):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/import", {"copy_path": str(tmp_path)})
        assert error.value.code == 400

    def test_a_second_transfer_appends_to_the_history(self, server, romset, xml_path,
                                                      catlist_path):
        base, app = server
        self.plan_and_copy(base, app, xml_path, catlist_path)
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {"mame_version": "0.253"})
        wait_for(app.job, "done", "error")
        described = get(base, "/api/state")["destination"]
        assert described["mame_version"] == "0.253"
        assert described["runs"] == 2
        assert described["history"][-1]["from"] == "0.252"


class TestChosenVersionDrivesTheFetch:
    def test_a_chosen_version_fetches_both_files(self, server, romset, monkeypatch,
                                                 xml_path, catlist_path):
        from marquee import fetch
        asked = {}

        def fake_xml(version, **_kwargs):
            asked["xml"] = version
            return xml_path

        def fake_catlist(_name, version, **_kwargs):
            asked["catlist"] = version
            return catlist_path

        monkeypatch.setattr(fetch, "fetch_xml", fake_xml)
        monkeypatch.setattr(fetch, "fetch_support_file", fake_catlist)
        base, app = server
        post(base, "/api/plan", {"mame_version": "0.252"})
        assert wait_for(app.job, "planned", "error") == "planned"
        assert asked == {"xml": "0.252", "catlist": "0.252"}


class TestMachinesByCategory:
    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_filter_by_category(self, planned, romset):
        """The tree asks for one category at a time as it is opened."""
        base, _app = planned
        data = get(base, "/api/machines?category=" +
                   urllib.parse.quote("Maze / Misc."))
        assert data["total"] > 0
        assert all(row["category"] == "Maze / Misc." for row in data["rows"])

    def test_category_and_genre_together(self, planned, romset):
        base, _app = planned
        assert get(base, "/api/machines?genre=Maze&category=" +
                   urllib.parse.quote("Fighter / 2.5D"))["total"] == 0

    def test_unknown_category_is_empty(self, planned, romset):
        base, _app = planned
        assert get(base, "/api/machines?category=Nope")["total"] == 0


class TestPlanProvenance:
    """The page compares its form against what the plan was built from."""

    def test_plan_reports_its_inputs(self, server, romset, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "mame_version": "0.252",
                                 "blacklist_genres": ["Casino", "Board Game"],
                                 "blacklist_roms": ["nocat"]})
        wait_for(app.job, "planned", "error")
        built = get(base, "/api/state")["plan"]["built_from"]
        assert built["mame_version"] == "0.252"
        assert built["rom_dir"] == str(romset["rom_dir"])
        assert built["blacklist_genres"] == ["Board Game", "Casino"]   # sorted
        assert built["blacklist_roms"] == ["nocat"]
        assert built["allow_mature"] is False

    def test_lists_are_sorted_so_order_never_looks_like_a_change(self, server, romset,
                                                                 xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_genres": ["Casino", "Board Game"]})
        wait_for(app.job, "planned", "error")
        first = get(base, "/api/state")["plan"]["built_from"]

        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_genres": ["Board Game", "Casino"]})
        wait_for(app.job, "planned", "error")
        assert get(base, "/api/state")["plan"]["built_from"] == first

    def test_a_changed_filter_shows_up(self, server, romset, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        before = get(base, "/api/state")["plan"]["built_from"]

        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_roms": ["goodgame"]})
        wait_for(app.job, "planned", "error")
        assert get(base, "/api/state")["plan"]["built_from"] != before


class TestStaticAssets:
    """The page is several modules now, so they all have to actually be served."""

    def fetch(self, base, path):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.read().decode(), response.headers

    def status(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_the_stylesheet_is_served(self, server):
        base, _app = server
        body, _headers = self.fetch(base, "/static/app.css")
        assert "--accent" in body

    def test_every_module_the_page_imports_is_served(self, server):
        base, _app = server
        page, _headers = self.fetch(base, "/")
        assert "/static/js/app.js" in page
        for name in ("app", "api", "util", "library", "selection", "settings",
                     "activity"):
            body, _headers = self.fetch(base, f"/static/js/{name}.js")
            assert body.strip(), f"{name}.js came back empty"

    def test_javascript_gets_a_javascript_content_type(self, server):
        base, _app = server
        _body, headers = self.fetch(base, "/static/js/app.js")
        assert headers["Content-Type"].startswith("text/javascript")

    def test_a_path_outside_the_static_root_is_refused(self, server):
        base, _app = server
        # Without the realpath check this would hand out whatever it resolved to.
        assert self.status(base, "/static/..%2f..%2fsettings.ini") in (403, 404)

    def test_a_missing_asset_is_a_404(self, server):
        base, _app = server
        assert self.status(base, "/static/js/nope.js") == 404


class TestConfigDirectory:
    """`--config` may name the directory holding settings.ini, as the container mounts.

    Reading resolved it and saving did not, so settings could be read but never
    written -- and the failure reached the browser as "NetworkError" because the
    handler died without answering.
    """

    @pytest.fixture
    def by_directory(self, tmp_path, config, romset):
        settings = tmp_path / "settings.ini"
        config.settings_path = str(settings)
        configuration.write_settings_file(str(settings), config)
        app = Application(str(tmp_path))          # the directory, not the file
        handler = type("Bound", (Handler,), {"app": app})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
        httpd.shutdown()
        httpd.server_close()

    def test_the_directory_resolves_to_its_settings_file(self, tmp_path):
        app = Application(str(tmp_path))
        assert app.settings_path == str(tmp_path / "settings.ini")

    def test_settings_can_be_saved(self, by_directory):
        base, directory = by_directory
        answer = post(base, "/api/save", {"mature_rom_folder": "Grown-Ups"})
        assert answer.get("saved") or answer.get("ok") or answer
        assert "Grown-Ups" in (directory / "settings.ini").read_text()

    def test_what_was_saved_is_read_back(self, by_directory):
        base, _directory = by_directory
        post(base, "/api/save", {"mature_rom_folder": "Grown-Ups"})
        assert get(base, "/api/state")["config"]["mature_rom_folder"] == "Grown-Ups"

    def test_the_api_key_lands_beside_the_settings(self, tmp_path):
        from marquee.web.server import api_key
        key = api_key(str(tmp_path))
        assert (tmp_path / "api_key").read_text().strip() == key


class TestUnexpectedErrors:
    def test_an_unexpected_failure_answers_rather_than_dropping_the_connection(
            self, server, monkeypatch):
        base, app = server

        def explode(_body):
            raise RuntimeError("the disk caught fire")

        monkeypatch.setattr(app, "save", explode)
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/save", {})
        assert error.value.code == 500
        # The browser only ever says "NetworkError"; the message has to come from here.
        assert "the disk caught fire" in json.loads(error.value.read())["error"]


class TestBrowseShape:
    """The picker showed nothing at all because the server and the page disagreed.

    The server answered `directories` (bare names) and the page read `entries`
    (objects with a full path), so every folder looked empty.
    """

    def test_entries_carry_the_full_path(self, server, romset):
        base, _app = server
        payload = get(base, "/api/browse?path=" + str(romset["chd_dir"]))
        assert payload["entries"], "the picker reads entries, not directories"
        for entry in payload["entries"]:
            assert entry["path"].endswith(entry["name"])
            assert entry["path"].startswith(str(romset["chd_dir"]))

    def test_the_two_shapes_agree(self, server, romset):
        base, _app = server
        payload = get(base, "/api/browse?path=" + str(romset["chd_dir"]))
        assert [entry["name"] for entry in payload["entries"]] == payload["directories"]

    def test_roots_are_offered_to_start_from(self, server):
        base, _app = server
        # In a container there is no home directory to fall back on.
        assert "/" in get(base, "/api/browse?path=/")["roots"]

    def test_an_empty_folder_still_answers(self, server, tmp_path):
        base, _app = server
        empty = tmp_path / "nothing-in-here"
        empty.mkdir()
        payload = get(base, "/api/browse?path=" + str(empty))
        assert payload["entries"] == []
        assert payload["path"] == str(empty)


class TestMissingDestination:
    """A library folder that is not there has to be said out loud.

    It plans happily, reports nothing already present, and then writes hundreds of
    gigabytes somewhere nobody meant -- most likely the container's own filesystem,
    because the path exists on the host and not inside the container.
    """

    def plan_with(self, base, app, copy_path, xml_path, catlist_path):
        post(base, "/api/save", {"copy_path": copy_path})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        for _ in range(100):
            if app.job.state in ("planned", "error"):
                break
            time.sleep(0.05)
        return get(base, "/api/state")

    def test_an_existing_folder_is_not_flagged(self, server, romset, xml_path,
                                               catlist_path):
        base, app = server
        payload = self.plan_with(base, app, str(romset["out_dir"]), xml_path, catlist_path)
        assert payload["resolution"]["destination_exists"] is True

    def test_a_folder_that_is_not_there_is_flagged(self, server, tmp_path, xml_path,
                                                   catlist_path):
        base, app = server
        payload = self.plan_with(base, app, str(tmp_path / "nowhere"), xml_path,
                                 catlist_path)
        assert payload["resolution"]["destination_exists"] is False

    def test_it_warns_rather_than_refusing(self, server, tmp_path, xml_path,
                                           catlist_path):
        base, app = server
        # Creating a new library is legitimate; this must not stop the run.
        payload = self.plan_with(base, app, str(tmp_path / "nowhere"), xml_path,
                                 catlist_path)
        assert payload["state"] == "planned"

    def test_a_remote_destination_is_not_guessed_at(self):
        from marquee.pipeline import _destination_exists
        # Only a connection attempt could tell whether an SMB share is there, and that
        # happens later; guessing "missing" would warn on every remote library.
        for url in ("smb://host/share/roms", "ftp://host/roms", "sftp://host/roms"):
            assert _destination_exists(url) is True
        assert _destination_exists("") is False




class TestSelectionMatches:
    """Acting on a filter means acting on all of it, not on the page being shown.

    The tree draws 500 rows; a filter can cover thousands of games, and "leave all of
    these out" has to mean all of them.
    """

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        return base, app

    def test_it_names_every_match_not_just_a_page(self, planned):
        base, _app = planned
        answer = get(base, "/api/selection")
        assert answer["total"] == len(answer["machines"])
        assert answer["total"] == get(base, "/api/machines?limit=500")["total"]

    def test_each_one_carries_where_it_sits_and_what_it_weighs(self, planned):
        """Unticking a game has to know its category to leave its siblings alone, and
        its size for the running total to move when it goes."""
        base, _app = planned
        rows = {m["name"]: m for m in get(base, "/api/selection")["machines"]}
        entry = rows["goodgame"]
        assert set(entry) == {"name", "genre", "category", "bytes", "library"}
        assert entry["genre"] and entry["category"]
        assert entry["bytes"] == 300
        # Not transferred yet, so unticking it would delete nothing.
        assert entry["library"] is False

    def test_a_game_in_the_library_says_so(self, planned, xml_path, catlist_path):
        """Unticking one of these is a deletion waiting to happen; the tree has to
        be able to say so before the Transfer page does."""
        base, app = planned
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        rows = {m["name"]: m for m in get(base, "/api/selection")["machines"]}
        assert rows["goodgame"]["library"] is True

    def test_a_category_comes_back_whole(self, planned):
        """The tick rules are decided on this, so a page of it is worse than useless."""
        base, _app = planned
        every = get(base, "/api/selection")["machines"]
        category = every[0]["category"]
        just_it = get(base, "/api/selection?category=" + urllib.parse.quote(category))
        assert just_it["total"] == sum(1 for m in every if m["category"] == category)
        assert {m["name"] for m in just_it["machines"]} == \
            {m["name"] for m in every if m["category"] == category}

    def test_the_same_filters_narrow_it(self, planned):
        base, _app = planned
        everything = get(base, "/api/selection")["total"]
        flawed = get(base, "/api/selection?condition=flawed")
        assert 0 < flawed["total"] < everything
        assert "impgame" in {m["name"] for m in flawed["machines"]}

    def test_without_a_plan_it_is_empty_rather_than_an_error(self, server):
        base, _app = server
        assert get(base, "/api/selection") == {"total": 0, "machines": []}


class TestStartsWithAPlan:
    """A restart must not leave every page empty.

    This is a service. It is restarted for an image update or when the host comes
    back, and until something is planned the library, the selection tree and the
    wanted list all show nothing at all.
    """

    def test_it_plans_from_the_saved_settings(self, tmp_path, config, romset,
                                              xml_path, catlist_path):
        config.settings_path = str(tmp_path / "settings.ini")
        config.mame_xml, config.catlist_ini = xml_path, catlist_path
        configuration.write_settings_file(config.settings_path, config)
        app = Application(config.settings_path)
        assert app.autoplan()["started"] is True
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        assert app.job.plan.items

    def test_an_incomplete_first_run_is_left_alone(self, tmp_path):
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, configuration.Config())
        app = Application(path)
        answer = app.autoplan()
        assert answer["started"] is False
        assert "not complete" in answer["reason"]
        assert app.job.state == "idle"

    def test_no_settings_at_all_is_not_an_error(self, tmp_path):
        app = Application(str(tmp_path / "nothing.ini"))
        assert app.autoplan()["started"] is False
        assert app.job.state == "idle"


class TestASaveCannotReportWhatItDoesNotKnow:
    """The page sends a whole configuration on every save, and an empty form and an
    unloaded one look identical from the server's side.

    This cost a real user their download client, its credentials and 3,235
    individually excluded games in one click.
    """

    def test_a_body_that_says_nothing_changes_nothing(self, server, config, tmp_path):
        base, app = server
        post(base, "/api/save", {"blacklist_roms": ["a", "b"],
                                 "download_client": "http://host:8080/"})
        post(base, "/api/save", {})
        saved = app.current_config()
        assert saved.blacklist_roms == ["a", "b"]
        assert saved.download_client == "http://host:8080/"

    def test_a_blacklist_that_is_not_a_list_is_ignored(self, server, config):
        """The page omits them until it has read the saved ones; anything else is a
        bug somewhere, not an instruction to throw them away."""
        base, app = server
        post(base, "/api/save", {"blacklist_roms": ["a", "b"]})
        for nonsense in (None, "", 0, "a,b"):
            post(base, "/api/save", {"blacklist_roms": nonsense})
            assert app.current_config().blacklist_roms == ["a", "b"]

    def test_an_explicit_empty_list_still_clears(self, server, config):
        base, app = server
        post(base, "/api/save", {"blacklist_roms": ["a"]})
        post(base, "/api/save", {"blacklist_roms": []})
        assert app.current_config().blacklist_roms == []


class TestOneJobAtATime:
    def test_a_second_plan_does_not_blank_the_first(self, server, xml_path,
                                                    catlist_path, romset):
        """start_plan used to reset the job and only then refuse, so pressing Build
        plan twice emptied the page while the first run was still going."""
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        try:
            app.job.start_plan(app.current_config(), SourceOptions())
        except MarqueeError as error:
            assert "already running" in str(error)
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error

    def test_copying_with_nothing_planned_is_refused_not_a_crash(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/copy", {})
        assert error.value.code == 400
        assert "plan" in json.loads(error.value.read())["error"].lower()


class TestBadQueryParameters:
    """A mistyped number in a URL is not a server fault and must not read like one."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_a_word_where_a_number_belongs(self, planned):
        base, _app = planned
        assert get(base, "/api/machines?offset=abc&limit=2")["offset"] == 0
        assert len(get(base, "/api/machines?limit=nonsense")["rows"]) > 0
        assert get(base, "/api/state?after=abc")["state"]

    def test_a_negative_offset_does_not_wrap_to_the_end(self, planned):
        base, _app = planned
        first = get(base, "/api/machines?offset=0&limit=3&sort=name&dir=asc")
        wrapped = get(base, "/api/machines?offset=-3&limit=3&sort=name&dir=asc")
        # Python slices from the end on a negative start; that answered with the last
        # three rows and called them page one.
        assert wrapped["offset"] == 0
        assert wrapped["rows"] == first["rows"]


class TestWhereTheRestWent:
    """4,323 of 0.289's 16,350 machines never reach the library. Showing only the two
    totals leaves that as a question the app never answers."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_the_reasons_are_reported(self, planned):
        base, _app = planned
        rows = get(base, "/api/state")["resolution"]["filtered"]
        assert rows, "nothing explains the machines that were dropped"
        by_reason = {row["reason"]: row for row in rows}
        # The fixture set has a preliminary driver and a prototype in it.
        assert by_reason["not-working"]["count"] >= 1
        assert by_reason["proto-beta"]["count"] >= 1
        assert all(row["label"] for row in rows)

    def test_they_add_up(self, planned):
        base, _app = planned
        resolution = get(base, "/api/state")["resolution"]
        dropped = sum(row["count"] for row in resolution["filtered"])
        assert resolution["machines_read"] - dropped == resolution["machines_kept"]

    def test_a_reason_that_caught_nothing_is_not_listed(self, planned):
        base, _app = planned
        rows = get(base, "/api/state")["resolution"]["filtered"]
        assert all(row["count"] > 0 for row in rows)


class TestLeftOut:
    """Everything that did not make it, as a catalogue of its own.

    Excluding a game by name used to be a one-way door: it was dropped before anything
    else ran, so it appeared in no list, no search and no tree, and the only way back
    was editing settings.ini by hand.
    """

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/save", {"blacklist_roms": ["goodgame"]})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        return base, app

    def test_it_is_a_catalogue_with_a_tree_of_its_own(self, planned):
        base, _app = planned
        payload = get(base, "/api/left-out")
        assert payload["total"] > 0
        assert payload["genres"] and payload["categories"]
        assert any(entry["reason"] == "blacklisted" for entry in payload["reasons"])

    def test_a_machine_you_excluded_is_reachable_again(self, planned):
        base, _app = planned
        rows = get(base, "/api/machines?set=left-out&limit=500")["rows"]
        found = {row["name"]: row for row in rows}
        assert "goodgame" in found
        assert found["goodgame"]["reason"] == "blacklisted"
        # And it carries enough to be recognised, not just a name.
        assert found["goodgame"]["description"]
        assert found["goodgame"]["category"]

    def test_the_library_listing_is_untouched(self, planned):
        base, _app = planned
        assert "goodgame" not in {
            row["name"] for row in get(base, "/api/machines?limit=500")["rows"]}

    def test_it_can_be_narrowed_by_reason(self, planned):
        base, _app = planned
        mine = get(base, "/api/machines?set=left-out&reason=blacklisted&limit=500")
        assert {row["name"] for row in mine["rows"]} == {"goodgame"}
        rules = get(base, "/api/machines?set=left-out&reason=not-working&limit=500")
        assert "goodgame" not in {row["name"] for row in rules["rows"]}

    def test_putting_one_back_takes_it_off_the_blacklist(self, planned):
        base, app = planned
        answer = post(base, "/api/left-out/restore", {"machines": ["goodgame"]})
        assert answer["restored"] == 1
        assert "goodgame" not in app.current_config().blacklist_roms

    def test_what_the_filters_left_out_cannot_be_put_back_here(self, planned):
        base, _app = planned
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/left-out/restore", {"machines": ["deadgame"]})
        assert error.value.code == 400
        assert "working filters" in json.loads(error.value.read())["error"]

    def test_the_tree_endpoint_serves_it_too(self, planned):
        base, _app = planned
        answer = get(base, "/api/selection?set=left-out")
        assert answer["total"] > 0
        assert "goodgame" in {m["name"] for m in answer["machines"]}

    def test_without_a_plan_it_is_empty_rather_than_an_error(self, server):
        base, _app = server
        assert get(base, "/api/left-out")["total"] == 0
        assert get(base, "/api/machines?set=left-out")["rows"] == []


class TestLeftOutFilters:
    """A dropdown that narrows the rows inside a category but leaves every count above
    it reading the unfiltered total is worse than no filter at all."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/save", {"blacklist_roms": ["goodgame"]})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_the_tree_narrows_with_the_reason(self, planned):
        base, _app = planned
        every = get(base, "/api/left-out")
        mine = get(base, "/api/left-out?reason=blacklisted")
        assert mine["total"] < every["total"]
        assert mine["total"] == sum(entry["wanted"] for entry in mine["genres"])
        assert mine["total"] == sum(entry["wanted"] for entry in mine["categories"])

    def test_the_counts_add_up_to_the_rows_that_are_there(self, planned):
        base, _app = planned
        payload = get(base, "/api/left-out?reason=not-working")
        for entry in payload["categories"]:
            rows = get(base, "/api/machines?set=left-out&reason=not-working"
                       + "&limit=500&category=" + urllib.parse.quote(entry["name"]))
            assert rows["total"] == entry["wanted"], entry["name"]

    def test_the_tally_always_describes_the_whole_set(self, planned):
        """So the dropdown can say what picking each one would give you."""
        base, _app = planned
        every = {entry["reason"]: entry["count"]
                 for entry in get(base, "/api/left-out")["reasons"]}
        narrowed = {entry["reason"]: entry["count"]
                    for entry in get(base, "/api/left-out?reason=blacklisted")["reasons"]}
        assert every == narrowed

    def test_a_search_narrows_it_too(self, planned):
        base, _app = planned
        found = get(base, "/api/left-out?q=goodgame")
        assert found["total"] == 1
        assert len(found["categories"]) == 1


class TestWhereAnExcludeListCameFrom:
    """One press of "leave all out" on a filtered view writes one name per game, and
    the list that comes out carries no memory of the rule that made it. The page has
    to be able to hand that recognition back."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/save", {"blacklist_roms": ["impgame", "goodgame"]})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_it_says_how_many_of_them_share_a_condition(self, planned):
        base, _app = planned
        payload = get(base, "/api/left-out")
        # impgame runs with imperfect sound and unemulated graphics; goodgame does not.
        assert payload["imperfect_exclusions"] == 1

    def test_the_exclude_list_can_be_narrowed_to_them(self, planned):
        base, _app = planned
        flawed = get(base, "/api/left-out?reason=blacklisted&condition=flawed")
        assert flawed["total"] == 1
        rows = get(base, "/api/machines?set=left-out&reason=blacklisted"
                   "&condition=flawed&limit=500")["rows"]
        assert [row["name"] for row in rows] == ["impgame"]

    def test_and_put_back_as_a_group(self, planned):
        base, app = planned
        answer = post(base, "/api/left-out/restore",
                      {"filters": {"reason": "blacklisted", "condition": "flawed"}})
        assert answer["restored"] == 1
        left = app.current_config().blacklist_roms
        assert left == ["goodgame"], "it put back more than the group it was asked for"


class TestCheckingTheLibrary:
    """"We have it" from the filename alone says nothing. MAME replaces bad dumps and
    renames chips between releases; the only honest answer is to look inside."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        return base, app

    def test_it_reports_what_it_found(self, planned, romset, xml_path, catlist_path):
        base, app = planned
        # Nothing has been transferred: there is nothing in the library to look at,
        # and saying so beats reporting every game in the source folder as "absent"
        # -- which is what used to happen, and then hid behind every check filter.
        post(base, "/api/check", {})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        assert not app.job.checked
        assert any("nothing to check" in event["text"].lower()
                   for event in app.job.snapshot(0)["events"])

        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/check", {})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        # The fixture set has no <rom> entries, so nothing can be called wrong -- but
        # every machine in the library is looked at and answered for.
        assert app.job.checked
        assert sum(app.job.checked.values()) > 0
        assert "absent" not in app.job.checked
        assert get(base, "/api/state")["plan"]["states"]

    def test_a_stale_file_becomes_something_to_fetch(self, planned, romset):
        base, app = planned
        from marquee import verify
        item = app.job.plan.items[0]
        before = len(app.job.plan.needed)
        item.in_library, item.rom_source, item.state = True, None, verify.STALE
        assert item.name in app.job.plan.needed
        assert len(app.job.plan.needed) == before + 1

    def test_the_library_can_be_narrowed_to_what_is_wrong(self, planned):
        base, app = planned
        from marquee import verify
        app.job.plan.items[0].state = verify.STALE
        rows = get(base, "/api/machines?state=stale&limit=500")["rows"]
        assert [row["name"] for row in rows] == [app.job.plan.items[0].name]
        assert rows[0]["state"] == "stale"

    def test_finding_something_wrong_takes_the_diff_again(self, planned, romset,
                                                         monkeypatch, xml_path,
                                                         catlist_path):
        """Otherwise the page says "out of date" and the transfer does nothing.

        The diff is worked out from names and sizes. The check reads what is inside,
        and a redump that weighs the same as the dump it replaces is invisible to a
        size comparison -- so the diff has to be taken again with what the check found.
        """
        base, app = planned
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        before = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        assert before["keep"]["files"] > 0
        assert before["update"]["files"] == 0

        # The fixture romset holds no real archives, so a machine the release has a
        # ROM list for is one the check cannot read: damaged, which is a replacement.
        from marquee import sources
        monkeypatch.setattr(sources, "rom_manifests",
                            lambda *args, **kwargs: {
                                "goodgame": [("goodgame.1", "deadbeef", False)]})
        post(base, "/api/check", {})
        wait_for(app.job, "planned", "error")

        after = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        assert after["update"]["files"] == 1
        rows = get(base, "/api/changes?kind=update")["rows"]
        assert [row["name"] for row in rows] == ["goodgame"]

    def test_checking_without_a_plan_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/check", {})
        assert error.value.code == 400

    def test_one_job_at_a_time_still_holds(self, planned):
        base, app = planned
        app.job.state = "copying"
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/check", {})
        assert error.value.code == 400
        app.job.state = "planned"




class TestAGameOutsideTheLibraryStillHasDetails:
    """Clicking a game unticked on Selection answered "No machine named 'pgs268'".
    It is still in the release; the panel says what the XML says, and why it is out."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        return base, app

    def test_an_excluded_game(self, planned):
        base, app = planned
        excluded = app.job.plan.excluded_items
        assert excluded, "the fixture excludes the Board Game genre"
        detail = get(base, f"/api/machine?name={excluded[0].name}")
        assert detail["left_out"] is True
        assert detail["description"] == excluded[0].description
        assert detail["reason_label"]

    def test_a_game_a_filter_dropped(self, planned):
        base, app = planned
        dropped = app.job.plan.left_out.wanted
        assert dropped, "the fixture has machines the filters leave out"
        item = dropped[0]
        detail = get(base, f"/api/machine?name={item.name}")
        assert detail["left_out"] is True
        assert detail["reason"] == item.reason
        assert detail["reason_label"] and detail["reason_label"] != item.reason

    def test_a_name_in_no_list_is_still_unknown(self, planned):
        base, _app = planned
        with pytest.raises(urllib.error.HTTPError) as error:
            get(base, "/api/machine?name=nosuchgame")
        assert error.value.code == 404

class TestACheckIsKept:
    """A check opens every file -- ten minutes over a share -- and its answer used to
    go with the plan it was made on. The next "Build plan", or a container restart,
    and the Overview said nothing had ever looked inside the files."""

    @pytest.fixture
    def checked(self, server, xml_path, catlist_path, romset):
        base, app = server
        for path, payload, until in (("/api/plan", {"xml": xml_path, "catlist": catlist_path}, "planned"),
                                     ("/api/copy", {}, "done"),
                                     ("/api/plan", {"xml": xml_path, "catlist": catlist_path}, "planned"),
                                     ("/api/check", {}, "planned")):
            post(base, path, payload)
            wait_for(app.job, until, "error")
            assert app.job.state == until, app.job.error
        assert app.job.checked
        return base, app

    def replan(self, base, app, xml_path, catlist_path):
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error

    def test_a_new_plan_keeps_it(self, checked, xml_path, catlist_path):
        base, app = checked
        before = dict(app.job.checked)
        self.replan(base, app, xml_path, catlist_path)
        assert app.job.checked == before
        plan = get(base, "/api/state")["plan"]
        assert plan["states"] and plan["checked_at"]

    def test_so_does_a_restart(self, checked, xml_path, catlist_path):
        base, app = checked
        before = dict(app.job.checked)
        app.job.reset()
        assert app.job.checked is None
        self.replan(base, app, xml_path, catlist_path)
        assert app.job.checked == before

    def test_a_file_that_changed_is_not_vouched_for(self, checked, romset,
                                                   xml_path, catlist_path):
        base, app = checked
        name = next(item.name for item in app.job.plan.items if item.state)
        folder = next(item.folder for item in app.job.plan.items if item.name == name)
        # Changed in the source as well, so the library copy is the one planned to stay.
        with open(os.path.join(romset["out_dir"], folder, f"{name}.zip"), "ab") as handle:
            handle.write(b"more")
        with open(os.path.join(romset["rom_dir"], f"{name}.zip"), "ab") as handle:
            handle.write(b"more")
        self.replan(base, app, xml_path, catlist_path)
        item = next(item for item in app.job.plan.items if item.name == name)
        assert not item.state
        assert any(other.state for other in app.job.plan.items if other.name != name)

    def test_another_release_does_not_inherit_it(self, checked):
        _base, app = checked
        from marquee.web import checks
        assert checks.restore(app.job.plan, app.job.config.copy_path, "0.001") == (None, None)
        assert checks.restore(app.job.plan, "/elsewhere", app.job.resolution.xml_version) \
            == (None, None)

    def test_what_a_transfer_wrote_is_forgotten(self, checked, monkeypatch,
                                                xml_path, catlist_path):
        """A same-size redump replacing a stale zip must not inherit its verdict, or
        it is replaced again on every run for ever."""
        base, app = checked
        from marquee import sources
        monkeypatch.setattr(sources, "rom_manifests",
                            lambda *args, **kwargs: {
                                "goodgame": [("goodgame.1", "deadbeef", False)]})
        post(base, "/api/check", {})
        wait_for(app.job, "planned", "error")
        self.replan(base, app, xml_path, catlist_path)
        kinds = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        assert kinds["update"]["files"] == 1, "a kept stale verdict still means replace"

        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        self.replan(base, app, xml_path, catlist_path)
        kinds = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        assert kinds["update"]["files"] == 0
        goodgame = next(item for item in app.job.plan.items if item.name == "goodgame")
        assert not goodgame.state


class TestALibraryGameHasASize:
    """The Library read "sizes unknown until the release is read" over ten thousand
    games whose files were sitting right there: a game was weighed by the torrent
    folder or the release, never by the library."""

    def test_its_files_in_the_library_weigh_it(self, server, xml_path, catlist_path,
                                               romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        for name in os.listdir(romset["rom_dir"]):
            os.remove(os.path.join(romset["rom_dir"], name))
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        found = get(base, "/api/machines?limit=500")
        assert found["bytes"] > 0
        goodgame = next(row for row in found["rows"] if row["name"] == "goodgame")
        assert goodgame["bytes"] == 300
        assert get(base, "/api/state")["plan"]["wanted_bytes"] > 0

class TestAFilterIsNotAnEmptyCatalogue:
    """Searching Left out for something it does not have wiped the page -- the filter
    bar with it, so there was no way back to what you had typed. The page can only
    tell "nothing was left out" from "nothing matches that" if the tally it is given
    covers everything rather than the filtered view.
    """

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_a_search_with_no_matches_still_reports_the_whole_tally(self, planned):
        base, _app = planned
        payload = get(base, "/api/left-out?q=zzzznothinglikethis")
        assert payload["total"] == 0
        assert payload["reasons"], "the page cannot tell a filter from an empty catalogue"
        assert sum(entry["count"] for entry in payload["reasons"]) > 0

    def test_the_tally_does_not_move_when_a_filter_does(self, planned):
        base, _app = planned
        everything = get(base, "/api/left-out")
        narrowed = get(base, "/api/left-out?reason=not-working")
        assert narrowed["total"] < everything["total"]
        assert narrowed["reasons"] == everything["reasons"]


class TestWhatTheNextRunWillDo:
    """The difference between the library and the selection, before anything runs.

    All of it has been worked out since the first version -- the sync report is what
    the copy runs from -- and none of it was ever on screen. A run that copies,
    replaces, renames and deletes should be readable beforehand, not afterwards in a
    log.
    """

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_nothing_to_say_before_a_plan(self, server):
        base, _app = server
        payload = get(base, "/api/changes")
        assert payload["compared"] is False
        assert payload["rows"] == []

    def test_every_kind_is_accounted_for(self, planned):
        base, _app = planned
        kinds = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        assert set(kinds) == {"new", "update", "move", "keep", "orphan", "fetch"}
        assert all(entry["label"] and entry["note"] for entry in kinds.values())

    def test_an_empty_library_is_all_arrivals(self, planned):
        base, app = planned
        payload = get(base, "/api/changes")
        kinds = {entry["kind"]: entry for entry in payload["kinds"]}
        assert kinds["new"]["files"] == app.job.plan.file_count
        assert kinds["new"]["machines"] == len(app.job.plan.items)
        assert payload["to_transfer_human"]

    def test_the_arrivals_are_named(self, planned):
        base, _app = planned
        rows = get(base, "/api/changes?kind=new")["rows"]
        by_name = {row["name"]: row for row in rows}
        assert "goodgame" in by_name
        assert by_name["goodgame"]["description"]
        assert by_name["goodgame"]["bytes_human"]

    def test_a_machine_is_one_row_however_many_files_it_has(self, planned):
        base, _app = planned
        rows = {row["name"]: row for row in get(base, "/api/changes?kind=new")["rows"]}
        assert rows["twodisk"]["files"] == 2
        assert len(rows["twodisk"]["paths"]) == 2

    def test_a_second_look_is_all_leave_alone(self, planned, xml_path, catlist_path):
        base, app = planned
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        kinds = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        assert kinds["new"]["files"] == 0
        assert kinds["keep"]["files"] > 0

    def test_deletions_are_listed_as_files_with_a_reason(self, planned, romset,
                                                         xml_path, catlist_path):
        base, app = planned
        stray = romset["out_dir"] / "Maze" / "Misc"
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "blockedgame.zip").write_bytes(b"old" * 10)
        (stray / "neverheardof.zip").write_bytes(b"old" * 10)
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        payload = get(base, "/api/changes?kind=orphan")
        rows = {row["path"]: row for row in payload["rows"]}
        assert "Maze/Misc/blockedgame.zip" in rows
        # It is on the exclude list, and saying so is the difference between a list
        # anyone can agree to delete and four thousand unexplained paths.
        assert rows["Maze/Misc/blockedgame.zip"]["why"] == "you left it out"
        assert rows["Maze/Misc/blockedgame.zip"]["description"] == "Blocked Game"
        assert rows["Maze/Misc/neverheardof.zip"]["why"] == "not in MAME 0.252"

    def test_a_stray_disk_is_never_labelled_as_the_game_whose_folder_it_is_in(
            self, planned, romset, xml_path, catlist_path):
        """The one that made a correct deletion look like a mistake.

        A dropped clone keeps its disk in its parent's folder, so falling back to the
        folder's game put `CarnEvil (v1.0.3)` -- the game being kept -- on the row
        proposing to delete 1.4 GB of a clone MAME no longer has. Whose folder it is
        in is context; it is not what the file is.
        """
        base, app = planned
        holder = romset["out_dir"] / "Maze" / "Misc" / "parentchd"
        holder.mkdir(parents=True, exist_ok=True)
        (holder / "pdisk_v1.chd").write_bytes(b"a dropped clone's disk")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        rows = {row["path"]: row for row in get(base, "/api/changes?kind=orphan")["rows"]}
        mine = rows["Maze/Misc/parentchd/pdisk_v1.chd"]
        assert mine["description"] != "Parent CHD", "it is not that game"
        assert "Parent CHD" in mine["why"], "whose folder it is in is worth saying"
        assert "no machine in MAME 0.252 uses this disk" in mine["why"]

    def test_a_disk_belonging_to_a_left_out_game_names_that_game(self, planned, romset,
                                                                 xml_path, catlist_path):
        """The release does list it -- for a machine you are not keeping."""
        base, app = planned
        post(base, "/api/save", {"blacklist_roms": ["cloneown"]})
        holder = romset["out_dir"] / "Fighter" / "25D" / "cloneown"
        holder.mkdir(parents=True, exist_ok=True)
        (holder / "odisk.chd").write_bytes(b"its own disk")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        rows = {row["path"]: row for row in get(base, "/api/changes?kind=orphan")["rows"]}
        mine = next(row for path, row in rows.items() if path.endswith("odisk.chd"))
        assert mine["why"] == "a disk for cloneown, which is left out"

    def test_a_deletion_that_is_really_a_replacement_says_so(self, planned, romset,
                                                            xml_path, catlist_path):
        """The same game, somewhere the release no longer files it."""
        base, app = planned
        stray = romset["out_dir"] / "Oldgenre" / "Misc"
        stray.mkdir(parents=True, exist_ok=True)
        (stray / "goodgame.zip").write_bytes(b"different length entirely" * 3)
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        rows = {row["path"]: row for row in get(base, "/api/changes?kind=orphan")["rows"]}
        assert "Maze/Misc" in rows["Oldgenre/Misc/goodgame.zip"]["why"]

    def test_searching_narrows_the_rows(self, planned):
        base, _app = planned
        payload = get(base, "/api/changes?kind=new&q=goodgame")
        assert payload["total"] == 1
        assert payload["rows"][0]["name"] == "goodgame"

    def test_a_kind_nobody_asked_for_is_not_a_crash(self, planned):
        base, _app = planned
        payload = get(base, "/api/changes?kind=nonsense")
        assert payload["kind"] == ""
        assert payload["rows"] == []

    def test_a_bad_offset_does_not_wrap_around(self, planned):
        base, _app = planned
        payload = get(base, "/api/changes?kind=new&offset=-5")
        assert payload["offset"] == 0
        assert payload["rows"]

    def test_it_says_whether_anything_has_read_the_files(self, planned):
        base, _app = planned
        assert get(base, "/api/changes")["checked"] is False

    def test_what_is_selected_but_not_downloaded_is_counted(self, planned):
        base, app = planned
        assert get(base, "/api/changes")["waiting"] == len(app.job.plan.needed)


class TestTheSidebarCountsTheLibrary:
    """"Games 0 · Library 0 B" over ten thousand games.

    `machines` is what is in the torrent folder, which on a library built before
    Marquee ever ran is nothing at all -- and that is the figure the sidebar showed.
    """

    def test_it_reports_what_is_at_the_destination(self, server, xml_path,
                                                   catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        plan = get(base, "/api/state")["plan"]
        assert plan["library_machines"] == len(app.job.plan.items)
        assert plan["library_bytes_human"] == plan["bytes_human"]

    def test_it_says_nothing_before_the_destination_has_been_looked_at(self, server):
        base, _app = server
        assert get(base, "/api/state")["plan"] is None


class TestOneMachineTellsOneStory:
    """The grid said `keep`, the panel behind it said `missing`, and the file list
    under both said nothing was on disk. All three were about the same game.

    `machine_detail` decided from the source folder alone, which on a library built
    from an older romset is empty by definition.
    """

    @pytest.fixture
    def upgraded(self, server, xml_path, catlist_path, romset, tmp_path):
        """A library that holds the games, and a download folder that holds nothing."""
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        empty = tmp_path / "nothing-downloaded"
        empty.mkdir()
        post(base, "/api/save", {"rom_dir": str(empty), "chd_dir": str(empty)})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_the_panel_agrees_with_the_row(self, upgraded):
        base, _app = upgraded
        row = next(r for r in get(base, "/api/machines?limit=500")["rows"]
                   if r["name"] == "goodgame")
        detail = get(base, "/api/machine?name=goodgame")
        assert row["status"] == "keep"
        assert detail["status"] == row["status"]

    def test_it_says_where_the_files_actually_are(self, upgraded):
        base, _app = upgraded
        detail = get(base, "/api/machine?name=goodgame")
        assert detail["files"] == [], "nothing is downloaded, so nothing is ready to copy"
        assert [f["destination"] for f in detail["library_files"]] \
            == ["Maze/Misc/goodgame.zip"]
        assert detail["library_files"][0]["bytes"] > 0

    def test_a_game_that_is_nowhere_still_says_so(self, upgraded):
        base, app = upgraded
        absent = next(item for item in app.job.plan.wanted if not item.in_library)
        detail = get(base, f"/api/machine?name={absent.name}")
        assert detail["status"] == "missing"
        assert detail["library_files"] == []

    def test_a_file_on_its_way_somewhere_else_says_where(self, upgraded, romset,
                                                         xml_path, catlist_path):
        base, app = upgraded
        moved = romset["out_dir"] / "Oldgenre" / "Misc"
        moved.mkdir(parents=True, exist_ok=True)
        (romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip").rename(
            moved / "goodgame.zip")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        detail = get(base, "/api/machine?name=goodgame")
        here = detail["library_files"][0]
        assert here["destination"] == "Oldgenre/Misc/goodgame.zip"
        assert here["moving_to"] == "Maze/Misc/goodgame.zip"


class TestASecondCopyIsNamedAsOne:
    """A library that has been recategorised more than once holds the same disk in
    two or three old folders. One is claimed and the rest are duplicates -- and
    calling them "not in this release" made the list read as nonsense, because the
    file plainly was.
    """

    @pytest.fixture
    def twice(self, server, xml_path, catlist_path, romset):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")

        # The same disk, again, under a category this release no longer uses.
        real = romset["out_dir"] / "Maze" / "Misc" / "twodisk" / "ok.chd"
        stale = romset["out_dir"] / "Oldgenre" / "Misc" / "twodisk"
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "ok.chd").write_bytes(real.read_bytes())
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_it_says_where_the_kept_one_is(self, twice):
        base, _app = twice
        rows = {row["path"]: row for row in get(base, "/api/changes?kind=orphan")["rows"]}
        mine = rows["Oldgenre/Misc/twodisk/ok.chd"]
        assert mine["why"] == "a second copy; kept as Maze/Misc/twodisk/ok.chd"

    def test_the_kept_one_is_not_in_the_delete_list(self, twice):
        base, _app = twice
        paths = {row["path"] for row in get(base, "/api/changes?kind=orphan")["rows"]}
        assert "Maze/Misc/twodisk/ok.chd" not in paths

    def test_a_different_file_of_the_same_name_is_not_called_a_copy(self, twice,
                                                                    romset, xml_path,
                                                                    catlist_path):
        """Two machines can name a disk the same thing; the size has to agree."""
        base, app = twice
        other = romset["out_dir"] / "Elsewhere" / "Misc" / "twodisk"
        other.mkdir(parents=True, exist_ok=True)
        (other / "ok.chd").write_bytes(b"a different disk entirely, of another length")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")

        rows = {row["path"]: row for row in get(base, "/api/changes?kind=orphan")["rows"]}
        assert "second copy" not in rows["Elsewhere/Misc/twodisk/ok.chd"]["why"]


class TestTheLibraryAgreesWithTheTransferPage:
    """944 games the next run was going to relocate read `keep` on the library page,
    and its `move` filter was permanently empty.

    machine_status() walked plan.items and then stamped every in-library machine as
    KEEP, which is blind to a relocation -- and a relocation is most of what an
    upgrade does.
    """

    @pytest.fixture
    def moved(self, server, xml_path, catlist_path, romset, tmp_path):
        """A library holding the games under a folder this release no longer uses."""
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")

        old = romset["out_dir"] / "Oldgenre" / "Misc"
        old.mkdir(parents=True, exist_ok=True)
        (romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip").rename(old / "goodgame.zip")
        empty = tmp_path / "nothing-downloaded"
        empty.mkdir()
        post(base, "/api/save", {"rom_dir": str(empty), "chd_dir": str(empty)})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_a_relocation_is_not_reported_as_leave_alone(self, moved):
        base, _app = moved
        row = next(r for r in get(base, "/api/machines?limit=500")["rows"]
                   if r["name"] == "goodgame")
        assert row["status"] == "move"

    def test_the_move_filter_finds_it(self, moved):
        base, _app = moved
        found = get(base, "/api/machines?status=move&limit=500")
        assert "goodgame" in {row["name"] for row in found["rows"]}

    def test_both_pages_count_the_same_machines(self, moved):
        base, _app = moved
        kinds = {entry["kind"]: entry for entry in get(base, "/api/changes")["kinds"]}
        for kind in ("new", "move"):
            rows = get(base, f"/api/machines?status={kind}&limit=500")
            assert rows["total"] == kinds[kind]["machines"], kind

    def test_a_machine_that_is_nowhere_is_still_missing(self, moved):
        base, app = moved
        absent = next(item for item in app.job.plan.wanted
                      if not item.in_library and not item.rom_source)
        row = next(r for r in get(base, "/api/machines?limit=500")["rows"]
                   if r["name"] == absent.name)
        assert row["status"] == "missing"


class TestARequestThatIsNotOurs:
    """A page on another origin can send a form or text/plain POST without a
    preflight. With no token on localhost, that used to be enough to start a run."""

    def raw_post(self, base, path, body, headers):
        request = urllib.request.Request(base + path, data=body, headers=headers,
                                         method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def test_a_text_plain_post_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            self.raw_post(base, "/api/cancel", b"{}", {"Content-Type": "text/plain"})
        assert error.value.code == 400

    def test_a_bad_content_length_is_a_400_not_a_hang(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            self.raw_post(base, "/api/cancel", b"{}",
                          {"Content-Type": "application/json", "Content-Length": "abc"})
        assert error.value.code == 400


class TestCancelSaysWhetherAnythingStops:
    def test_idle_has_nothing_to_stop(self, server):
        base, _app = server
        assert post(base, "/api/cancel", {}) == {"cancelling": False}


class TestTwoStartsAtOnce:
    def test_only_one_run_gets_through(self, config, romset, xml_path, catlist_path):
        job = Job()
        job.start_plan(config, SourceOptions(xml=xml_path, catlist=catlist_path))
        wait_for(job, "planned", "error")
        started, refused = [], []
        gate = threading.Event()

        def slow():
            gate.wait(5)

        def attempt():
            try:
                job._start(slow, "copying", "done")
                started.append(1)
            except MarqueeError:
                refused.append(1)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        gate.set()
        assert len(started) == 1 and len(refused) == 7


class TestAnExcludedCategoryCanStillBeOpened:
    """The tree lists every category so one can be put back. Machines left out by a
    genre on the exclude list used to be kept nowhere -- not in the plan, not in the
    left-out catalogue -- so "Board Game / Cards: 1" opened onto nothing."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path,
                                 "blacklist_genres": ["Board Game"]})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_the_selection_page_sees_them(self, planned):
        base, _app = planned
        rows = get(base, "/api/machines?genre=Board+Game&excluded=1")["rows"]
        assert [row["name"] for row in rows] == ["boardgame1"]
        assert rows[0]["excluded"] is True
        keys = get(base, "/api/selection?genre=Board+Game&excluded=1")["machines"]
        assert [key["name"] for key in keys] == ["boardgame1"]

    def test_the_library_does_not(self, planned):
        base, _app = planned
        assert get(base, "/api/machines?genre=Board+Game")["total"] == 0
        assert get(base, "/api/selection?genre=Board+Game")["total"] == 0

    def test_the_tree_count_and_the_rows_agree(self, planned):
        base, app = planned
        cats = {c["name"]: c for c in get(base, "/api/state")["plan"]["categories"]}
        board = next(c for c in cats.values() if c["genre"] == "Board Game")
        listed = get(base, f"/api/machines?category={urllib.parse.quote(board['name'])}"
                           "&excluded=1")["total"]
        assert listed == board["wanted"] == 1


class TestTheTransferByGenre:
    """The same rows the list shows, under the Selection page's genre -> category tree,
    so a run can be read as what it does to the library rather than as 1,144 lines."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def test_the_tree_adds_up_to_the_list(self, planned):
        base, _app = planned
        flat = get(base, "/api/changes?kind=new&limit=500")
        tree = get(base, "/api/changes?kind=new&group=1")
        assert tree["rows"] == [], "a grouped answer carries totals, not rows"
        genres = tree["tree"]
        assert genres, "a plan with new files has at least one genre"
        assert sum(g["machines"] for g in genres) == flat["total"]
        assert sum(g["bytes"] for g in genres) == flat["bytes"]
        for genre in genres:
            assert sum(c["machines"] for c in genre["categories"]) == genre["machines"]
            assert all(c["label"] for c in genre["categories"])

    def test_a_branch_fetches_only_its_own_games(self, planned):
        base, _app = planned
        tree = get(base, "/api/changes?kind=new&group=1")
        leaf = tree["tree"][0]["categories"][0]
        rows = get(base, "/api/changes?kind=new&category="
                         + urllib.parse.quote(leaf["name"]))["rows"]
        assert len(rows) == leaf["machines"]
        assert {row["category"] for row in rows} == {leaf["name"]}

    def test_orphans_are_grouped_by_the_folder_they_sit_in(self, planned, romset):
        base, app = planned
        stray = romset["out_dir"] / "Maze" / "Misc" / "stray.zip"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"x" * 10)
        from marquee import pipeline
        app.job.plan.sync = pipeline.compare_destination(app.job.plan, app.job.config)
        tree = get(base, "/api/changes?kind=orphan&group=1")["tree"]
        assert [g["name"] for g in tree] == ["Maze"]
        assert tree[0]["categories"][0]["name"] == "Maze/Misc"


class TestReachingTheLibrary:
    """The picker walks this machine's disks; a share on the console has to be
    tested, not browsed."""

    def test_a_local_folder_answers_with_what_is_in_it(self, server, romset):
        base, _app = server
        (romset["out_dir"] / "Maze").mkdir()
        answer = post(base, "/api/destination/test", {"copy_path": str(romset["out_dir"])})
        assert answer["ok"] and answer["remote"] is False
        assert answer["sample"] == ["Maze"]
        assert answer["writable"] is True
        assert "Maze" in answer["message"]

    def test_a_folder_that_is_not_there_says_so(self, server, tmp_path):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/destination/test", {"copy_path": str(tmp_path / "nowhere")})
        assert error.value.code == 400
        assert "nowhere" in json.loads(error.value.read())["error"]

    def test_a_share_that_cannot_be_reached_is_a_message_not_a_traceback(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/destination/test", {"copy_path": "smb://127.0.0.1:1/share/mame"})
        assert error.value.code == 400
        assert "Could not reach smb://127.0.0.1:1/share/mame" in json.loads(error.value.read())["error"]


class TestTheKeyFromTheEnvironment:
    def test_the_variable_wins_and_is_not_written_down(self, tmp_path, monkeypatch):
        from marquee.web.server import api_key
        monkeypatch.setenv("MARQUEE_API_KEY", "  from-the-nas  ")
        assert api_key(str(tmp_path)) == "from-the-nas"
        assert not (tmp_path / "api_key").exists()

    def test_an_empty_variable_means_the_file(self, tmp_path, monkeypatch):
        from marquee.web.server import api_key
        monkeypatch.setenv("MARQUEE_API_KEY", "")
        key = api_key(str(tmp_path))
        assert key and (tmp_path / "api_key").read_text().strip() == key


class TestChoosingTheRelease:
    def test_a_version_from_the_form_is_saved(self, server):
        base, app = server
        post(base, "/api/save", {"mame_version": "0.289"})
        assert app.current_config().mame_version == "0.289"

    def test_automatic_clears_it(self, server):
        base, app = server
        post(base, "/api/save", {"mame_version": "0.289"})
        post(base, "/api/save", {"mame_version": ""})
        assert app.current_config().mame_version is None

    def test_a_form_that_does_not_mention_it_leaves_it_alone(self, server):
        base, app = server
        post(base, "/api/save", {"mame_version": "0.289"})
        post(base, "/api/save", {"allow_mature": True})
        assert app.current_config().mame_version == "0.289"


class TestWhichMarqueeThisIs:
    def test_the_state_says_the_version_and_the_build(self, server, monkeypatch):
        base, app = server
        monkeypatch.setenv("MARQUEE_NO_UPDATE_CHECK", "1")
        monkeypatch.setenv("MARQUEE_BUILD", "v0.5.0@7c17217fde7bb8740f452794e1ebb0025fe8e702")
        about = get(base, "/api/state")["app"]
        from marquee import __version__
        assert about["version"] == __version__
        assert about["build"] == "v0.5.0@7c17217"
        assert about["update"]["newer"] is False

    def test_the_newest_tag_wins_and_junk_is_ignored(self):
        from marquee.web.application import newest_version, build_label
        assert newest_version(["v0.4.0", "v0.10.1", "v0.5.0", "nightly", "v1.0.0-rc1"]) == "0.10.1"
        assert newest_version([]) is None
        import os
        os.environ.pop("MARQUEE_BUILD", None)
        assert build_label() == "source"

    def test_a_newer_tag_is_reported(self, server, monkeypatch):
        base, app = server
        monkeypatch.setenv("MARQUEE_NO_UPDATE_CHECK", "1")
        app.update.update(latest="99.0.0", checked_at=1)
        about = get(base, "/api/state")["app"]
        assert about["update"]["newer"] is True
        assert about["update"]["url"].endswith("/releases/tag/v99.0.0")


class TestWhatTheClientIsStillFetching:
    """The plan asks the download client which files are still arriving and keeps
    them out of the transfer, whatever the files on disk look like."""

    class Client:
        def __init__(self, save_path, arriving, torrent_progress=0.4, size=200008):
            self.save_path, self.arriving = save_path, arriving
            self.torrent_progress = torrent_progress
            self.size = size

        def status(self, infohashes=None, category=None):
            return [{"hash": "cd" + "0" * 38, "progress": self.torrent_progress,
                     "save_path": self.save_path, "category": "marquee"}]

        def files(self, infohash):
            return [{"index": index, "path": path, "progress": progress,
                     "size": self.size}
                    for index, (path, progress) in enumerate(self.arriving)]

        def properties(self, infohash):
            return {"piece_size": 4096}

        def piece_hashes(self, infohash):
            return getattr(self, "hashes", [])

    def test_a_deselected_file_in_a_finished_torrent_still_counts(self, server, romset):
        """A torrent reads 100% once its unwanted files are deselected. The disk at
        96% that narrow() dropped because the library 'had' it is exactly the one
        that must not be copied -- and it was."""
        base, app = server
        chd_dir = romset["chd_dir"]
        app.client = lambda overrides=None: self.Client(
            str(chd_dir.parent), [("chds/twodisk/ok.chd", 0.96)], torrent_progress=1.0)
        post(base, "/api/save", {"download_client": "http://client:1", "download_dir": str(chd_dir.parent)})
        assert app.source_progress() == {str(chd_dir / "twodisk" / "ok.chd"): 0.96}
        assert app.incomplete_sources() == {str(chd_dir / "twodisk" / "ok.chd")}

    def test_a_deep_check_hashes_disks_against_the_torrent(self, server, romset, xml_path,
                                                           catlist_path):
        import hashlib
        base, app = server
        chd_dir = romset["chd_dir"]
        size = (chd_dir / "twodisk" / "ok.chd").stat().st_size
        client = self.Client(str(chd_dir.parent), [("chds/twodisk/ok.chd", 1.0)],
                             torrent_progress=1.0, size=size)
        app.client = lambda overrides=None: client
        post(base, "/api/save", {"download_client": "http://client:1", "download_dir": str(chd_dir.parent)})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        item = next(one for one in app.job.plan.wanted if one.name == "twodisk")
        library = romset["out_dir"] / item.folder / "twodisk" / "ok.chd"
        assert library.is_file()
        # The torrent's piece hashes are those of what is in the library -- except the
        # library copy is then given one wrong piece.
        good = library.read_bytes()
        assert len(good) == size
        client.properties = lambda infohash: {"piece_size": 64}
        padded = good + b"\x00" * (-len(good) % 64)
        client.hashes = [hashlib.sha1(padded[i:i + 64]).hexdigest()
                         for i in range(0, len(padded), 64)]
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        answer = post(base, "/api/check", {"deep": True})
        assert answer["deep"] is True
        wait_for(app.job, "planned", "error")
        assert app.job.checked.get("damaged", 0) == 0
        bad = bytearray(good)
        bad[300:310] = b"\x00" * 10
        library.write_bytes(bytes(bad))
        post(base, "/api/check", {"deep": True})
        wait_for(app.job, "planned", "error")
        assert app.job.checked.get("damaged") == 1
        item = next(one for one in app.job.plan.wanted if one.name == "twodisk")   # re-planned since
        assert item.damaged_disks == [f"{item.folder}/twodisk/ok.chd"]

    def test_arriving_files_are_mapped_into_this_apps_paths(self, server, romset):
        base, app = server
        chd_dir = romset["chd_dir"]
        app.client = lambda overrides=None: self.Client(
            "/data/torrents", [("chds/twodisk/ok.chd", 0.4), ("chds/parentchd/pdisk.chd", 1.0)])
        post(base, "/api/save", {"download_client": "http://client:1", "download_dir": str(chd_dir.parent)})
        found = app.incomplete_sources()
        assert found == {str(chd_dir / "twodisk" / "ok.chd")}

    def test_the_plan_leaves_them_out(self, server, romset, xml_path, catlist_path):
        base, app = server
        chd_dir = romset["chd_dir"]
        app.client = lambda overrides=None: self.Client(
            str(chd_dir.parent), [("chds/twodisk/ok.chd", 0.4)])
        post(base, "/api/save", {"download_client": "http://client:1", "download_dir": str(chd_dir.parent)})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error
        twodisk = next(item for item in app.job.plan.wanted if item.name == "twodisk")
        assert twodisk.partial is True and "ok" in twodisk.missing_disks
        assert any("still arriving" in event["text"] for event in app.job.snapshot(0)["events"])

    def test_a_client_that_will_not_answer_is_not_fatal(self, server, romset, xml_path, catlist_path):
        base, app = server

        def broken(overrides=None):
            raise MarqueeError("no client today")
        app.client = broken
        post(base, "/api/save", {"download_client": "http://client:1"})
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        assert app.job.state == "planned", app.job.error


class TestLibraryCredentials:
    """A library on a share carries its password inside the URL. It went out on
    every poll of /api/state, in `built_from`, and in "could not reach" errors."""

    SECRET = "s3cr@t"
    URL = f"smb://bob:{SECRET}@nas/Share/roms"

    @pytest.fixture
    def shared(self, server, config):
        base, app = server
        config.copy_path = self.URL
        configuration.write_settings_file(config.settings_path, config)
        return base, app

    def raw(self, base, path):
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.read().decode()

    def test_the_state_never_carries_the_password(self, shared):
        base, _app = shared
        body = self.raw(base, "/api/state")
        assert self.SECRET not in body
        assert json.loads(body)["config"]["copy_path"] == \
            "smb://bob:••••••••@nas/Share/roms"

    def test_the_mask_coming_back_keeps_the_saved_password(self, shared):
        base, app = shared
        masked = get(base, "/api/state")["config"]["copy_path"]
        post(base, "/api/save", {"copy_path": masked.replace("/roms", "/mame")})
        assert app.current_config().copy_path == f"smb://bob:{self.SECRET}@nas/Share/mame"

    def test_the_mask_is_never_sent_to_a_different_server(self, shared):
        base, app = shared
        masked = get(base, "/api/state")["config"]["copy_path"]
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/save", {"copy_path": masked.replace("@nas/", "@evil/")})
        assert error.value.code == 400
        assert app.current_config().copy_path == self.URL

    def test_a_new_password_is_taken_as_typed(self, shared):
        base, app = shared
        post(base, "/api/save", {"copy_path": "smb://bob:other@nas/Share/roms"})
        assert app.current_config().copy_path == "smb://bob:other@nas/Share/roms"

    def test_a_failed_test_does_not_repeat_the_password(self, shared):
        base, _app = shared
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/destination/test",
                 {"copy_path": f"ftp://bob:{self.SECRET}@127.0.0.1:1/roms"})
        assert self.SECRET not in error.value.read().decode()


class TestRedact:
    def test_every_scheme_is_hidden(self):
        from marquee.backends import MASK, redact
        for url in ("smb://u:p@h/s", "ftp://u:p@h/r", "ftps://u:p@h:990/r",
                    "sftp://u:p@h/r"):
            assert "p@" not in redact(url).replace(MASK, "")
            assert redact(url).startswith(url.split("u:")[0] + "u:" + MASK + "@")

    def test_nothing_to_hide_is_left_alone(self):
        from marquee.backends import redact
        for url in ("smb://h/s", "sftp://u@h/r", "/library", "", None):
            assert redact(url) == url

    def test_an_at_sign_in_the_password_stays_hidden(self):
        from marquee.backends import redact
        assert "cret" not in redact("smb://u:se@cret@h/s")


class TestDnsRebinding:
    """With no key on localhost, the Host header is the only thing telling a request
    from this machine apart from a page on evil.example re-pointed at 127.0.0.1."""

    def request(self, base, method, path, host, body=None):
        import http.client
        port = int(base.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": host, "Content-Type": "application/json"}
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers=headers)
        response = conn.getresponse()
        status, text = response.status, response.read().decode()
        conn.close()
        return status, text

    def test_a_foreign_host_is_refused(self, server):
        base, _app = server
        assert self.request(base, "GET", "/api/state", "evil.example")[0] == 403
        status, _text = self.request(base, "POST", "/api/save", "evil.example:8777",
                                     {"copy_path": "/tmp/elsewhere"})
        assert status == 403

    def test_localhost_by_any_of_its_names_is_served(self, server):
        base, _app = server
        port = base.rsplit(":", 1)[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}",
                     "localhost"):
            assert self.request(base, "GET", "/api/state", host)[0] == 200, host

    def test_a_name_can_be_allowed_for_a_proxy(self, server, monkeypatch):
        base, _app = server
        monkeypatch.setenv("MARQUEE_ALLOWED_HOSTS", "marquee.lan, other")
        assert self.request(base, "GET", "/api/state", "marquee.lan")[0] == 200

    def test_the_shell_still_loads(self, server):
        base, _app = server
        assert self.request(base, "GET", "/api/health", "evil.example")[0] == 200

    def test_with_a_key_the_key_decides(self, server):
        base, app = server
        app.token = "k"
        status, _text = self.request(base, "GET", "/api/state?token=k", "marquee.lan")
        assert status == 200


class TestTheSavedClientPasswordStaysWithTheSavedClient:
    """The Test button sends the form's address with the password masked. Paired
    with the saved password, any address a request named got it."""

    @pytest.fixture
    def app(self, server, config):
        _base, app = server
        config.download_client = "http://nas:8080/"
        config.download_username = "admin"
        config.download_password = "hunter2"
        configuration.write_settings_file(config.settings_path, config)
        return app

    def built(self, app, monkeypatch, overrides):
        seen = {}
        monkeypatch.setattr("marquee.download.for_url",
                            lambda url, username=None, password=None:
                            seen.update(url=url, username=username, password=password))
        app.client(overrides)
        return seen

    def test_the_saved_client_gets_the_saved_password(self, app, monkeypatch):
        seen = self.built(app, monkeypatch, {"download_client": "http://nas:8080",
                                             "download_password": "•" * 8})
        assert seen["password"] == "hunter2"

    def test_another_address_does_not(self, app, monkeypatch):
        seen = self.built(app, monkeypatch, {"download_client": "http://evil:8080/",
                                             "download_password": "•" * 8})
        assert seen["password"] is None

    def test_another_user_does_not(self, app, monkeypatch):
        seen = self.built(app, monkeypatch, {"download_username": "guest"})
        assert seen["password"] is None

    def test_a_typed_password_is_used_anywhere(self, app, monkeypatch):
        seen = self.built(app, monkeypatch, {"download_client": "http://new:8080/",
                                             "download_password": "typed"})
        assert seen["password"] == "typed"


class TestAPlanTheSettingsHaveMovedOn:
    """The transfer runs the configuration the plan was built with. Settings saved
    since, that would have shaped the plan, made it the wrong plan -- and the ones
    that only decide how files are written were silently ignored."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        time.sleep(0.02)
        return base, app

    def test_a_genre_left_out_since_refuses_the_transfer(self, planned):
        base, app = planned
        genre = app.job.plan.items[0].genre
        post(base, "/api/save", {"blacklist_genres": [genre]})
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/copy", {})
        assert error.value.code == 400
        assert b"Build the plan again" in error.value.read()
        assert app.job.state == "planned"

    def test_parents_only_counts_as_moving_on(self, planned):
        base, _app = planned
        post(base, "/api/save", {"parents_only": True})
        with pytest.raises(urllib.error.HTTPError):
            post(base, "/api/copy", {})

    def test_a_hardlink_ticked_since_is_used(self, planned):
        base, app = planned
        post(base, "/api/save", {"hardlink": True, "write_gamelist": False})
        post(base, "/api/copy", {})
        wait_for(app.job, "done", "error")
        assert app.job.config.hardlink is True
        assert app.job.config.write_gamelist is False

    def test_nothing_saved_since_runs_as_planned(self, planned):
        base, app = planned
        post(base, "/api/copy", {})
        assert wait_for(app.job, "done", "error") == "done"


class TestABodyOfTheWrongShape:
    """Valid JSON, wrong shape: each used to be a 500 with a traceback."""

    @pytest.mark.parametrize("path,payload", [
        ("/api/save", []), ("/api/save", None), ("/api/import", []),
        ("/api/plan", {"rom_dir": 5}), ("/api/save", {"download_password": 5}),
    ])
    def test_it_is_a_400_that_says_why(self, server, path, payload):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, path, payload)
        assert error.value.code == 400
        assert "Traceback" not in error.value.read().decode()


class TestBackgroundTasksStartOnce:
    """Each task checked "running" and set it in two steps; two requests close
    together both started one."""

    def test_a_task_is_claimed_once_however_many_ask(self, server):
        _base, app = server
        task = {"running": False}
        results = []
        barrier = threading.Barrier(8)

        def ask():
            barrier.wait()
            results.append(app._claim(task))
        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert results.count(True) == 1

    def test_a_survey_that_fails_says_so(self, server, monkeypatch):
        base, app = server

        def broken(_paths):
            raise PermissionError("no entry")
        monkeypatch.setattr("marquee.sync.survey", broken)
        post(base, "/api/survey", {})
        for _ in range(200):
            if not app.survey["running"]:
                break
            time.sleep(0.02)
        assert "no entry" in (app.survey["error"] or "")
        assert get(base, "/api/state")["survey"]["error"]


class TestTheStatePollSendsOnlyWhatMoved:
    """On a real library the plan and the settings were 130 KB of a 137 KB answer,
    sent every 0.7 s while busy. The page names the revisions it holds."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        return base, app

    def revs(self, state):
        return urllib.parse.urlencode({"plan_rev": state["plan_rev"],
                                       "config_rev": state["config_rev"]})

    def test_what_the_page_holds_is_left_out(self, planned):
        base, _app = planned
        first = get(base, "/api/state")
        assert first["plan"] and first["config"]
        again = get(base, "/api/state?" + self.revs(first))
        assert "plan" not in again and "config" not in again
        assert again["plan_rev"] == first["plan_rev"]
        assert again["state"] == "planned", "everything else still comes"

    def test_a_save_sends_the_settings_again(self, planned):
        base, _app = planned
        first = get(base, "/api/state")
        post(base, "/api/save", {"allow_mature": False})
        again = get(base, "/api/state?" + self.revs(first))
        assert again["config"]["allow_mature"] is False
        assert again["config_rev"] != first["config_rev"]

    def test_a_new_plan_is_sent(self, planned, xml_path, catlist_path):
        base, app = planned
        first = get(base, "/api/state")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "planned", "error")
        again = get(base, "/api/state?" + self.revs(first))
        assert again["plan"] and again["plan_rev"] != first["plan_rev"]

    def test_without_revisions_everything_comes(self, planned):
        base, _app = planned
        state = get(base, "/api/state")
        assert state["plan"]["built_from"] and state["config"]


class TestOneDownloadClientPerAddress:
    """A new client per call was a new qBittorrent login per call, and the queue
    alone asks every few seconds."""

    @pytest.fixture
    def app(self, server, config):
        _base, app = server
        config.download_client, config.download_username = "http://nas:8080/", "admin"
        config.download_password = "hunter2"
        configuration.write_settings_file(config.settings_path, config)
        return app

    def test_the_same_client_is_used_again(self, app):
        assert app.client() is app.client()

    def test_other_credentials_get_their_own(self, app):
        assert app.client() is not app.client({"download_password": "other"})

    def test_the_queue_asks_only_for_this_tools_category(self, app, monkeypatch):
        asked = []

        class Probe:
            def status(self, infohashes=None, category=None):
                asked.append(category)
                return []
        monkeypatch.setattr(app, "client", lambda overrides=None: Probe())
        app.queue_payload()
        assert asked == ["marquee"]


class TestStopping:
    """SIGTERM from `docker stop` used to be dropped (the server is PID 1) and the
    container was killed ten seconds later, mid-file."""

    class Job:
        def __init__(self, stops_after):
            self.stops_after, self.cancelled = stops_after, None

        def cancel(self):
            self.cancelled = time.monotonic()
            return True

        @property
        def busy(self):
            return self.cancelled is None or time.monotonic() - self.cancelled < self.stops_after

    def app(self, stops_after):
        return type("App", (), {"job": self.Job(stops_after)})()

    def test_a_transfer_is_told_to_stop_and_waited_for(self):
        from marquee.web import server as web_server
        app = self.app(0.2)
        assert web_server.shutdown(app, grace=2) is True
        assert app.job.cancelled is not None

    def test_the_wait_has_an_end(self):
        from marquee.web import server as web_server
        started = time.monotonic()
        assert web_server.shutdown(self.app(60), grace=0.3) is False
        assert time.monotonic() - started < 2

    def test_sigterm_is_turned_into_a_stop(self):
        from marquee.web import server as web_server
        with pytest.raises(KeyboardInterrupt):
            web_server._terminate(15, None)


class TestAStartedTask:
    """One wrapper for every background task: whatever the work raises is shown, and
    the task is never left looking busy."""

    def wait(self, task):
        for _ in range(200):
            if not task["running"]:
                return
            time.sleep(0.01)
        raise AssertionError("still running")

    def test_an_unexpected_error_is_recorded(self, server):
        _base, app = server
        task, finished = {"running": False, "error": None}, []

        def work():
            raise ValueError("too many values to unpack")
        assert app._start(task, work, finish=lambda: finished.append(True)) is True
        self.wait(task)
        assert task["error"] == "ValueError: too many values to unpack"
        assert finished == [True]

    def test_our_own_errors_read_as_written(self, server):
        from marquee.errors import MarqueeError
        _base, app = server
        task = {"running": False, "error": None}

        def work():
            raise MarqueeError("No download client is configured.")
        app._start(task, work)
        self.wait(task)
        assert task["error"] == "No download client is configured."

    def test_a_running_task_is_not_started_twice(self, server):
        _base, app = server
        task = {"running": True}
        assert app._start(task, lambda: None) is False


class TestTheHardlinkSetting:
    def test_automatic_is_reported_with_what_it_comes_to(self, server):
        base, _app = server
        post(base, "/api/save", {"hardlink": "auto"})
        config = get(base, "/api/state")["config"]
        assert config["hardlink"] == "auto"
        assert isinstance(config["hardlink_effective"], bool)

    def test_it_can_be_pinned_and_set_back_to_automatic(self, server):
        base, app = server
        post(base, "/api/save", {"hardlink": False})
        assert app.current_config().hardlink is False
        post(base, "/api/save", {"hardlink": "auto"})
        assert app.current_config().hardlink is None


class TestReclaimingSpace:
    """The one act that takes finished data away: Marquee's own torrents only, only
    when the library has every file, priced first, and only when asked by name."""

    @pytest.fixture
    def rig(self, server, tmp_path):
        from types import SimpleNamespace
        from marquee import sync as sync_module
        base, app = server
        torrents = tmp_path / "torrents" / "MAME 0.289 ROMs (non-merged)"
        torrents.mkdir(parents=True)
        library = tmp_path / "lib"
        (library / "Maze").mkdir(parents=True)
        files = {}
        for name in ("pacman", "galaga"):
            path = torrents / f"{name}.zip"
            path.write_bytes(b"x" * 100)
            files[name] = path
        (library / "Maze" / "pacman.zip").write_bytes(b"x" * 100)
        post(base, "/api/save", {"copy_path": str(library)})
        actions = [sync_module.Action(sync_module.KEEP, "Maze/pacman.zip",
                                      str(files["pacman"]), size=100)]
        app.job.plan = SimpleNamespace(sync=SimpleNamespace(actions=actions))
        state = {"torrents": [], "deleted": []}
        app._torrent_files = lambda config: state["torrents"]

        class Client:
            def delete(self, infohash, delete_files=False):
                state["deleted"].append((infohash, delete_files))
        app.client = lambda overrides=None: Client()
        return base, app, files, library, state

    def torrent(self, infohash, category, paths, progress=1.0):
        entry = {"hash": infohash, "name": infohash, "category": category}
        return (entry, [(str(path), {"size": 100, "progress": progress}) for path in paths])

    def test_a_torrent_whose_files_are_all_in_the_library_is_priced(self, rig):
        base, _app, files, _library, state = rig
        state["torrents"] = [self.torrent("aa" * 20, "marquee", [files["pacman"]])]
        price = get(base, "/api/reclaim")
        assert price["torrents"][0]["reclaimable"] is True
        assert price["freed"] == 100

    def test_a_file_not_in_the_library_blocks_it(self, rig):
        base, _app, files, _library, state = rig
        state["torrents"] = [self.torrent("aa" * 20, "marquee",
                                          [files["pacman"], files["galaga"]])]
        row = get(base, "/api/reclaim")["torrents"][0]
        assert row["reclaimable"] is False and "only copy" in row["blocked"]

    def test_a_download_still_arriving_blocks_it(self, rig):
        base, _app, files, _library, state = rig
        state["torrents"] = [self.torrent("aa" * 20, "marquee", [files["pacman"]], 0.5)]
        assert "still arriving" in get(base, "/api/reclaim")["torrents"][0]["blocked"]

    def test_a_skipped_file_touched_by_a_shared_piece_is_not_arriving(self, rig):
        """A deselected file shows a sliver of progress from the pieces it shares with
        a wanted neighbour; that data is in the client's part file, not in the file."""
        base, _app, files, _library, state = rig
        entry, named = self.torrent("aa" * 20, "marquee", [files["pacman"]])
        named.append((str(files["galaga"]) + ".nowhere", {"size": 100, "progress": 1.0,
                                                          "priority": 0}))
        state["torrents"] = [(entry, named)]
        row = get(base, "/api/reclaim")["torrents"][0]
        assert row["reclaimable"] is True and row["files"] == 1

    def test_a_deselected_file_really_on_disk_still_blocks(self, rig):
        """Deselected by hand after it finished: the file is there, and not in the
        library, so removing the torrent would delete it."""
        base, _app, files, _library, state = rig
        entry, named = self.torrent("aa" * 20, "marquee", [files["pacman"]])
        named.append((str(files["galaga"]), {"size": 100, "progress": 1.0, "priority": 0}))
        state["torrents"] = [(entry, named)]
        assert get(base, "/api/reclaim")["torrents"][0]["reclaimable"] is False

    def test_somebody_elses_torrent_is_not_even_listed(self, rig):
        base, _app, files, _library, state = rig
        state["torrents"] = [self.torrent("bb" * 20, None, [files["pacman"]])]
        assert get(base, "/api/reclaim")["torrents"] == []

    def test_a_hardlinked_library_frees_nothing_and_says_so(self, rig):
        base, _app, files, library, state = rig
        target = library / "Maze" / "pacman.zip"
        target.unlink()
        os.link(files["pacman"], target)
        state["torrents"] = [self.torrent("aa" * 20, "marquee", [files["pacman"]])]
        row = get(base, "/api/reclaim")["torrents"][0]
        assert row["freed"] == 0 and row["linked"] == 1

    def test_removal_is_only_what_was_asked_and_allowed(self, rig):
        base, _app, files, _library, state = rig
        state["torrents"] = [self.torrent("aa" * 20, "marquee", [files["pacman"]]),
                             self.torrent("cc" * 20, "marquee", [files["galaga"]])]
        with pytest.raises(urllib.error.HTTPError):
            post(base, "/api/reclaim", {})
        with pytest.raises(urllib.error.HTTPError):
            post(base, "/api/reclaim", {"hashes": ["cc" * 20]})
        assert state["deleted"] == []
        done = post(base, "/api/reclaim", {"hashes": ["aa" * 20]})
        assert done["removed"] == 1 and done["freed"] == 100
        assert state["deleted"] == [("aa" * 20, True)]


class TestAFirstPlanWithAnEmptyTorrentFolder:
    """Nothing chosen, nothing downloaded yet, so nothing is named after a release:
    the plan used to stop and ask for one. The newest published set is the answer."""

    def test_the_newest_listed_rom_set_is_used(self, server, monkeypatch):
        from marquee import fetch, pipeline
        _base, app = server
        app.releases["data"] = [
            {"kind": "roms", "full_set": True, "version": "0.288"},
            {"kind": "roms", "full_set": True, "version": "0.289"},
            {"kind": "chds", "full_set": True, "version": "0.290"},
        ]
        asked = []
        monkeypatch.setattr(fetch, "fetch_xml",
                            lambda version, **kw: asked.append(version) or "/nowhere.xml")
        options = pipeline.SourceOptions(newest_published=app.newest_published)
        config = configuration.Config(rom_dir="/empty", copy_path="/o")
        monkeypatch.setattr(pipeline.sources, "find_mame_xml", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.sources, "generate_listxml", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.sources, "locate_set", lambda *a, **k: "/empty")
        assert pipeline.resolve_mame_xml(config, options, pipeline.Reporter()) == "/nowhere.xml"
        assert asked == ["0.289"]


class TestACrossSitePostWithNoBody:
    """`fetch(url, {method: "POST", mode: "no-cors"})` from any page sends no body and
    no JSON content type. On a keyless localhost server it used to start a transfer."""

    def test_it_is_refused_before_anything_runs(self, server):
        base, app = server
        request = urllib.request.Request(base + "/api/copy", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=10)
        assert error.value.code == 400
        assert app.job.state == "idle"

    def test_the_page_s_own_empty_post_still_works(self, server):
        base, _app = server
        request = urllib.request.Request(base + "/api/cancel", data=b"", method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.status == 200


class TestALibraryThatCannotBeReached:
    """The console was off when the NAS restarted: the plan took the unreadable share
    for an empty library and Wanted offered all 10,022 games again. Now the plan fails,
    and Wanted must not turn "no plan" into "nothing missing" either."""

    def test_the_plan_fails_and_wanted_says_why(self, server, monkeypatch, xml_path,
                                                catlist_path):
        from marquee import pipeline
        from marquee.backends import BackendError

        def gone(*_args, **_kwargs):
            raise BackendError("Failed to connect to 192.168.1.20: No route to host")

        monkeypatch.setattr(pipeline.backends, "for_destination", gone)
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "error")
        assert app.job.plan is None
        missing = get(base, "/api/missing")
        assert missing["planned"] is False
        assert "could not be read" in missing["reason"]
        with pytest.raises(urllib.error.HTTPError):
            post(base, "/api/missing/fetch", {})


class TestPlanningAgainOnceTheLibraryAnswers:
    """The console is off most of the time. A plan that failed because the library
    could not be reached is tried again, by itself, once the library answers -- probed
    with a plain connection, so the page does not flip to "planning" every minute."""

    @pytest.fixture
    def waiting(self, server, monkeypatch):
        from marquee.errors import LibraryUnreachable
        from marquee.web import application as module
        base, app = server
        app.job.failure = LibraryUnreachable("gone")
        app.job.state = "error"
        started = []
        monkeypatch.setattr(app, "autoplan", lambda: started.append(1) or {"started": True})
        monkeypatch.setattr(module.backends, "is_remote", lambda _path: True)
        return app, started, monkeypatch, module

    def test_nothing_happens_while_it_is_still_off(self, waiting):
        app, started, monkeypatch, module = waiting
        monkeypatch.setattr(module.backends, "answers", lambda *_a, **_k: False)
        assert app.replan_when_reachable()["started"] is False
        assert started == []

    def test_it_plans_once_the_library_answers(self, waiting):
        app, started, monkeypatch, module = waiting
        monkeypatch.setattr(module.backends, "answers", lambda *_a, **_k: True)
        assert app.replan_when_reachable()["started"] is True
        assert started == [1]

    def test_answering_but_still_failing_is_not_retried_every_minute(self, waiting):
        # A console half way through booting: the port is open, the plan fails again.
        app, started, monkeypatch, module = waiting
        monkeypatch.setattr(module.backends, "answers", lambda *_a, **_k: True)
        assert app.replan_when_reachable()["started"] is True
        assert app.replan_when_reachable()["started"] is False
        assert started == [1]
        app._library_watch["next_at"] = 0.0          # the back-off has run out
        assert app.replan_when_reachable()["started"] is True
        assert app._library_watch["delay"] == 4 * app.REPLAN_BACKOFF

    def test_off_and_on_again_plans_at_once(self, waiting):
        app, started, monkeypatch, module = waiting
        answer = {"now": True}
        monkeypatch.setattr(module.backends, "answers", lambda *_a, **_k: answer["now"])
        assert app.replan_when_reachable()["started"] is True
        answer["now"] = False
        assert app.replan_when_reachable()["started"] is False
        answer["now"] = True
        assert app.replan_when_reachable()["started"] is True
        assert started == [1, 1]

    def test_any_other_failure_is_left_for_the_user(self, waiting):
        from marquee.errors import MarqueeError
        app, started, monkeypatch, module = waiting
        app.job.failure = MarqueeError("the catlist is for another release")
        monkeypatch.setattr(module.backends, "answers", lambda *_a, **_k: True)
        assert app.replan_when_reachable()["started"] is False
        assert started == []

    def test_a_failed_plan_remembers_why(self, server, monkeypatch, xml_path,
                                         catlist_path):
        from marquee import pipeline
        from marquee.backends import BackendError
        from marquee.errors import LibraryUnreachable

        def gone(*_args, **_kwargs):
            raise BackendError("No route to host")

        monkeypatch.setattr(pipeline.backends, "for_destination", gone)
        base, app = server
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait_for(app.job, "error")
        assert isinstance(app.job.failure, LibraryUnreachable)
        assert get(base, "/api/missing")["waiting"] is True
