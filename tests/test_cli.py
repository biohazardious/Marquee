"""The terminal front end, and locating MAME data from a Config."""
import os

import pytest

from marquee import cli, fetch, pipeline, sources
from marquee.config import Config
from marquee.errors import MarqueeError, SourceNotFoundError, VersionMismatchError
from marquee.pipeline import SourceOptions
from marquee.reporting import CollectingReporter

BODY = """[config]
rom_dir = ./roms
chd_dir = ./chds
copy_path = /tmp/out/
allow_mature = False
"""


@pytest.fixture(autouse=True)
def off_the_real_settings(monkeypatch, tmp_path):
    """Keep the default config path away from the project's own settings.ini."""
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS", str(tmp_path / "absent-default.ini"))


@pytest.fixture
def settings_file(tmp_path):
    def write(extra=""):
        (tmp_path / "roms").mkdir(exist_ok=True)
        (tmp_path / "chds").mkdir(exist_ok=True)
        path = tmp_path / "settings.ini"
        path.write_text(BODY + extra)
        return str(path)
    return write


def args(**overrides):
    parsed = cli.build_arg_parser().parse_args([])
    for key, value in overrides.items():
        setattr(parsed, key, value)
    return parsed


class TestLoadConfig:
    def test_explicit_missing_file_raises(self):
        with pytest.raises(MarqueeError, match="not found"):
            cli.load_config(args(config="/nonexistent/settings.ini"))

    def test_reads_the_file(self, settings_file, tmp_path):
        built = cli.load_config(args(config=settings_file()))
        assert built.rom_dir == str(tmp_path / "roms")
        assert built.allow_mature is False

    def test_command_line_overrides_the_file(self, settings_file, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        built = cli.load_config(args(config=settings_file(), rom_dir=str(other)))
        assert built.rom_dir == str(other)

    def test_paths_can_come_entirely_from_the_command_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "interactive", lambda: False)
        (tmp_path / "roms").mkdir()
        (tmp_path / "chds").mkdir()
        built = cli.load_config(args(rom_dir=str(tmp_path / "roms"),
                                     chd_dir=str(tmp_path / "chds"),
                                     dest=str(tmp_path / "out")))
        assert built.blacklist_genres == []
        assert built.allow_mature is True

    def test_nothing_configured_points_at_setup(self, monkeypatch):
        monkeypatch.setattr(cli, "interactive", lambda: False)
        with pytest.raises(MarqueeError, match="--setup"):
            cli.load_config(args())

    def test_partial_configuration_names_what_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "interactive", lambda: False)
        with pytest.raises(MarqueeError) as error:
            cli.load_config(args(rom_dir=str(tmp_path)))
        message = str(error.value)
        assert "chd_dir" in message and "copy_path" in message and "rom_dir" not in message

    def test_obsolete_key_is_called_out(self, settings_file, capsys):
        cli.load_config(args(config=settings_file(extra="genre_ini = ./g.ini\n")))
        assert "no longer used" in capsys.readouterr().out


