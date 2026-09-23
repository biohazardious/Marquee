"""Acquisition through the web API: what is missing, artwork, and upgrades.

Split out of test_web.py, which had grown to cover the whole surface at once. This
half is everything that reaches outward -- the download client, the artwork cache and
the release listing -- and every one of them is driven by a stand-in, so nothing here
touches the network.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from marquee import config as configuration
from marquee.download.qbittorrent import QBittorrentError
from marquee.web.server import Application, Handler


@pytest.fixture
def server(tmp_path, config, romset):
    config.settings_path = str(tmp_path / "settings.ini")
    configuration.write_settings_file(config.settings_path, config)
    app = Application(config.settings_path)
    handler = type("Bound", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
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


def planned_server(server, xml_path, catlist_path, **settings):
    """A server with a plan in hand, which most of this file needs."""
    base, app = server
    if settings:
        post(base, "/api/save", settings)
    post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
    for _ in range(200):
        if app.job.state in ("planned", "error"):
            break
        time.sleep(0.05)
    assert app.job.state == "planned", app.job.error
    return base, app


class TestMissing:
    """A partly-downloaded set is the normal case; what is absent has to be usable."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        return planned_server(server, xml_path, catlist_path)

    def test_the_poll_payload_carries_counts_not_lists(self, planned):
        base, _app = planned
        plan = get(base, "/api/state")["plan"]
        # These run to thousands of names on a real set and the page polls constantly.
        assert isinstance(plan["missing_roms"], int)
        assert isinstance(plan["missing_examples"], list)

    def test_missing_groups_by_genre(self, planned):
        base, _app = planned
        payload = get(base, "/api/missing")
        assert payload["total"] >= 1
        assert any(entry["machines"] for entry in payload["genres"])

    def test_missing_names_each_machine_with_its_description(self, planned):
        base, _app = planned
        rows = get(base, "/api/missing")["rows"]
        found = {row["name"]: row for row in rows}
        assert "betamax" in found
        # A bare machine name is not something anyone can act on.
        assert found["betamax"]["description"]
        assert found["betamax"]["genre"]

    def test_missing_is_empty_before_anything_is_planned(self, server):
        base, _app = server
        assert get(base, "/api/missing")["total"] == 0

    def test_missing_disks_are_reported_separately(self, planned):
        base, _app = planned
        assert isinstance(get(base, "/api/missing")["disks"], list)


class TestAdultFilter:
    """Adult titles get their own folder; the library has to be filterable by it."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        # The fixture config leaves adult titles out entirely; they have to be in the
        # plan before there is anything to filter.
        return planned_server(server, xml_path, catlist_path, allow_mature=True)

    def rows(self, base, mature=""):
        return get(base, f"/api/machines?limit=500&mature={mature}")["rows"]

    def test_machines_say_whether_they_are_adult(self, planned):
        base, _app = planned
        assert any(row["mature"] for row in self.rows(base))

    def test_only_adult(self, planned):
        base, _app = planned
        rows = self.rows(base, "only")
        assert rows
        assert all(row["mature"] for row in rows)

    def test_hiding_adult(self, planned):
        base, _app = planned
        rows = self.rows(base, "hide")
        assert rows
        assert not any(row["mature"] for row in rows)

    def test_the_two_halves_add_up(self, planned):
        base, _app = planned
        assert len(self.rows(base, "only")) + len(self.rows(base, "hide")) == \
            len(self.rows(base))

    def test_an_unknown_value_filters_nothing(self, planned):
        base, _app = planned
        assert len(self.rows(base, "wat")) == len(self.rows(base))


class TestArtwork:
    """Serving cached pictures, and filling the cache."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        return planned_server(server, xml_path, catlist_path)

    def test_an_uncached_picture_redirects_to_the_source(self, server):
        base, _app = server
        # Redirecting keeps a screen of sixty posters from waiting on sixty fetches
        # through this process.
        request = urllib.request.Request(base + "/art/Named_Titles/Galaga.png")
        opener = urllib.request.build_opener(NoRedirect)
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(request, timeout=10)
        assert error.value.code == 302
        assert "libretro-thumbnails" in error.value.headers["Location"]

    def test_a_cached_picture_is_served_from_disk(self, server, tmp_path, monkeypatch):
        base, _app = server
        from marquee import art as art_module
        target = os.path.join(art_module.art_dir(), "Served Game.png")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\nlocal")
        try:
            with urllib.request.urlopen(
                    base + "/art/Named_Titles/Served%20Game.png", timeout=10) as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == "image/png"
                assert response.read() == b"\x89PNG\r\n\x1a\nlocal"
        finally:
            os.remove(target)

    def test_an_unknown_kind_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/art/Named_Nonsense/x.png", timeout=10)
        assert error.value.code == 404

    def test_status_lists_the_kinds(self, server):
        base, _app = server
        payload = get(base, "/api/art")
        assert "Named_Titles" in payload["kinds"]
        assert payload["running"] is False

    def test_downloading_needs_a_plan(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/art/download", {})
        assert error.value.code == 400

    def test_an_unknown_kind_cannot_be_downloaded(self, planned):
        base, _app = planned
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/art/download", {"kind": "Named_Nonsense"})
        assert error.value.code == 400

    def test_a_download_reports_a_total_and_can_be_stopped(self, planned):
        base, app = planned
        started = post(base, "/api/art/download", {"kind": "Named_Titles"})
        assert started["total"] == len({item.description for item in app.job.plan.wanted})
        post(base, "/api/art/stop", {})
        for _ in range(200):
            if not app.art["running"]:
                break
            time.sleep(0.05)
        assert app.art["running"] is False


class TestArtworkCoversTheWholeLibrary:
    """With the torrent folder empty and the library full, Artwork found nothing to
    illustrate: it only looked at what was in the source folder."""

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        return planned_server(server, xml_path, catlist_path)

    def test_games_only_in_the_library_are_included(self, planned):
        base, app = planned
        plan = app.job.plan
        moved = list(plan.items)
        plan.absent_items = plan.absent_items + moved
        plan.items = []
        try:
            started = post(base, "/api/art/download", {"kind": "Named_Titles"})
            assert started["total"] == len({item.description for item in plan.wanted}) > 0
        finally:
            post(base, "/api/art/stop", {})
            for _ in range(200):
                if not app.art["running"]:
                    break
                time.sleep(0.05)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TestFetchMissing:
    """Asking the download client for what the selection wants but has not got.

    The hazard this guards against is real: the client may already hold the release
    torrent, finished and seeding. `select_only` would drop every file it was not
    asked about to priority 0 and stop it seeding them.
    """

    @pytest.fixture
    def wired(self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path)

        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]

        client = FakeClient(app.job.plan.missing_roms)
        app.client = lambda overrides=None: client
        return base, app, client

    def test_a_dry_run_prices_it_without_touching_the_client(self, wired):
        base, _app, client = wired
        answer = post(base, "/api/missing/fetch", {"dry_run": True})
        assert answer["files"] >= 1
        assert answer["bytes_human"]
        assert client.narrowed == []
        assert client.started == []

    def test_somebody_elses_torrent_is_only_ever_added_to(self, wired):
        """The user's own download of the set, half-way through and not in this
        tool's category: nothing in it may be dropped, however incomplete."""
        base, _app, client = wired
        for entry in client.files_list:
            entry["progress"] = 0.5
        answer = post(base, "/api/missing/fetch", {})
        assert client.narrowed == [], "not ours to narrow"
        assert client.included, "the wanted files were raised"
        assert answer["shared"] is True

    def test_our_own_torrent_is_narrowed_but_nothing_complete_is_dropped(self, wired):
        base, _app, client = wired
        client.category_of = "marquee"
        for entry in client.files_list:                 # a finished, seeding set
            entry["progress"] = 1.0
        post(base, "/api/missing/fetch", {})
        _keep, skip = client.narrowed[-1]
        assert skip == [], "a complete file dropped to priority 0 stops seeding"

    def test_it_starts_the_torrent(self, wired):
        base, _app, client = wired
        post(base, "/api/missing/fetch", {})
        assert client.started

    def test_a_torrent_already_in_the_client_is_not_added_again(self, wired):
        base, _app, client = wired
        post(base, "/api/missing/fetch", {})
        assert client.added == []

    def test_a_release_it_had_to_add_is_narrowed_first(self, wired):
        """qBittorrent gives a newly added torrent every file at normal priority.

        So raising priorities raises nothing, and starting it fetches the whole 1.3 TB
        set. A torrent with nothing downloaded has nothing to protect, so everything
        that was not asked for is skipped.
        """
        base, _app, client = wired
        client.present = False
        post(base, "/api/missing/fetch", {})
        _keep, skip = client.narrowed[-1]
        assert skip, "a fresh add must be narrowed, or it fetches the whole set"

    def test_nothing_missing_is_refused(self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path)
        app.job.plan.missing_roms = []
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/missing/fetch", {})
        assert error.value.code == 400

    def test_without_a_plan_it_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/missing/fetch", {})
        assert error.value.code == 400


