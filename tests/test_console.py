"""Filing apart the games the console's own MAME cannot run.

Batocera 43 runs MAME 0.285 under a library built from the 0.289 set: 714 of 10,022
games are machines 0.285 does not have, and 67 are ones whose zip lacks a ROM it still
wants. They go under their own folders -- never deleted -- and come back when the
console catches up.
"""
import pytest

from marquee import console, pipeline
from marquee.reporting import CollectingReporter


class TestTheVerdict:
    SET = {"kept": [{"aa": "k.1"}, {}], "relabelled": [{"bb": "new.1"}, {}],
           "redumped": [{"cc": "r.1"}, {}], "disked": [{"dd": "d.1"}, {"s1": "disk"}]}
    CONSOLE = {"kept": [{"aa": "k.1"}, {}], "relabelled": [{"bb": "old.1"}, {}],
               "redumped": [{"c0": "r.1"}, {}], "disked": [{"dd": "d.1"}, {"s0": "disk"}]}

    def test_a_machine_the_console_does_not_have_needs_a_newer_mame(self):
        found = console.classify({"brandnew": [{}, {}]}, {}, ["brandnew"])
        assert found == {"brandnew": (console.NEWER, [])}

    def test_a_rom_under_another_label_is_still_there(self):
        """MAME finds it by CRC: a renamed chip is not a missing one."""
        assert console.classify(self.SET, self.CONSOLE, ["relabelled", "kept"]) == {}

    def test_a_redumped_rom_is_missing_and_named(self):
        found = console.classify(self.SET, self.CONSOLE, ["redumped"])
        assert found == {"redumped": (console.MISSING, ["r.1"])}

    def test_a_disk_is_held_to_its_sha1(self):
        found = console.classify(self.SET, self.CONSOLE, ["disked"])
        assert found == {"disked": (console.MISSING, ["disk.chd"])}

    def test_the_folders_are_the_settings_or_the_defaults(self, config):
        assert console.folder_for(console.NEWER, config) == "ZZ-Version-Mismatch"
        config.missing_rom_folder = "ZZ-Needs-Redump"
        assert console.folder_for(console.MISSING, config) == "ZZ-Needs-Redump"
        config.version_mismatch_folder = "  "
        assert console.folder_for(console.NEWER, config) == "ZZ-Version-Mismatch"


@pytest.fixture
def console_xml(tmp_path, xml_path):
    """The fixture release as an older console MAME would have it: without dotgame,
    with a different dump of goodgame's ROM, and a different parentchd disk."""
    text = open(xml_path, encoding="utf-8").read()
    text = text.replace('build="0.252 (mame0252)"', 'build="0.250 (mame0250)"')
    start = text.index(' <machine name="dotgame">')
    text = text[:start] + text[text.index("</machine>", start) + len("</machine>\n"):]
    text = text.replace('name="goodgame.bin" size="16" crc="000041ea"',
                        'name="goodgame.bin" size="16" crc="deadbeef"')
    # The parent's disk, and so the clone's that merges it: a redump changes both.
    text = text.replace('sha1="cd"', 'sha1="c0"')
    path = tmp_path / "mame0.250.xml"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def planned(config, romset, xml_path, catlist_path, console_xml, monkeypatch):
    config.mame_xml, config.catlist_ini = xml_path, catlist_path
    monkeypatch.setattr(pipeline.fetch, "cached_xml", lambda version: console_xml)

    def plan(**settings):
        for key, value in settings.items():
            setattr(config, key, value)
        reporter = CollectingReporter()
        built, resolution = pipeline.build_plan(config, reporter=reporter)
        return {item.name: item for item in built.wanted}, resolution, reporter
    return plan


class TestAPlan:
    def test_off_changes_nothing(self, planned):
        items, resolution, _reporter = planned()
        assert resolution.console is None
        assert not any(item.console for item in items.values())
        assert not any(item.folder.startswith("ZZ-") for item in items.values())

    def test_the_set_s_own_release_changes_nothing(self, planned):
        _items, resolution, _reporter = planned(console_mame_version="0.252")
        assert resolution.console is None

    def test_games_it_cannot_run_are_filed_apart(self, planned):
        items, resolution, _reporter = planned(console_mame_version="0.250")
        assert resolution.console == {"version": "0.250", "newer": 1, "missing": 3,
                                      "error": None}
        assert items["dotgame"].folder == "ZZ-Version-Mismatch/Fighter/25D"
        assert items["dotgame"].console == console.NEWER
        assert items["goodgame"].folder.startswith("ZZ-Missing-ROM/")
        assert items["goodgame"].console_detail == ["goodgame.bin"]
        assert items["parentchd"].console_detail == ["pdisk.chd"]
        assert items["clonemerged"].console == console.MISSING
        # Everything else stays exactly where it was.
        assert items["impgame"].folder == "Maze/Misc" and not items["impgame"].console

    def test_the_files_go_where_the_folder_says(self, planned):
        items, _resolution, _reporter = planned(console_mame_version="0.250")
        paths = [relpath for _source, relpath, _size in items["goodgame"].files()]
        assert paths == ["ZZ-Missing-ROM/Maze/Misc/goodgame.zip"]

    def test_renamed_folders_are_used(self, planned):
        items, _resolution, _reporter = planned(console_mame_version="0.250",
                                                version_mismatch_folder="Later")
        assert items["dotgame"].folder.startswith("Later/")

    def test_an_adult_game_keeps_its_own_folder_inside(self, planned, console_xml):
        text = open(console_xml, encoding="utf-8").read()
        start = text.index(' <machine name="maturegame">')
        text = text[:start] + text[text.index("</machine>", start) + len("</machine>\n"):]
        open(console_xml, "w", encoding="utf-8").write(text)
        items, _resolution, _reporter = planned(console_mame_version="0.250",
                                                allow_mature=True)
        assert items["maturegame"].folder.startswith("ZZ-Version-Mismatch/ZZ-Adult/")

    def test_a_release_that_cannot_be_read_moves_nothing(self, planned, monkeypatch):
        monkeypatch.setattr(pipeline.fetch, "cached_xml", lambda version: "/nowhere.xml")

        def refused(version, **kwargs):
            raise pipeline.SourceNotFoundError("no listxml for that one")
        monkeypatch.setattr(pipeline.fetch, "fetch_xml", refused)
        items, resolution, reporter = planned(console_mame_version="0.250")
        assert resolution.console["error"]
        assert not any(item.folder.startswith("ZZ-") for item in items.values())
        assert any("stays where it is" in message for message in reporter.warnings)