class TestSetupWizard:
    def test_writes_a_usable_settings_file(self, tmp_path, monkeypatch):
        (tmp_path / "roms").mkdir()
        answers = iter([str(tmp_path / "roms"), "", str(tmp_path / "out"), "y", "y"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        path = str(tmp_path / "settings.ini")
        cli.run_setup(path)

        built = cli.load_config(args(config=path))
        assert built.rom_dir == str(tmp_path / "roms")
        assert built.copy_path == str(tmp_path / "out")
        assert built.allow_mature is True
        assert "Slot Machine" in built.blacklist_genres

    def test_chd_dir_defaults_to_the_rom_dir(self, tmp_path, monkeypatch):
        (tmp_path / "roms").mkdir()
        answers = iter([str(tmp_path / "roms"), "", str(tmp_path / "out"), "n", "n"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
        path = str(tmp_path / "settings.ini")
        cli.run_setup(path)
        built = cli.load_config(args(config=path))
        assert built.chd_dir == str(tmp_path / "roms")
        assert built.allow_mature is False
        assert built.blacklist_genres == []

    def test_rejects_a_directory_that_does_not_exist(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "roms").mkdir()
        answers = iter(["/nonexistent/path", str(tmp_path / "roms"), "",
                        str(tmp_path / "out"), "y", "y"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
        cli.run_setup(str(tmp_path / "settings.ini"))
        assert "is not a directory" in capsys.readouterr().out

    def test_declining_to_overwrite_leaves_the_file_alone(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.ini"
        path.write_text("[config]\nkeep = me\n")
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert cli.run_setup(str(path)) is None
        assert path.read_text() == "[config]\nkeep = me\n"

    def test_offered_when_nothing_is_configured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "interactive", lambda: True)
        (tmp_path / "roms").mkdir()
        answers = iter(["y", str(tmp_path / "roms"), "", str(tmp_path / "out"), "y", "n"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
        built = cli.load_config(args())
        assert built.rom_dir == str(tmp_path / "roms")

    def test_declining_setup_still_fails(self, monkeypatch):
        monkeypatch.setattr(cli, "interactive", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        with pytest.raises(MarqueeError):
            cli.load_config(args())


class TestResolveSources:
    @pytest.fixture
    def reporter(self):
        return CollectingReporter()

    def test_explicit_xml_option_wins(self, xml_path, reporter):
        assert pipeline.resolve_mame_xml(Config(), SourceOptions(xml=xml_path), reporter) == xml_path

    def test_configured_xml_is_used(self, xml_path, reporter):
        assert pipeline.resolve_mame_xml(Config(mame_xml=xml_path), SourceOptions(), reporter) == xml_path

    def test_broken_configured_xml_falls_back_to_discovery(self, xml_path, reporter, monkeypatch):
        monkeypatch.setattr(sources, "find_mame_xml", lambda _hints: xml_path)
        found = pipeline.resolve_mame_xml(Config(mame_xml="/nonexistent/x.xml"),
                                          SourceOptions(), reporter)
        assert found == xml_path
        assert reporter.warnings and "does not exist" in reporter.warnings[0]

    def test_listxml_is_the_last_resort(self, xml_path, reporter, monkeypatch):
        monkeypatch.setattr(sources, "find_mame_xml", lambda _hints: None)
        monkeypatch.setattr(sources, "generate_listxml", lambda _b, reporter=None: xml_path)
        assert pipeline.resolve_mame_xml(Config(), SourceOptions(), reporter) == xml_path

    def test_no_xml_at_all_raises(self, reporter, monkeypatch):
        monkeypatch.setattr(sources, "find_mame_xml", lambda _hints: None)
        monkeypatch.setattr(sources, "generate_listxml", lambda _b, reporter=None: None)
        with pytest.raises(SourceNotFoundError, match="--xml"):
            pipeline.resolve_mame_xml(Config(), SourceOptions(), reporter)

    def test_explicit_catlist_option_wins(self, catlist_path, reporter):
        assert pipeline.resolve_catlist(Config(), SourceOptions(catlist=catlist_path),
                                        "0.252", reporter) == catlist_path

    def test_configured_catlist_is_used(self, catlist_path, reporter):
        assert pipeline.resolve_catlist(Config(catlist_ini=catlist_path), SourceOptions(),
                                        "0.252", reporter) == catlist_path

    def test_missing_explicit_catlist_raises(self, reporter):
        with pytest.raises(SourceNotFoundError, match="catlist.ini not found"):
            pipeline.resolve_catlist(Config(), SourceOptions(catlist="/nonexistent/c.ini"),
                                     "0.252", reporter)

    def test_the_projects_mamefiles_folder_is_searched(self):
        """The documented place to drop your own catlist.ini has to be looked in.

        Nothing is shipped in it any more -- a bundled catlist is for one MAME release
        and wrong for every other -- but it is where the README, the error messages and
        MameFiles/README.md all tell people to put theirs.
        """
        hints = SourceOptions().hints(Config())
        assert any(os.path.basename(path) == "MameFiles" for path in hints)

    def test_a_catlist_dropped_there_is_used(self, tmp_path, reporter):
        drop = tmp_path / "MameFiles"
        drop.mkdir()
        mine = drop / "catlist.ini"
        mine.write_text(";; CATLIST.ini 0.252 / 06-Mar-23 / MAME 0.252 ;;\n[ROOT_FOLDER]\n")
        found = pipeline.resolve_catlist(
            Config(), SourceOptions(search_dirs=(str(drop),)), "0.252", reporter)
        assert found == str(mine)


class TestCatlistDownload:
    @pytest.fixture
    def nowhere(self, monkeypatch):
        monkeypatch.setattr(sources, "find_support_file",
                            lambda _name, _hints, wanted_version=None: None)
        return CollectingReporter()

    def test_fetches_the_version_matching_the_xml(self, nowhere, monkeypatch, tmp_path):
        asked = {}

        def fake_fetch(name, version, refresh=False, reporter=None):
            asked.update(name=name, version=version)
            target = tmp_path / "fetched.ini"
            target.write_text("[ROOT_FOLDER]\n")
            return str(target)

        monkeypatch.setattr(fetch, "fetch_support_file", fake_fetch)
        result = pipeline.resolve_catlist(Config(), SourceOptions(), "0.270", nowhere)
        assert asked == {"name": "catlist.ini", "version": "0.270"}
        assert result.endswith("fetched.ini")

    def test_offline_refuses_to_fetch(self, nowhere):
        with pytest.raises(SourceNotFoundError, match="--offline"):
            pipeline.resolve_catlist(Config(), SourceOptions(offline=True), "0.270", nowhere)

    def test_unknown_xml_version_cannot_pick_a_file(self, nowhere):
        with pytest.raises(SourceNotFoundError, match="--catlist"):
            pipeline.resolve_catlist(Config(), SourceOptions(), None, nowhere)

    def test_download_failure_is_propagated(self, nowhere, monkeypatch):
        def explode(_name, _version, refresh=False, reporter=None):
            raise SourceNotFoundError("archive covers 0.262 to 0.289")

        monkeypatch.setattr(fetch, "fetch_support_file", explode)
        with pytest.raises(SourceNotFoundError, match="0.262 to 0.289"):
            pipeline.resolve_catlist(Config(), SourceOptions(), "0.252", nowhere)

    def test_fetch_flag_skips_the_local_search(self, monkeypatch, tmp_path):
        def unexpected(*_a, **_k):
            raise AssertionError("local search ran despite --fetch-support-files")

        monkeypatch.setattr(sources, "find_support_file", unexpected)
        target = tmp_path / "fetched.ini"
        target.write_text("[ROOT_FOLDER]\n")
        monkeypatch.setattr(fetch, "fetch_support_file", lambda *_a, **_k: str(target))
        assert pipeline.resolve_catlist(Config(), SourceOptions(fetch_support_files=True),
                                        "0.289", CollectingReporter()) == str(target)


class TestVersionGate:
    def test_matching_versions_pass(self):
        reporter = CollectingReporter()
        pipeline.check_versions("0.252", "0.252", False, reporter)
        assert "0.252" in reporter.text
        assert reporter.warnings == []

    def test_mismatch_raises(self):
        with pytest.raises(VersionMismatchError, match="0.251"):
            pipeline.check_versions("0.252", "0.251", False, CollectingReporter())

    def test_mismatch_can_be_overridden(self):
        reporter = CollectingReporter()
        pipeline.check_versions("0.252", "0.251", True, reporter)
        assert reporter.warnings

    def test_unknown_versions_do_not_block(self):
        pipeline.check_versions("0.252", None, False, CollectingReporter())


class TestACatlistForAnotherRelease:
    """What the project used to do to itself: ship a 0.252 catlist.ini in MameFiles/,
    which the search found first, and then refuse to run for anybody on a newer set.
    """

    @pytest.fixture
    def stale(self, tmp_path):
        path = tmp_path / "catlist.ini"
        path.write_text(";; CATLIST.ini 0.252 / 06-Mar-23 / MAME 0.252 ;;\n[ROOT_FOLDER]\n")
        return str(tmp_path)

    def test_the_right_one_is_fetched_instead(self, stale, monkeypatch, tmp_path):
        asked = {}

        def fake_fetch(name, version, refresh=False, reporter=None):
            asked.update(name=name, version=version)
            target = tmp_path / "fetched.ini"
            target.write_text("[ROOT_FOLDER]\n")
            return str(target)

        monkeypatch.setattr(fetch, "fetch_support_file", fake_fetch)
        reporter = CollectingReporter()
        result = pipeline.resolve_catlist(
            Config(), SourceOptions(search_dirs=(stale,)), "0.289", reporter)
        assert asked == {"name": "catlist.ini", "version": "0.289"}
        assert result.endswith("fetched.ini")

    def test_it_says_which_file_it_ignored(self, stale, monkeypatch, tmp_path):
        target = tmp_path / "fetched.ini"
        target.write_text("[ROOT_FOLDER]\n")
        monkeypatch.setattr(fetch, "fetch_support_file", lambda *_a, **_k: str(target))
        reporter = CollectingReporter()
        pipeline.resolve_catlist(Config(), SourceOptions(search_dirs=(stale,)),
                                 "0.289", reporter)
        said = " ".join(reporter.messages)
        assert "0.252" in said and "Ignoring" in said

    def test_offline_falls_back_to_it_and_says_so(self, stale):
        """Nothing else to use, so it is offered -- and the mismatch check still runs."""
        reporter = CollectingReporter()
        found = pipeline.resolve_catlist(
            Config(), SourceOptions(search_dirs=(stale,), offline=True), "0.289",
            reporter)
        assert found.endswith("catlist.ini")
        assert any("0.252" in message for message in reporter.messages)

    def test_a_matching_one_is_used_without_fetching(self, tmp_path, monkeypatch):
        good = tmp_path / "here"
        good.mkdir()
        path = good / "catlist.ini"
        path.write_text(";; CATLIST.ini 0.289 / 01-Jan-26 / MAME 0.289 ;;\n[ROOT_FOLDER]\n")

        def unexpected(*_a, **_k):
            raise AssertionError("fetched despite having the right file on disk")

        monkeypatch.setattr(fetch, "fetch_support_file", unexpected)
        assert pipeline.resolve_catlist(
            Config(), SourceOptions(search_dirs=(str(good),)), "0.289",
            CollectingReporter()) == str(path)


class TestAChosenVersionAlreadyOnDisk:
    def reporter(self):
        return CollectingReporter()

    def test_offline_uses_the_matching_xml_rather_than_failing(self, monkeypatch, xml_path):
        def no_fetch(*_a, **_k):
            raise AssertionError("fetched although the file was on disk")

        def find(hints, wanted_version=None):
            return xml_path if wanted_version == "0.252" else None

        monkeypatch.setattr(fetch, "fetch_xml", no_fetch)
        monkeypatch.setattr(sources, "find_mame_xml", find)
        found = pipeline.resolve_mame_xml(Config(mame_version="0.252"),
                                          SourceOptions(offline=True), self.reporter())
        assert found == xml_path

    def test_a_missing_explicit_xml_is_an_error_not_a_fallback(self, monkeypatch):
        def unexpected(*_a, **_k):
            raise AssertionError("went looking for another XML")

        monkeypatch.setattr(sources, "find_mame_xml", unexpected)
        with pytest.raises(SourceNotFoundError, match="/nonexistent/x.xml"):
            pipeline.resolve_mame_xml(Config(), SourceOptions(xml="/nonexistent/x.xml"),
                                      self.reporter())


class TestTheFolderNamesTheRelease:
    def test_a_pleasuredome_folder_says_which_release(self):
        assert sources.version_in_path("/downloads/MAME 0.289 ROMs (non-merged)") == "0.289"
        assert sources.version_in_path("/downloads/MAME 0.288 CHDs (merged)/dlair") == "0.288"
        assert sources.version_in_path("/srv/roms/mame") is None

    def test_a_fresh_install_fetches_the_release_the_folder_names(self, tmp_path, monkeypatch):
        folder = tmp_path / "MAME 0.289 ROMs (non-merged)"
        folder.mkdir()
        (folder / "pacman.zip").write_bytes(b"x")
        asked = []

        def fake_fetch(version, refresh=False, reporter=None):
            asked.append(version)
            target = tmp_path / "mame0.289.xml"
            target.write_text('<mame build="0.289 (mame0289)"/>')
            return str(target)

        monkeypatch.setattr(fetch, "fetch_xml", fake_fetch)
        monkeypatch.setattr(sources, "find_mame_xml", lambda *a, **k: None)
        monkeypatch.setattr(sources, "generate_listxml", lambda *a, **k: None)
        found = pipeline.resolve_mame_xml(Config(rom_dir=str(tmp_path)), SourceOptions(),
                                          CollectingReporter())
        assert asked == ["0.289"]
        assert found.endswith("mame0.289.xml")

    def test_without_a_named_folder_the_message_points_at_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sources, "find_mame_xml", lambda *a, **k: None)
        monkeypatch.setattr(sources, "generate_listxml", lambda *a, **k: None)
        with pytest.raises(SourceNotFoundError, match="Pick the release in Settings"):
            pipeline.resolve_mame_xml(Config(rom_dir=str(tmp_path)), SourceOptions(),
                                      CollectingReporter())
