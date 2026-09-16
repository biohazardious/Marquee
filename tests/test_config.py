"""Config as a value: reading, layering, and writing back."""
import os

import pytest

from marquee import config as configuration
from marquee.config import Config
from marquee.errors import ConfigError

BODY = """[config]
rom_dir = ./roms
chd_dir = ./chds
copy_path = /tmp/out/
allow_mature = {mature}
mature_rom_folder = ZZ-Adult
blacklist_genres = ['Board Game']
blacklist_roms = ["pong"]
"""


@pytest.fixture
def settings_file(tmp_path):
    def write(mature="False", extra="", body=None):
        (tmp_path / "roms").mkdir(exist_ok=True)
        (tmp_path / "chds").mkdir(exist_ok=True)
        path = tmp_path / "settings.ini"
        path.write_text(body if body is not None else BODY.format(mature=mature) + extra)
        return str(path)
    return write


class TestReading:
    def test_missing_file_raises(self):
        with pytest.raises(ConfigError, match="not found"):
            configuration.read_settings_file("/nonexistent/settings.ini")

    def test_missing_section_raises(self, tmp_path):
        path = tmp_path / "settings.ini"
        path.write_text("[other]\nfoo = bar\n")
        with pytest.raises(ConfigError, match="config"):
            configuration.read_settings_file(str(path))

    def test_allow_mature_false_is_a_bool(self, settings_file):
        """'False' is a truthy string, which is why the mature filter never fired."""
        assert configuration.read_settings_file(settings_file(mature="False")).allow_mature is False

    def test_allow_mature_true(self, settings_file):
        assert configuration.read_settings_file(settings_file(mature="True")).allow_mature is True

    def test_blacklists_become_lists(self, settings_file):
        built = configuration.read_settings_file(settings_file())
        assert built.blacklist_genres == ["Board Game"]
        assert built.blacklist_roms == ["pong"]

    def test_malformed_blacklist_names_the_key(self, settings_file):
        path = settings_file(body=BODY.format(mature="True").replace(
            'blacklist_roms = ["pong"]', "blacklist_roms = [unclosed"))
        with pytest.raises(ConfigError, match="blacklist_roms"):
            configuration.read_settings_file(path)

    def test_relative_paths_resolve_against_the_file(self, settings_file, tmp_path):
        """Not the shell's cwd, so the script runs from anywhere."""
        path = settings_file()
        os.chdir("/")
        assert configuration.read_settings_file(path).rom_dir == str(tmp_path / "roms")

    def test_absolute_paths_are_left_alone(self, settings_file):
        path = settings_file(extra="catlist_ini = /somewhere/catlist.ini\n")
        assert configuration.read_settings_file(path).catlist_ini == "/somewhere/catlist.ini"

    def test_legacy_category_ini_maps_to_catlist(self, settings_file):
        """The key was misleadingly named; catlist.ini is what it always held."""
        path = settings_file(extra="category_ini = /somewhere/catlist.ini\n")
        assert configuration.read_settings_file(path).catlist_ini == "/somewhere/catlist.ini"

    def test_obsolete_keys_are_noted(self, settings_file):
        built = configuration.read_settings_file(settings_file(extra="genre_ini = ./g.ini\n"))
        assert built.obsolete_keys == ["genre_ini"]


class TestLayering:
    def test_defaults_without_a_file(self, tmp_path):
        built = configuration.load(str(tmp_path / "absent.ini"))
        assert built.allow_mature is True
        assert built.blacklist_genres == []
        assert built.missing_paths == ["rom_dir", "copy_path"]

    def test_overrides_win(self, settings_file):
        built = configuration.load(settings_file(), rom_dir="/elsewhere")
        assert built.rom_dir == "/elsewhere"

    def test_none_overrides_are_ignored(self, settings_file, tmp_path):
        built = configuration.load(settings_file(), rom_dir=None)
        assert built.rom_dir == str(tmp_path / "roms")

    def test_false_override_is_not_ignored(self, settings_file):
        """allow_mature=False must override a True in the file, unlike a None."""
        built = configuration.load(settings_file(mature="True"), allow_mature=False)
        assert built.allow_mature is False

    def test_missing_paths_lists_only_what_is_missing(self):
        assert Config(rom_dir="/a").missing_paths == ["copy_path"]

    def test_the_chd_folder_is_the_rom_folder_unless_said_otherwise(self):
        """A first run used to stop on "missing chd_dir" for someone with no disks;
        the CHD set is found inside the ROM folder the same way the ROM set is."""
        assert Config(rom_dir="/a").chd_dir == "/a"
        assert Config(rom_dir="/a", chd_dir="/b").chd_dir == "/b"
        assert Config(rom_dir="/a", chd_dir="/b").with_overrides(chd_dir="").chd_dir == "/a"

    def test_remote_destination_is_recognised(self):
        assert Config(copy_path="smb://nas/Share").is_remote is True
        assert Config(copy_path="/tmp/out").is_remote is False

    def test_with_overrides_does_not_mutate(self):
        original = Config(rom_dir="/a")
        assert original.with_overrides(rom_dir="/b").rom_dir == "/b"
        assert original.rom_dir == "/a"