class TestFetchingDisks:
    """A disk is fetched when it is nowhere, not when the source folder happens to
    lack it. 199 of the 311 "missing" disks on the library this was found on were
    already in the library, and a fetch would have pulled 120 GB for nothing -- while
    a zip in the source folder whose disk was nowhere was never asked for at all."""

    @pytest.fixture
    def wired(self, server, xml_path, catlist_path, romset):
        base, app = server
        # twodisk's disk: not in the source folder, but in the library already.
        (romset["chd_dir"] / "twodisk" / "ok.chd").unlink()
        # parentchd's disk: nowhere.
        (romset["chd_dir"] / "parentchd" / "pdisk.chd").unlink()
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        for _ in range(200):
            if app.job.state in ("planned", "error"):
                break
            time.sleep(0.05)
        assert app.job.state == "planned", app.job.error
        twodisk = next(item for item in app.job.plan.wanted if item.name == "twodisk")
        library = romset["out_dir"] / twodisk.folder / "twodisk"
        library.mkdir(parents=True)
        (library / "ok.chd").write_bytes(b"x" * 600)
        # Compare again now that the library has something in it.
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        for _ in range(200):
            if app.job.state in ("planned", "error"):
                break
            time.sleep(0.05)
        app.releases["data"] = [
            {"kind": "roms", "version": "0.289", "variant": "non-merged",
             "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
             "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
             "from_version": None, "datfile": None},
            {"kind": "chds", "version": "0.289", "variant": "merged",
             "full_set": True, "name": "MAME 0.289 CHDs (merged)",
             "infohash": "cd" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "cd" + "0" * 38,
             "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms,
                            disks=[("twodisk", "ok"), ("parentchd", "pdisk")])
        app.client = lambda overrides=None: client
        return base, app, client

    def test_a_disk_already_in_the_library_is_not_wanted(self, wired):
        base, app, _client = wired
        plan = app.job.plan
        twodisk = next(item for item in plan.wanted if item.name == "twodisk")
        parentchd = next(item for item in plan.wanted if item.name == "parentchd")
        assert "ok" in twodisk.missing_disks and twodisk.disks_to_fetch == []
        assert parentchd.disks_to_fetch == ["pdisk"]
        payload = get(base, "/api/missing")
        assert payload["disks"] == ["pdisk"]
        # The zip is in the source folder; only the disk is wanted, and the row says so.
        row = next(row for row in payload["rows"] if row["name"] == "parentchd")
        assert row["zip"] is False and row["disks"] == ["pdisk"]
        assert "twodisk" not in {row["name"] for row in payload["rows"]}

    def test_only_the_disk_that_is_nowhere_is_fetched(self, wired):
        base, _app, client = wired
        answer = post(base, "/api/download", {"filters": {}, "dry_run": True})
        chds = next(part for part in answer["parts"] if part["kind"] == "chds")
        # One file: parentchd and the clone that shares its disk name the same one.
        assert chds["files"] == 1
        assert answer["disks"] >= 1
        assert client.narrowed == [] and client.started == []

    def test_the_zip_only_road_does_not_ask_for_zips_it_has(self, wired):
        """/api/missing/fetch narrows the ROM set; a machine on the list for its disk
        alone must not have its zip -- already in the source folder -- re-selected."""
        base, app, _client = wired
        try:
            answer = post(base, "/api/missing/fetch", {"dry_run": True})
        except urllib.error.HTTPError as error:
            assert b"Nothing to fetch" in error.read()
            return
        parentchd = next(item for item in app.job.plan.wanted if item.name == "parentchd")
        assert parentchd.rom_to_fetch is False
        assert answer["files"] < len(app.job.plan.needed)


class FakeClient:
    """A download client that records what was asked of it."""

    # A real set holds 44,166 machines and a selection wants a handful of them, so a
    # stand-in whose every file is wanted would never notice a narrowing that does not
    # narrow.
    BYSTANDERS = ("someoneelse1", "someoneelse2", "someoneelse3")

    def __init__(self, machines, disks=(), present=True):
        self.files_list = [
            {"index": i, "path": f"MAME 0.289 ROMs (non-merged)/{name}.zip",
             "size": 1000, "piece_range": [i, i], "progress": 0.0, "priority": 1}
            for i, name in enumerate(list(machines) + list(self.BYSTANDERS))]
        self.chd_list = [
            {"index": i, "path": f"MAME 0.289 CHDs (merged)/{folder}/{disk}.chd",
             "size": 5000, "piece_range": [i, i], "progress": 0.0, "priority": 1}
            for i, (folder, disk) in enumerate(disks)]
        # Whether the release is already in the client. A torrent this tool did not
        # add may be seeding, and that is what decides whether files may be dropped.
        self.present = present
        # The category the client files it under. Only this tool's own category makes
        # a torrent ours to narrow; anything else is the user's and is only added to.
        self.category_of = None
        self.added, self.included, self.deselected, self.started = [], [], [], []
        self.narrowed, self.deleted = [], []
        # Torrents this client has been handed, by infohash, with their category. The
        # CHD set is only ever there once something added it.
        self.known = {}
        # Whether metadata ever arrives for a magnet added from now on.
        self.metadata_arrives = True

    def _table(self, infohash):
        return self.chd_list if infohash.startswith("cd") else self.files_list

    def one(self, infohash):
        if infohash in self.known:
            category = self.known[infohash]
        elif self.present:
            category = self.category_of
        else:
            return None
        name = "MAME 0.289 ROMs (non-merged)"
        return {"hash": infohash, "name": name, "category": category,
                "save_path": "/data/torrents",
                "content_path": f"/data/torrents/{name}"}

    def properties(self, infohash):
        return {"piece_size": 1000}

    def add(self, *args, **kwargs):
        self.added.append((args, kwargs))
        found = re.search(r"btih:([0-9a-fA-F]{40})", str(args[0]) if args else "")
        infohash = found.group(1) if found else "b3" + "0" * 38
        self.known[infohash] = kwargs.get("category")
        return infohash

    def wait_for_metadata(self, infohash, **kwargs):
        if not self.metadata_arrives:
            raise QBittorrentError(f"No metadata for {infohash[:12]} after 180s.")
        return self._table(infohash)

    def delete(self, infohash, delete_files=False):
        self.deleted.append((infohash, delete_files))
        self.known.pop(infohash, None)

    def files(self, infohash):
        return self._table(infohash)

    def include(self, infohash, indices):
        indices = list(indices)
        self.included.append(indices)
        return {"raised": len(indices), "already_selected": 0}

    def select_only(self, infohash, indices):
        self.deselected.append(list(indices))
        return {"selected": len(indices), "skipped": 0}

    def narrow(self, infohash, indices):
        """Faithful: a complete file is never skipped, whatever was asked for."""
        wanted = set(indices)
        table = self._table(infohash)
        keep = {entry["index"] for entry in table
                if entry["index"] in wanted or entry["progress"] >= 1.0}
        skip = [entry["index"] for entry in table if entry["index"] not in keep]
        for entry in table:
            entry["priority"] = 1 if entry["index"] in keep else 0
        self.narrowed.append((sorted(keep), skip))
        return {"selected": len(keep), "skipped": len(skip),
                "wanted": len(wanted & keep), "kept_complete": len(keep - wanted)}

    def start(self, infohash):
        self.started.append(infohash)

    def ensure_category(self, *args, **kwargs):
        self.category = (args, kwargs)

    def status(self, infohashes=None, category=None):
        """Queue rows for every torrent this client holds, as queue_payload reads them."""
        rows = []
        for infohash, cat in self.known.items():
            table = self._table(infohash)
            size = sum(entry["size"] for entry in table)
            done = sum(int(entry["size"] * entry.get("progress", 0)) for entry in table)
            rows.append({"hash": infohash, "name": infohash, "category": cat,
                         "phase": "complete" if done >= size else "downloading",
                         "state": "", "size": size, "downloaded": done,
                         "total_size": size, "progress": done / size if size else 0,
                         "speed": 0, "eta": 0, "save_path": "/data/torrents"})
        return [row for row in rows if category is None or row["category"] == category]


class TestUpgradePreview:
    """Moving the library to a newer release.

    The thesis: only the machines this library keeps are considered, and only the ones
    whose bytes actually changed are fetched -- so seven releases cost the same as one.
    """

    @pytest.fixture
    def ready(self, server, xml_path, catlist_path, monkeypatch, tmp_path):
        base, app = planned_server(server, xml_path, catlist_path)

        # Two releases: one machine's ROMs change, one machine is new.
        here = tmp_path / "old.xml"
        there = tmp_path / "new.xml"
        here.write_text(_listxml({"goodgame": "aa", "impgame": "bb"}))
        there.write_text(_listxml({"goodgame": "aa", "impgame": "zz", "newgame": "cc"}))
        from marquee.web import application as app_module
        monkeypatch.setattr(app_module.fetch, "fetch_xml",
                            lambda version, **kw: str(here if version == "0.282" else there))

        app.releases["data"] = [
            {"kind": "roms", "version": v, "variant": "non-merged", "full_set": True,
             "name": f"MAME {v} ROMs (non-merged)", "infohash": "b3" + "0" * 38,
             "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
             "from_version": None, "datfile": None}
            for v in ("0.282", "0.289")]
        # Where the library stands today; without it there is nothing to move from.
        post(base, "/api/save", {"mame_version": "0.282"})
        return base, app

    def wait(self, base):
        for _ in range(200):
            payload = get(base, "/api/upgrade")
            if not payload["running"]:
                return payload
            time.sleep(0.05)
        raise AssertionError("the preview never finished")

    def test_it_lists_what_is_available(self, ready):
        base, _app = ready
        assert get(base, "/api/upgrade")["available"] == ["0.289", "0.282"]

    def test_it_separates_unchanged_from_changed_and_new(self, ready):
        base, _app = ready
        post(base, "/api/upgrade/preview", {"version": "0.289"})
        data = self.wait(base)["data"]
        assert data["unchanged"] >= 1
        assert data["changed"] >= 1
        assert "impgame" in data["fetch"]
        # A machine whose bytes did not move is never fetched again.
        assert "goodgame" not in data["fetch"]

    def test_a_release_that_changes_nothing_costs_nothing(self, ready):
        base, _app = ready
        post(base, "/api/upgrade/preview", {"version": "0.282"})
        data = self.wait(base)["data"]
        assert data["fetch"] == [] or "goodgame" not in data["fetch"]

    def test_it_needs_a_target(self, ready):
        base, _app = ready
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/upgrade/preview", {})
        assert error.value.code == 400

    def test_it_needs_a_plan(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/upgrade/preview", {"version": "0.289"})
        assert error.value.code == 400

    def test_the_delta_can_be_handed_straight_to_the_client(self, ready):
        base, app = ready
        post(base, "/api/upgrade/preview", {"version": "0.289"})
        data = self.wait(base)["data"]
        client = FakeClient(data["fetch"])
        app.client = lambda overrides=None: client
        post(base, "/api/missing/fetch", {"machines": data["fetch"]})
        assert len(client.included[-1]) == len(data["fetch"])


def _listxml(machines):
    body = "".join(
        f'<machine name="{name}"><description>{name}</description>'
        f'<driver status="good" emulation="good"/><display type="raster"/>'
        f'<rom name="{name}.bin" size="8" sha1="{digest}"/></machine>'
        for name, digest in machines.items())
    return f'<?xml version="1.0"?><mame build="0.289 (mame0289)">{body}</mame>'


class TestReleaseWatch:
    """Noticing that a newer MAME is published.

    Worth a line on every page, because acting on it costs a few gigabytes rather
    than an afternoon.
    """

    def watch(self, base, app, releases, library_version):
        app.releases.update(data=[
            {"kind": "roms", "version": version, "variant": "non-merged",
             "full_set": True, "name": f"MAME {version} ROMs", "infohash": "b" * 40,
             "magnet": "", "from_version": None, "datfile": None}
            for version in releases], fetched_at=time.time(), running=False, error=None)
        post(base, "/api/save", {"mame_version": library_version})
        return get(base, "/api/state")["release_watch"]

    def test_a_newer_release_is_flagged(self, server):
        base, app = server
        found = self.watch(base, app, ["0.289", "0.290"], "0.289")
        assert found["newest"] == "0.290"
        assert found["newer"] is True

    def test_being_up_to_date_is_not(self, server):
        base, app = server
        assert self.watch(base, app, ["0.289"], "0.289")["newer"] is False

    def test_a_library_ahead_of_the_listing_is_not_flagged(self, server):
        base, app = server
        assert self.watch(base, app, ["0.282"], "0.289")["newer"] is False

    def test_versions_compare_as_numbers_not_strings(self, server):
        base, app = server
        # "0.9" sorts after "0.289" as a string; it is not a newer MAME.
        assert self.watch(base, app, ["0.289"], "0.9")["newer"] is True
        assert self.watch(base, app, ["0.9"], "0.289")["newer"] is False

    def test_no_listing_yet_says_nothing(self, server):
        base, app = server
        app.releases.update(data=None, running=True, error=None)
        found = get(base, "/api/state")["release_watch"]
        assert found["newer"] is False


class TestArtEdges:
    def test_a_nameless_picture_is_a_404(self, server):
        base, _app = server
        # Without the guard this put None into a Location header and crashed.
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/art/Named_Titles/.png", timeout=10)
        assert error.value.code == 404

    def test_a_path_cannot_climb_out_of_the_cache(self, server):
        base, _app = server
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(base + "/art/Named_Titles/..%2f..%2fsettings.ini.png",
                        timeout=10)
        except urllib.error.HTTPError as error:
            # Either refused, or redirected out to the source -- never a local file.
            assert error.code in (302, 404)
            if error.code == 302:
                assert "libretro-thumbnails" in error.headers["Location"]


class TestMissingPricing:
    """"Unknown" and "zero" are different answers.

    Without a download client the sizes cannot be known. With one, a zero means the
    release genuinely does not carry those machines -- which is the honest answer for
    a handful of MAME entries that have no ROM zip at all.
    """

    def test_no_client_means_unknown(self, server, xml_path, catlist_path):
        base, _app = planned_server(server, xml_path, catlist_path)
        assert get(base, "/api/missing")["priced"] is False

    def test_a_client_that_answers_prices_it(self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path)
        app.torrent_sizes = lambda: {name: 1234 for name in app.job.plan.missing_roms}
        payload = get(base, "/api/missing")
        assert payload["priced"] is True
        assert payload["bytes"] == 1234 * payload["total"]

    def test_a_configured_client_is_reported_even_when_it_has_nothing(
            self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path,
                                   download_client="http://nas:8080/")
        app.torrent_sizes = lambda: {}
        payload = get(base, "/api/missing")
        # "No client" and "a client that does not hold the release" are different
        # problems and need different words.
        assert payload["client"] is True
        assert payload["priced"] is False

    def test_no_client_at_all_says_so(self, server, xml_path, catlist_path):
        base, _app = planned_server(server, xml_path, catlist_path)
        assert get(base, "/api/missing")["client"] is False


class TestCatalogue:
    """A machine with nothing on disk still has to be visible and choosable.

    On a fresh install that is every machine there is: a library page that only lists
    what has already been downloaded shows an empty screen and offers no way out of
    it.
    """

    @pytest.fixture
    def planned(self, server, xml_path, catlist_path):
        return planned_server(server, xml_path, catlist_path)

    def test_machines_with_no_files_are_listed(self, planned):
        base, app = planned
        names = {row["name"] for row in get(base, "/api/machines?limit=500")["rows"]}
        # betamax survives the filters and has no zip on disk.
        assert "betamax" in names
        assert "betamax" in app.job.plan.missing_roms

    def test_they_are_marked_rather_than_mixed_in(self, planned):
        base, _app = planned
        rows = {row["name"]: row for row in get(base, "/api/machines?limit=500")["rows"]}
        assert rows["betamax"]["here"] is False
        assert rows["betamax"]["status"] == "missing"
        assert rows["goodgame"]["here"] is True

    def test_the_view_can_be_narrowed_to_either(self, planned):
        base, app = planned
        here = get(base, "/api/machines?have=yes&limit=500")
        missing = get(base, "/api/machines?have=no&limit=500")
        assert here["total"] == len(app.job.plan.items)
        assert missing["total"] == len(app.job.plan.absent_items)
        assert here["total"] + missing["total"] == \
            get(base, "/api/machines?limit=500")["total"]

    def test_a_detail_page_opens_for_one_that_is_not_here(self, planned):
        base, _app = planned
        found = get(base, "/api/machine?name=betamax")
        assert found["description"]
        assert found["status"] == "missing"

    def test_every_category_is_offered_even_with_nothing_downloaded(self, planned):
        base, _app = planned
        plan = get(base, "/api/state")["plan"]
        # `machines` is what could be copied today; `wanted` is what the selection
        # asks for, and it is what a genre picker has to count.
        assert plan["wanted"] >= plan["machines"]
        assert any(entry["wanted"] for entry in plan["categories"])
        # Excluded categories are still counted, so their weight can be shown; what is
        # left is exactly what the plan covers.
        assert sum(entry["wanted"] for entry in plan["categories"]
                   if not entry["excluded"]) == plan["wanted"]

    def test_sizes_are_unknown_until_the_release_has_been_read(self, planned):
        base, app = planned
        assert app.release_sizes() == {}
        row = next(r for r in get(base, "/api/machines?have=no&limit=500")["rows"]
                   if r["name"] == "betamax")
        assert row["bytes"] == 0


class TestDownloadSelection:
    """Fetching exactly what the library page is showing.

    The filters are resolved on the server so the list that is fetched is built by the
    same code that built the list being looked at.
    """

    @pytest.fixture
    def wired(self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path)
        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms, present=False)
        app.client = lambda overrides=None: client
        return base, app, client

    def test_a_dry_run_prices_it_and_selects_nothing(self, wired):
        base, _app, client = wired
        answer = post(base, "/api/download", {"filters": {}, "dry_run": True})
        assert answer["bytes"] > 0
        assert answer["machines"] >= 1
        assert client.started == []

    def test_pricing_a_set_it_had_to_add_leaves_it_narrowed_and_stopped(self, wired):
        """Otherwise anyone who presses play in qBittorrent gets all 1.3 TB.

        A torrent added only to read its file table arrives with every file selected.
        It is stopped, so it does nothing on its own -- but it is left holding a
        selection nobody asked for, and that is a loaded gun.
        """
        base, _app, client = wired
        post(base, "/api/download", {"filters": {}, "dry_run": True})
        assert client.narrowed, "a set added just to price it must not stay selected"
        assert client.started == []

    def test_a_torrent_with_nothing_downloaded_is_narrowed_to_the_selection(self, wired):
        base, _app, client = wired
        post(base, "/api/download", {"filters": {}})
        keep, skip = client.narrowed[-1]
        assert skip, "without this the client fetches the whole 1.3 TB set"
        assert keep
        assert client.started

    def test_a_finished_set_never_loses_a_file(self, wired):
        """The hazard, in one test. The release may be complete and seeding, and
        dropping any of it to priority 0 stops that -- on 1.3 TB, irrecoverably."""
        base, _app, client = wired
        client.present = True
        for entry in client.files_list:
            entry["progress"] = 1.0
        post(base, "/api/download", {"filters": {}})
        assert client.narrowed == [], "somebody else's torrent is never narrowed"
        assert client.included, "wanted files are raised, nothing is lowered"
        assert client.added == []

    def test_a_finished_set_of_our_own_keeps_every_complete_file(self, wired):
        base, _app, client = wired
        client.present = True
        client.category_of = "marquee"
        for entry in client.files_list:
            entry["progress"] = 1.0
        post(base, "/api/download", {"filters": {}})
        _keep, skip = client.narrowed[-1]
        assert skip == []

    def test_the_filter_decides_what_is_fetched(self, wired):
        base, app, _client = wired
        everything = post(base, "/api/download",
                          {"filters": {}, "dry_run": True})
        genre = app.job.plan.absent_items[0].genre
        narrowed = post(base, "/api/download",
                        {"filters": {"genre": genre}, "dry_run": True})
        assert 0 < narrowed["machines"] <= everything["machines"]

    def test_a_filter_matching_nothing_is_refused(self, wired):
        base, _app, _client = wired
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/download", {"filters": {"genre": "No Such Genre"}})
        assert error.value.code == 400

    def test_asking_for_what_is_already_here_costs_nothing(self, wired):
        base, _app, client = wired
        answer = post(base, "/api/download", {"filters": {"have": "yes"}})
        assert answer["nothing"] is True
        assert client.narrowed == [] and client.started == []

    def test_the_client_decides_where_it_goes(self, wired):
        """Handing qBittorrent a path as this process sees it scatters the set.

        Where downloads land is configured in the client, per category. What has to
        come back is where they landed, because the library is built from it.
        """
        base, _app, client = wired
        answer = post(base, "/api/download", {"filters": {}})
        assert client.added
        assert all(not kwargs.get("save_path") for _args, kwargs in client.added)
        assert answer["parts"][0]["path"] == "/data/torrents/MAME 0.289 ROMs (non-merged)"

    def test_an_explicit_list_is_honoured(self, wired):
        base, app, _client = wired
        name = app.job.plan.absent_items[0].name
        answer = post(base, "/api/download",
                      {"machines": [name], "dry_run": True})
        assert answer["machines"] == 1

    def test_a_stray_filter_key_is_ignored_rather_than_an_error(self, wired):
        base, _app, _client = wired
        answer = post(base, "/api/download",
                      {"filters": {"nonsense": "x"}, "dry_run": True})
        assert answer["machines"] >= 1

    def test_without_a_plan_it_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/download", {"filters": {}})
        assert error.value.code == 400

    def test_a_set_that_has_none_of_them_is_still_disarmed(self, wired):
        """Routine for the CHD set: the wanted disks are not in it yet. It was added
        to find that out, and used to be left with every file selected."""
        base, app, client = wired
        client.files_list[:] = [
            {"index": i, "path": f"MAME 0.289 ROMs (non-merged)/other{i}.zip",
             "size": 1000, "piece_range": [i, i], "progress": 0.0, "priority": 1}
            for i in range(3)]
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/download", {"filters": {}, "dry_run": True})
        assert b"has none of them" in error.value.read()
        keep, skip = client.narrowed[-1]
        assert len(keep) == 1 and len(skip) == 2
        assert client.started == []


class TestPricingTheSet:
    """Reading the release's file table is metadata only -- and must stay that way."""

    @pytest.fixture
    def wired(self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path)
        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms, present=False)
        app.client = lambda overrides=None: client
        return base, app, client

    def wait(self, app):
        for _ in range(200):
            if not app.catalogue["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("the catalogue never finished")

    def test_it_learns_what_every_machine_weighs(self, wired):
        base, app, _client = wired
        post(base, "/api/catalogue", {})
        self.wait(app)
        assert app.catalogue["error"] is None
        assert app.catalogue["roms"]
        sizes = app.release_sizes()
        assert sizes[app.job.plan.absent_items[0].name] > 0

    def test_it_survives_a_restart(self, wired, tmp_path, monkeypatch):
        """Learning it costs two torrents' metadata and half a minute of waiting."""
        base, app, _client = wired
        monkeypatch.setattr("marquee.sources.cache_dir", lambda: str(tmp_path))
        post(base, "/api/catalogue", {})
        self.wait(app)
        reborn = Application(app.settings_path)
        assert reborn.catalogue["roms"] == app.catalogue["roms"]
        assert reborn.catalogue["rom_files"] == app.catalogue["rom_files"]

    def test_a_set_it_added_is_left_holding_one_small_file(self, wired):
        """Stopped is not enough: pressing play in qBittorrent would fetch 1.3 TB."""
        base, app, client = wired
        post(base, "/api/catalogue", {})
        self.wait(app)
        keep, skip = client.narrowed[-1]
        assert len(keep) == 1
        assert skip
        assert client.started == []

    def test_a_listed_chd_set_is_priced_and_disarmed_too(self, wired):
        """Reading the CHD set crashed on an unpacking error the thread swallowed:
        no prices, no error, and a 1.3 TB set left with every file selected."""
        base, app, client = wired
        client.chd_list[:] = [
            {"index": i, "path": f"MAME 0.289 CHDs (merged)/{folder}/{disk}.chd",
             "size": 5000, "piece_range": [i, i], "progress": 0.0, "priority": 1}
            for i, (folder, disk) in enumerate([("area51", "area51"),
                                                ("kinst", "kinst"), ("gauntdl", "gdl")])]
        chd_hash = "cd" + "0" * 38
        app.releases["data"].append({
            "kind": "chds", "version": "0.289", "variant": "merged",
            "full_set": True, "name": "MAME 0.289 CHDs (merged)",
            "infohash": chd_hash, "magnet": "magnet:?xt=urn:btih:" + chd_hash,
            "from_version": None, "datfile": None})
        post(base, "/api/catalogue", {})
        self.wait(app)
        assert app.catalogue["error"] is None
        assert app.catalogue["disk_files"] == 3
        assert [entry["priority"] for entry in client.chd_list].count(1) == 1

    def test_an_unexpected_failure_is_reported_not_swallowed(self, wired):
        base, app, client = wired

        def broken(infohash):
            raise RuntimeError("boom")
        client.files = broken
        client.wait_for_metadata = lambda infohash, **kwargs: []
        post(base, "/api/catalogue", {})
        self.wait(app)
        assert "boom" in (app.catalogue["error"] or "")

    def test_a_set_whose_metadata_never_came_is_taken_back_out(self, wired):
        """Nothing can be narrowed before the file table is known, and once it is,
        the set sits there with everything selected. It was added a moment ago by
        this call, so it is removed -- without its files, of which there are none."""
        base, app, client = wired
        client.metadata_arrives = False
        post(base, "/api/catalogue", {})
        self.wait(app)
        assert "metadata" in (app.catalogue["error"] or "")
        assert client.deleted == [("b3" + "0" * 38, False)]

    def test_a_set_left_fully_selected_by_an_earlier_call_is_disarmed(self, wired):
        """In our category, every file selected, nothing fetched: an earlier call
        added it and never narrowed it. That this call did not add it is no reason
        to leave 1.3 TB one click away."""
        base, app, client = wired
        client.present = True
        client.category_of = "marquee"
        post(base, "/api/catalogue", {})
        self.wait(app)
        assert client.added == []
        keep, _skip = client.narrowed[-1]
        assert len(keep) == 1

    def test_a_set_of_ours_already_narrowed_is_left_as_it_is(self, wired):
        base, app, client = wired
        client.present = True
        client.category_of = "marquee"
        client.files_list[0]["priority"] = 0
        post(base, "/api/catalogue", {})
        self.wait(app)
        assert client.narrowed == []


class TestWhereItLands:
    """The library is built from what the client writes, so where that is has to
    cross back -- and whether this process can see it."""

    @pytest.fixture
    def wired(self, server, xml_path, catlist_path, tmp_path):
        base, app = planned_server(server, xml_path, catlist_path)
        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms, present=False)
        app.client = lambda overrides=None: client
        return base, app, client, tmp_path

    def test_a_folder_that_does_not_exist_here_is_flagged(self, wired):
        base, _app, _client, _tmp = wired
        answer = post(base, "/api/download", {"filters": {}, "dry_run": True})
        assert answer["parts"][0]["path_visible"] is False

    def test_a_torrent_folder_that_has_not_been_created_yet_is_not_flagged(self, wired):
        """It only appears once the download starts; the folder above it is the test."""
        base, app, _client, tmp = wired
        app.current_config = lambda: configuration.load(app.settings_path).with_overrides(
            remote_path_mappings=f"/data/torrents -> {tmp}")
        answer = post(base, "/api/download", {"filters": {}, "dry_run": True})
        part = answer["parts"][0]
        assert part["path"] == str(tmp / "MAME 0.289 ROMs (non-merged)")
        assert part["path_visible"] is True


