"""The web front end: job lifecycle, the JSON API, and the token gate."""
import json
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
        assert set(entry) == {"name", "genre", "category", "bytes"}
        assert entry["genre"] and entry["category"]
        assert entry["bytes"] == 300

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