class TestWriting:
    def test_round_trip(self, tmp_path):
        built = Config(rom_dir="/roms", chd_dir="/chds", copy_path="/out",
                       allow_mature=False, blacklist_genres=["Board Game", "Casino"],
                       blacklist_roms=["pong"])
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, built)
        again = configuration.read_settings_file(path)
        assert again.rom_dir == "/roms"
        assert again.allow_mature is False
        assert again.blacklist_genres == ["Board Game", "Casino"]
        assert again.blacklist_roms == ["pong"]

    def test_empty_lists_round_trip(self, tmp_path):
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(
            path, Config(rom_dir="/r", chd_dir="/c", copy_path="/o"))
        assert configuration.read_settings_file(path).blacklist_genres == []

    def test_single_item_list_round_trips(self, tmp_path):
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(
            path, Config(rom_dir="/r", chd_dir="/c", copy_path="/o",
                         blacklist_genres=["Casino"]))
        assert configuration.read_settings_file(path).blacklist_genres == ["Casino"]


class TestObsoleteKeys:
    def test_survive_an_override(self, settings_file):
        """dataclasses.replace() drops anything that is not a declared field."""
        built = configuration.load(settings_file(extra="genre_ini = ./g.ini\n"),
                                   rom_dir="/elsewhere")
        assert built.obsolete_keys == ["genre_ini"]

    def test_empty_when_the_file_is_clean(self, settings_file):
        assert configuration.load(settings_file()).obsolete_keys == []


class TestCategoryBlacklist:
    def test_round_trips(self, tmp_path):
        built = Config(rom_dir="/r", chd_dir="/c", copy_path="/o",
                       blacklist_categories=["Shooter / Gallery", "Maze / Misc."])
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, built)
        assert configuration.read_settings_file(path).blacklist_categories == \
            ["Shooter / Gallery", "Maze / Misc."]

    def test_absent_key_defaults_to_empty(self, settings_file):
        assert configuration.read_settings_file(settings_file()).blacklist_categories == []

    def test_malformed_value_names_the_key(self, settings_file):
        path = settings_file(extra="blacklist_categories = [oops\n")
        with pytest.raises(ConfigError, match="blacklist_categories"):
            configuration.read_settings_file(path)


class TestAcquisitionRoundTrip:
    """Saving must not quietly drop the download client.

    The web UI rewrites the whole settings file when you press Save. The acquisition
    block was commented-out examples only, so every save wiped the configured client,
    its credentials, the download folder and the path mappings.
    """

    def settings(self, tmp_path, **overrides):
        config = configuration.Config(
            rom_dir="/r", chd_dir="/c", copy_path="/d", **overrides)
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, config)
        return configuration.read_settings_file(path), path

    def test_the_client_survives_a_save(self, tmp_path):
        back, _path = self.settings(
            tmp_path, download_client="http://nas:8080/",
            download_username="admin", download_password="hunter2",
            download_dir="/downloads")
        assert back.download_client == "http://nas:8080/"
        assert back.download_username == "admin"
        assert back.download_password == "hunter2"
        assert back.download_dir == "/downloads"

    def test_hardlinking_survives_a_save(self, tmp_path):
        on, _ = self.settings(tmp_path, hardlink=True)
        off, _ = self.settings(tmp_path, hardlink=False)
        assert on.hardlink is True
        assert off.hardlink is False

    def test_several_path_mappings_survive_a_save(self, tmp_path):
        back, _path = self.settings(
            tmp_path, download_client="http://nas:8080/",
            remote_path_mappings="/downloads -> /mnt/a\n/other -> /mnt/b")
        assert back.path_mappings == [("/downloads", "/mnt/a"), ("/other", "/mnt/b")]

    def test_a_mapping_is_indented_so_configparser_keeps_reading_it(self, tmp_path):
        _back, path = self.settings(
            tmp_path, download_client="http://nas:8080/",
            remote_path_mappings="/a -> /b\n/c -> /d")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        # An unindented continuation line reads as a new key, not more of this value.
        assert "\n    /c -> /d" in body

    def test_no_client_writes_examples_and_reads_back_empty(self, tmp_path):
        back, path = self.settings(tmp_path)
        assert back.download_client is None
        with open(path, encoding="utf-8") as handle:
            assert ";download_client" in handle.read()

    def test_saving_twice_is_stable(self, tmp_path):
        first, path = self.settings(
            tmp_path, download_client="http://nas:8080/", download_password="p",
            remote_path_mappings="/a -> /b", hardlink=True)
        configuration.write_settings_file(path, first)
        second = configuration.read_settings_file(path)
        assert (second.download_client, second.download_password,
                second.path_mappings, second.hardlink) == \
               (first.download_client, first.download_password,
                first.path_mappings, first.hardlink)