class TestWhereADownloadLands:
    """qBittorrent's path and this app's path to one folder are often different, and
    a download this app cannot see finishes and then goes nowhere. Both the client
    Test and every fetch answer say where it lands and whether that is visible."""

    def test_a_fetch_answer_carries_both_paths(self, server, xml_path, catlist_path):
        base, app = planned_server(server, xml_path, catlist_path)
        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms)
        app.client = lambda overrides=None: client
        answer = post(base, "/api/missing/fetch", {"dry_run": True})
        assert answer["client_path"] == "/data/torrents/MAME 0.289 ROMs (non-merged)"
        assert answer["path_visible"] is False, "nothing is mounted at /data/torrents here"
        assert "path" in answer

    def test_the_client_test_names_the_landing_folder(self, server, tmp_path):
        base, app = server

        class Probe:
            def test(self):
                return {"app_version": "v5.0", "api_version": "2.11", "save_path": "/data/torrents"}

            def categories(self):
                return {"marquee": {"name": "marquee", "savePath": "/data/torrents/marquee"}}

            def status(self, category=None):
                return []

        app.client = lambda overrides=None: Probe()
        (tmp_path / "torrents").mkdir()
        answer = post(base, "/api/client/test", {
            "download_client": "http://x/",
            "remote_path_mappings": f"/data/torrents -> {tmp_path / 'torrents'}"})
        assert answer["landing"] == "/data/torrents/marquee"
        assert answer["local"] == str(tmp_path / "torrents" / "marquee")
        assert answer["visible"] is True
        assert "Visible from here" in answer["message"]

        blind = post(base, "/api/client/test", {"download_client": "http://x/",
                                                 "remote_path_mappings": ""})
        assert blind["visible"] is False
        assert "NOT visible" in blind["message"]


