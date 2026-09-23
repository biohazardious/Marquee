"""The ignore list: files in the library that are not Marquee's to touch.

A BIOS pack drops neogeo.zip, pgm.zip, cpzn1.zip and the like at the top of a
Batocera library for the older libretro cores. To the diff they were 16 files nothing
wants, one tick away from deletion.
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from marquee import catalog, ignore, pipeline, plan as planning
from marquee import config as configuration
from tests.conftest import ROM_BYTES


class TestThePatterns:
    def test_a_star_never_crosses_a_folder(self):
        compiled = ignore.patterns(["*.zip"])
        assert ignore.matches("neogeo.zip", compiled)
        assert not ignore.matches("Fighter/Versus/sfiii3.zip", compiled)

    def test_a_folder_pattern_matches_what_sits_directly_in_it(self):
        compiled = ignore.patterns(["Unlisted/*"])
        assert ignore.matches("Unlisted/nocat.zip", compiled)
        assert not ignore.matches("Unlisted/dlair/disk.chd", compiled)

    def test_case_and_leading_slashes_do_not_matter(self):
        compiled = ignore.patterns(["/NeoGeo.ZIP", "./pgm.zip", "  ", "neogeo.zip"])
        assert compiled == ["neogeo.zip", "pgm.zip"]
        assert ignore.matches("NEOGEO.zip", compiled)

    def test_a_path_the_selection_wants_is_never_ignored(self):
        existing = {"neogeo.zip": 10, "Maze/Misc/goodgame.zip": 300}
        kept, ignored = ignore.split(existing, ignore.patterns(["*", "Maze/Misc/*"]),
                                     {"Maze/Misc/goodgame.zip"})
        assert ignored == ["neogeo.zip"]
        assert kept == {"Maze/Misc/goodgame.zip": 300}


class TestAPlan:
    @pytest.fixture
    def library(self, categorised, config, romset):
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        pipeline.execute(built, config)
        out = romset["out_dir"]
        (out / "neogeo.zip").write_bytes(b"bios")
        (out / "Maze" / "Misc" / "stray.zip").write_bytes(b"stray")
        return categorised, out

    def plan(self, categorised, config):
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        built.sync = pipeline.compare_destination(built, config)
        return built

    def test_an_ignored_file_is_not_in_the_diff(self, library, config):
        categorised, _out = library
        config.ignore_paths = ["*.zip"]
        built = self.plan(categorised, config)
        orphans = [action.relpath for action in built.sync.of("orphan")]
        assert orphans == ["Maze/Misc/stray.zip"]
        assert built.sync.ignored == ["neogeo.zip"]

    def test_deleting_what_nothing_wants_leaves_it(self, library, config):
        categorised, out = library
        config.ignore_paths = ["neogeo.zip"]
        built = self.plan(categorised, config)
        pipeline.execute(built, config, delete_orphans=True)
        assert (out / "neogeo.zip").read_bytes() == b"bios"
        assert not (out / "Maze" / "Misc" / "stray.zip").exists()

    def test_an_ignored_file_is_never_moved_into_the_library(self, library, config):
        """Without the list, a stray copy of a wanted zip is taken as a move."""
        categorised, out = library
        (out / "Maze" / "Misc" / "goodgame.zip").unlink()
        (out / "goodgame.zip").write_bytes(ROM_BYTES)
        built = self.plan(categorised, config)
        assert any(action.from_relpath == "goodgame.zip" for action in built.sync.of("move"))
        config.ignore_paths = ["*.zip"]
        built = self.plan(categorised, config)
        assert not built.sync.of("move")
        pipeline.execute(built, config)
        assert (out / "goodgame.zip").exists(), "the user's copy stayed where it was"
        assert (out / "Maze" / "Misc" / "goodgame.zip").exists()

    def test_the_list_survives_the_settings_file(self, tmp_path):
        written = configuration.Config(rom_dir=str(tmp_path), copy_path=str(tmp_path),
                                       ignore_paths=["*.zip", "Unlisted/dlair.chd"])
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, written)
        assert configuration.read_settings_file(path).ignore_paths == \
            ["*.zip", "Unlisted/dlair.chd"]


class TestTheTransferPage:
    """"Ignore" on a file in the delete list: saved, and applied without replanning."""

    @pytest.fixture
    def server(self, tmp_path, config, romset, xml_path, catlist_path):
        from marquee.web.server import Application, Handler
        config.settings_path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(config.settings_path, config)
        app = Application(config.settings_path)
        handler = type("Bound", (Handler,), {"app": app})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait(app, "planned")
        post(base, "/api/copy", {})
        wait(app, "done")
        (romset["out_dir"] / "neogeo.zip").write_bytes(b"bios")
        (romset["out_dir"] / "pgm.zip").write_bytes(b"bios")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait(app, "planned")
        yield base, app
        httpd.shutdown()
        httpd.server_close()

    def test_top_level_files_are_offered_and_then_left_alone(self, server):
        base, app = server
        changes = get(base, "/api/changes")
        assert sorted(changes["top_level_deletions"]) == ["neogeo.zip", "pgm.zip"]
        answer = post(base, "/api/ignore", {"paths": changes["top_level_deletions"]})
        assert answer["ignored"] == 2
        changes = get(base, "/api/changes")
        assert changes["top_level_deletions"] == [] and changes["ignored"] == 2
        assert {entry["kind"]: entry["files"] for entry in changes["kinds"]}["orphan"] == 0
        assert sorted(app.current_config().ignore_paths) == ["neogeo.zip", "pgm.zip"]
        # The plan now is what the saved settings would build: not "out of date".
        built = get(base, "/api/state")["plan"]["built_from"]
        assert sorted(built["ignore_paths"]) == ["neogeo.zip", "pgm.zip"]

    def test_asking_twice_adds_nothing_twice(self, server):
        base, app = server
        post(base, "/api/ignore", {"paths": ["neogeo.zip"]})
        post(base, "/api/ignore", {"paths": ["/NEOGEO.zip"]})
        assert app.current_config().ignore_paths == ["neogeo.zip"]

    def test_nothing_named_is_refused(self, server):
        base, _app = server
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base, "/api/ignore", {"paths": []})
        assert error.value.code == 400


    def test_not_while_a_job_runs_and_nothing_is_saved(self, server):
        base, app = server
        app.job.state = "copying"
        try:
            with pytest.raises(urllib.error.HTTPError) as error:
                post(base, "/api/ignore", {"paths": ["neogeo.zip"]})
            assert error.value.code == 400
            assert app.current_config().ignore_paths == []
        finally:
            app.job.state = "planned"

    def test_ignoring_where_a_move_comes_from_plans_again(self, server, romset,
                                                         xml_path, catlist_path):
        """Dropped in place, the move would still run out of the ignored file."""
        base, app = server
        out = romset["out_dir"]
        (out / "Maze" / "Misc" / "goodgame.zip").rename(out / "goodgame.zip")
        post(base, "/api/plan", {"xml": xml_path, "catlist": catlist_path})
        wait(app, "planned")
        moves = [action.from_relpath for action in app.job.plan.sync.of("move")]
        assert "goodgame.zip" in moves
        answer = post(base, "/api/ignore", {"paths": ["goodgame.zip"]})
        assert answer["replanning"] is True
        wait(app, "planned")
        assert not app.job.plan.sync.of("move")
        assert "goodgame.zip" in app.job.plan.sync.ignored

def wait(app, state, timeout=15):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app.job.state in (state, "error"):
            assert app.job.state == state, app.job.error
            return
        time.sleep(0.02)
    raise AssertionError(f"stuck in {app.job.state}")


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as response:
        return json.loads(response.read())


def post(base, path, payload):
    request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())