class TestParentsOnly:
    def test_it_round_trips(self, tmp_path):
        for value in (True, False):
            config = configuration.Config(rom_dir="/r", chd_dir="/c", copy_path="/d",
                                          parents_only=value)
            path = str(tmp_path / "settings.ini")
            configuration.write_settings_file(path, config)
            assert configuration.read_settings_file(path).parents_only is value

    def test_it_defaults_to_off(self, tmp_path):
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(
            path, configuration.Config(rom_dir="/r", chd_dir="/c", copy_path="/d"))
        assert configuration.read_settings_file(path).parents_only is False


class TestEverythingSurvivesASave:
    """The web UI rewrites the whole file on every save, so anything the writer forgets
    is silently deleted the first time somebody presses a button.
    """

    def round_trip(self, tmp_path, **fields):
        path = str(tmp_path / "settings.ini")
        base = {"rom_dir": "/a", "chd_dir": "/b", "copy_path": "/c"}
        configuration.write_settings_file(
            path, configuration.Config(**{**base, **fields}))
        return configuration.read_settings_file(path)

    def test_the_console_settings_come_back(self, tmp_path):
        """Both checkboxes were decorative: the template had no line for either."""
        back = self.round_trip(tmp_path, write_gamelist=False, copy_artwork=False)
        assert back.write_gamelist is False
        assert back.copy_artwork is False

    def test_the_discovery_overrides_come_back(self, tmp_path):
        """The file's own comment tells you to set these."""
        back = self.round_trip(tmp_path, mame_xml="/data/mame0289.xml",
                               catlist_ini="/data/catlist.ini")
        assert back.mame_xml == "/data/mame0289.xml"
        assert back.catlist_ini == "/data/catlist.ini"

    def test_an_unset_path_is_empty_not_the_word_none(self, tmp_path):
        path = str(tmp_path / "settings.ini")
        configuration.write_settings_file(path, configuration.Config())
        back = configuration.read_settings_file(path)
        # str(None) was written out, read back as a relative path, and resolved into a
        # folder called "None" that the run then planned against.
        assert back.rom_dir is None
        assert back.missing_paths == ["rom_dir", "copy_path"]

    def test_a_percent_sign_does_not_take_the_file_with_it(self, tmp_path):
        """configparser interpolates by default: one % and nothing can be read."""
        back = self.round_trip(tmp_path, copy_path="/media/100% full",
                               download_client="http://x/", download_password="p%ss")
        assert back.copy_path == "/media/100% full"
        assert back.download_password == "p%ss"

    def test_the_acquisition_block_survives(self, tmp_path):
        back = self.round_trip(tmp_path, download_client="http://host:8080/",
                               download_username="admin", download_password="secret",
                               download_dir="/downloads",
                               remote_path_mappings="/a -> /b")
        assert back.download_client == "http://host:8080/"
        assert back.download_password == "secret"
        assert back.remote_path_mappings == "/a -> /b"

    def test_the_blacklists_survive_at_scale(self, tmp_path):
        """3,235 entries, written as an indented continuation block."""
        names = [f"game{index:05d}" for index in range(3235)]
        back = self.round_trip(tmp_path, blacklist_roms=names,
                               blacklist_genres=["Ball & Paddle", "TTL * Ball"])
        assert back.blacklist_roms == names
        assert back.blacklist_genres == ["Ball & Paddle", "TTL * Ball"]

    def test_a_name_with_an_apostrophe_survives(self, tmp_path):
        back = self.round_trip(tmp_path, blacklist_roms=["don't", 'say "no"'])
        assert back.blacklist_roms == ["don't", 'say "no"']


class TestAValueThatIsNotWhatItSays:
    def write(self, tmp_path, extra):
        path = tmp_path / "settings.ini"
        path.write_text("[config]\nrom_dir = /r\nchd_dir = /c\ncopy_path = /o\n" + extra)
        return str(path)

    def test_a_boolean_that_is_not_one_names_the_key(self, tmp_path):
        with pytest.raises(ConfigError, match="hardlink"):
            configuration.read_settings_file(self.write(tmp_path, "hardlink = maybe\n"))

    def test_a_lone_name_is_a_list_of_one_not_its_letters(self, tmp_path):
        back = configuration.read_settings_file(
            self.write(tmp_path, "blacklist_roms = 'pacman'\n"))
        assert back.blacklist_roms == ["pacman"]

    def test_a_number_is_refused_as_a_list(self, tmp_path):
        with pytest.raises(ConfigError, match="blacklist_roms"):
            configuration.read_settings_file(self.write(tmp_path, "blacklist_roms = 5\n"))


class TestSavingIsAllOrNothing:
    def test_a_failed_write_leaves_the_old_file_intact(self, tmp_path, monkeypatch):
        path = tmp_path / "settings.ini"
        configuration.write_settings_file(str(path), Config(rom_dir="/old"))
        before = path.read_text()

        real_replace = os.replace

        def refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", refuse)
        with pytest.raises(OSError):
            configuration.write_settings_file(str(path), Config(rom_dir="/new"))
        monkeypatch.setattr(os, "replace", real_replace)
        assert path.read_text() == before
        assert not (tmp_path / "settings.ini.part").exists()