class TestFindingTheTorrentFolderByName:
    """qBittorrent calls it /data/torrents/MAME 0.289 ROMs (non-merged); this container
    calls it /downloads/MAME 0.289 ROMs (non-merged). Nobody should have to type that."""

    def test_locate_falls_back_to_the_name_under_the_download_folder(self, tmp_path):
        from marquee import acquisition
        (tmp_path / "MAME 0.289 ROMs (non-merged)").mkdir()
        client_path = "/data/torrents/MAME 0.289 ROMs (non-merged)"
        assert acquisition.locate(client_path, [], str(tmp_path)) == \
            str(tmp_path / "MAME 0.289 ROMs (non-merged)")
        assert acquisition.inferred_mapping(client_path, str(tmp_path)) == ("/data/torrents", str(tmp_path))
        # A written mapping still wins, and nothing is invented when the name is absent.
        assert acquisition.locate(client_path, [("/data/torrents", "/elsewhere")], str(tmp_path)) == \
            "/elsewhere/MAME 0.289 ROMs (non-merged)"
        assert acquisition.locate("/data/torrents/other", [], str(tmp_path)) == "/data/torrents/other"

    def test_a_fetch_is_visible_once_the_folder_exists_here(self, server, xml_path, catlist_path, tmp_path):
        # The download folder is only kept in settings.ini beside a client.
        base, app = planned_server(server, xml_path, catlist_path,
                                   download_client="http://x/", download_dir=str(tmp_path))
        (tmp_path / "MAME 0.289 ROMs (non-merged)").mkdir()
        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms)
        app.client = lambda overrides=None: client
        answer = post(base, "/api/missing/fetch", {"dry_run": True})
        assert answer["path"] == str(tmp_path / "MAME 0.289 ROMs (non-merged)")
        assert answer["path_visible"] is True

    def test_the_category_can_be_sent_back_to_the_default_folder(self, server):
        base, app = server
        calls = []

        class Probe:
            def set_category_path(self, name, path=""):
                calls.append((name, path))

        app.client = lambda overrides=None: Probe()
        answer = post(base, "/api/client/category/reset", {"download_client": "http://x/"})
        assert calls == [("marquee", "")]
        assert answer["ok"]