class TestTheLibraryFollows:
    """Filed apart by a rename, and back by a rename: nothing is copied twice."""

    def test_moving_apart_and_back_is_renames(self, config, romset, xml_path,
                                              catlist_path, console_xml, monkeypatch):
        config.mame_xml, config.catlist_ini = xml_path, catlist_path
        monkeypatch.setattr(pipeline.fetch, "cached_xml", lambda version: console_xml)
        out = romset["out_dir"]

        built, _resolution = pipeline.build_plan(config)
        pipeline.execute(built, config)
        assert (out / "Maze" / "Misc" / "goodgame.zip").exists()

        config.console_mame_version = "0.250"
        built, _resolution = pipeline.build_plan(config)
        assert built.sync.counts.get("new", 0) == 0
        assert built.sync.counts.get("move", 0) >= 3
        pipeline.execute(built, config)
        assert (out / "ZZ-Missing-ROM" / "Maze" / "Misc" / "goodgame.zip").exists()
        assert not (out / "Maze" / "Misc" / "goodgame.zip").exists()

        config.console_mame_version = None
        built, _resolution = pipeline.build_plan(config)
        assert built.sync.counts.get("new", 0) == 0
        pipeline.execute(built, config)
        assert (out / "Maze" / "Misc" / "goodgame.zip").exists()
        assert not (out / "ZZ-Missing-ROM").exists(), "the emptied folder was tidied"



class TestADiskAParentShares:
    """sfiii2a went under ZZ-Version-Mismatch while its parent sfiii2 stayed; the
    disk they share lives in sfiii2's folder, and the plan asked to fetch it again."""

    def test_it_stays_where_the_parent_keeps_it(self, config, romset, xml_path,
                                                catlist_path, tmp_path, monkeypatch):
        text = open(xml_path, encoding="utf-8").read()
        start = text.index(' <machine name="clonemerged"')
        text = text[:start] + text[text.index("</machine>", start) + len("</machine>\n"):]
        older = tmp_path / "mame0.250.xml"
        older.write_text(text.replace('build="0.252 (mame0252)"', 'build="0.250 (mame0250)"'),
                         encoding="utf-8")
        config.mame_xml, config.catlist_ini = xml_path, catlist_path
        monkeypatch.setattr(pipeline.fetch, "cached_xml", lambda version: str(older))

        built, _resolution = pipeline.build_plan(config)
        pipeline.execute(built, config)
        config.console_mame_version = "0.250"
        built, _resolution = pipeline.build_plan(config)
        items = {item.name: item for item in built.wanted}
        clone, parent = items["clonemerged"], items["parentchd"]
        assert clone.folder.startswith("ZZ-Version-Mismatch/")
        assert not parent.console
        disk = [path for path in clone.wanted_paths() if path.endswith(".chd")]
        assert disk == [f"{parent.folder}/parentchd/pdisk.chd"]
        assert not clone.disks_to_fetch
        assert built.sync.counts.get("new", 0) == 0

class TestTheSettings:
    def test_they_survive_the_settings_file(self, tmp_path):
        from marquee import config as configuration
        written = configuration.Config(rom_dir=str(tmp_path), copy_path=str(tmp_path),
                                       console_mame_version="0.285",
                                       missing_rom_folder="ZZ-Redumped")
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, written)
        read = configuration.read_settings_file(path)
        assert read.console_mame_version == "0.285"
        assert read.version_mismatch_folder == "ZZ-Version-Mismatch"
        assert read.missing_rom_folder == "ZZ-Redumped"

    def test_the_form_can_clear_it(self, tmp_path, config, romset):
        from marquee import config as configuration
        from marquee.web.server import Application
        config.settings_path = str(tmp_path / "settings.ini")
        config.console_mame_version = "0.285"
        configuration.write_settings_file(config.settings_path, config)
        app = Application(config.settings_path)
        assert app.config_payload()["console_mame_version"] == "0.285"
        app.save({"console_mame_version": ""})
        assert app.current_config().console_mame_version is None
        app.save({"console_mame_version": "0.287", "version_mismatch_folder": "Later"})
        assert app.current_config().console_mame_version == "0.287"
        assert app.current_config().version_mismatch_folder == "Later"