class TestWhyATorrentFailed:
    def test_the_reason_from_the_log_rides_on_the_queue_row(self, server):
        base, app = server
        post(base, "/api/save", {"download_client": "http://x/"})

        class Probe:
            def status(self, category=None):
                return [{"hash": "b3" * 20, "name": "MAME 0.289 ROMs (non-merged)",
                         "state": "error", "phase": "failed", "progress": 0.0,
                         "downloaded": 0, "size": 10, "total_size": 10, "speed": 0,
                         "eta": 8640000, "save_path": "/downloads",
                         "content_path": "/downloads/MAME 0.289 ROMs (non-merged)",
                         "category": "marquee", "seeds": 0, "peers": 0}]

            def recent_errors(self):
                return {"MAME 0.289 ROMs (non-merged)":
                        "MAME 0.289 ROMs (non-merged) file_open (/downloads/x.zip) error: Permission denied"}

        app.client = lambda overrides=None: Probe()
        app._queue_cache = {"at": 0.0, "data": None}
        row = get(base, "/api/queue")["torrents"][0]
        assert row["phase"] == "failed"
        assert "Permission denied" in row["error_message"]

    def test_the_log_is_parsed_into_names_and_reasons(self):
        from marquee.download.qbittorrent import QBittorrent
        import json as _json
        client = QBittorrent.__new__(QBittorrent)
        client._call = lambda path, data=None, files=None: _json.dumps([
            {"message": 'File error alert. Torrent: "A". File: "/x". Reason: "A file_open (/x) error: Permission denied"'},
            {"message": 'Failed to remove partfile. Torrent: "B". Reason: "Permission denied".'},
            {"message": "Something unrelated"}])
        found = client.recent_errors()
        assert found["A"].endswith("Permission denied")
        assert found["B"] == "Permission denied"
        assert len(found) == 2


class TestADownloadIsAJob:
    """Activity used to show a torrent at 63%. What someone asked for was 212 games:
    how many are in, what is still coming, and why some never will from this set."""

    @pytest.fixture
    def wired(self, server, xml_path, catlist_path, tmp_path, monkeypatch):
        monkeypatch.setattr("marquee.sources.cache_dir", lambda: str(tmp_path))
        base, app = planned_server(server, xml_path, catlist_path,
                                   download_client="http://fake:8080/")
        app.releases["data"] = [{
            "kind": "roms", "version": "0.289", "variant": "non-merged",
            "full_set": True, "name": "MAME 0.289 ROMs (non-merged)",
            "infohash": "b3" + "0" * 38, "magnet": "magnet:?xt=urn:btih:" + "b3" + "0" * 38,
            "from_version": None, "datfile": None}]
        client = FakeClient(app.job.plan.missing_roms, present=False)
        app.client = lambda overrides=None: client
        return base, app, client

    def queue(self, app):
        app._queue_cache = {"at": 0.0, "data": None}
        return app.queue_payload()

    def test_a_started_download_is_remembered_as_its_games(self, wired):
        base, app, _client = wired
        post(base, "/api/download", {"filters": {}})
        jobs = self.queue(app)["jobs"]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["games"] == len(app.job.plan.missing_roms)
        assert job["games_done"] == 0 and job["state"] == "waiting"
        assert {row["game"] for row in job["arriving"]} <= set(app.job.plan.missing_roms)

    def test_a_price_is_not_a_download(self, wired):
        base, app, _client = wired
        post(base, "/api/download", {"filters": {}, "dry_run": True})
        assert self.queue(app)["jobs"] == []

    def test_games_count_in_as_their_files_finish(self, wired):
        base, app, client = wired
        post(base, "/api/download", {"filters": {}})
        first = app.job.plan.missing_roms[0]
        for entry in client.files_list:
            if entry["path"].endswith(f"/{first}.zip"):
                entry["progress"] = 1.0
        job = self.queue(app)["jobs"][0]
        assert job["games_done"] == 1
        assert job["state"] == ("complete" if job["games"] == 1 else "downloading")
        assert first not in {row["game"] for row in job["arriving"]}

    def test_a_finished_download_since_the_plan_asks_for_a_new_plan(
            self, wired, xml_path, catlist_path):
        base, app, client = wired
        post(base, "/api/download", {"filters": {}})
        for entry in client.files_list:
            entry["progress"] = 1.0
        queue = self.queue(app)
        assert queue["jobs"][0]["state"] == "complete"
        assert queue["jobs"][0]["completed_at"]
        assert queue["finished_since_plan"]["games"] == len(app.job.plan.missing_roms)
        # The files land in the torrent folder; planning again takes them in, and the
        # suggestion goes away -- because the plan has them, not because time passed.
        import zipfile
        rom_dir = app.current_config().rom_dir
        for name in app.job.plan.missing_roms:
            with zipfile.ZipFile(os.path.join(rom_dir, f"{name}.zip"), "w") as archive:
                archive.writestr(f"{name}.bin", b"rom" * 100)
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        for _ in range(200):
            if app.job.state in ("planned", "error"):
                break
            time.sleep(0.05)
        assert self.queue(app)["finished_since_plan"]["games"] == 0

    def test_a_plan_that_already_has_the_files_asks_for_nothing(self, wired):
        """A restart plans straight away, and only then notices the download finished:
        a clock would call that "arrived since the plan". The plan has them."""
        base, app, client = wired
        post(base, "/api/download", {"filters": {}})
        for entry in client.files_list:
            entry["progress"] = 1.0
        from types import SimpleNamespace
        real = app.job.plan
        app.job.plan = SimpleNamespace(needed=[], **{k: getattr(real, k) for k in ("sync",)})
        try:
            assert self.queue(app)["finished_since_plan"]["games"] == 0
        finally:
            app.job.plan = real

    def test_a_disk_counts_for_the_machine_that_asked_not_its_folder(self):
        from marquee.web import fetches
        # warfaa's own disk is filed under its parent in a merged set.
        assert fetches.game_of("MAME 0.288 CHDs (merged)/warfa/warfaa.chd", "chds",
                               {"warfaa": "warfaa"}) == "warfaa"
        assert fetches.game_of("MAME 0.288 CHDs (merged)/warfa/warfaa.chd", "chds") == "warfa"
        assert fetches.game_of("MAME 0.289 ROMs (non-merged)/warfaa.zip", "roms") == "warfaa"

    def test_disks_the_set_does_not_carry_are_explained(self):
        from marquee.web import fetches
        part = {"kind": "chds", "release": "MAME 0.288 CHDs (merged)", "version": "0.288",
                "infohash": "cd" * 20, "files": [],
                "missing": ["konam80a/826aaa01", "gtfrk11m/886_d02"]}
        view = fetches._view({"id": "x", "parts": [part]}, "0.289")
        assert view["missing"][0]["count"] == 2
        assert view["missing"][0]["names"] == ["826aaa01", "886_d02"]
        assert "0.288" in view["missing"][0]["why"] and "later CHD set" in view["missing"][0]["why"]


class TestArrivedIsAskedOfTheFiles:
    """warfab's zip arrived; its disk warfa15 is in no CHD set yet. The plan still
    needs warfab -- for the disk -- and counting the game asked for a new plan for
    ever. Only what this download brought, and the plan still wants, counts."""

    def mixin(self, tmp_path, monkeypatch, items):
        import threading
        from types import SimpleNamespace
        from marquee.web.fetches import FetchesMixin
        monkeypatch.setattr("marquee.sources.cache_dir", lambda: str(tmp_path))
        host = FetchesMixin()
        host._task_lock = threading.Lock()
        host._queue_cache = {}
        host.job = SimpleNamespace(plan=SimpleNamespace(wanted=items))
        host._write_fetches([{"id": "x", "completed_at": 1.0, "parts": [
            {"kind": "roms", "release": "R", "infohash": "a" * 40,
             "files": [{"index": 0, "path": "R/warfab.zip", "size": 10, "game": "warfab"}]},
            {"kind": "chds", "release": "C", "infohash": "c" * 40,
             "files": [{"index": 0, "path": "C/warfa/warfaa.chd", "size": 20, "game": "warfaa"}]}]}])
        return host

    def item(self, name, rom_to_fetch, disks):
        from types import SimpleNamespace
        return SimpleNamespace(name=name, rom_to_fetch=rom_to_fetch, disks_to_fetch=disks)

    def test_a_zip_taken_in_with_its_disk_nowhere_is_not_asked_again(self, tmp_path, monkeypatch):
        host = self.mixin(tmp_path, monkeypatch, [self.item("warfab", False, ["warfa15"]),
                                                  self.item("warfaa", False, [])])
        assert host.finished_since_plan()["games"] == 0

    def test_what_the_plan_still_wants_is(self, tmp_path, monkeypatch):
        host = self.mixin(tmp_path, monkeypatch, [self.item("warfab", True, ["warfa15"]),
                                                  self.item("warfaa", False, ["warfaa"])])
        arrived = host.finished_since_plan()
        assert arrived["games"] == 2 and arrived["bytes"] == 30
