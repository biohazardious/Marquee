"""The record a destination keeps of itself."""
import json

from marquee import manifest, pipeline
from marquee.config import Config


class Fake:
    """Stands in for a Summary."""
    copied = 5
    updated = 1
    moved = 2
    deleted = 0
    skipped = 7
    copied_bytes = 1234
    machines = 9
    files = 14
    destination_bytes = 6000
    cancelled = False


class TestReadWrite:
    def test_absent_destination_reads_as_nothing(self, tmp_path):
        assert manifest.read(str(tmp_path)) is None

    def test_remote_destination_is_skipped(self):
        assert manifest.read("smb://nas/Share/roms") is None
        assert manifest.write("smb://nas/Share/roms", Config(), "0.289", Fake()) is None

    def test_round_trip(self, tmp_path):
        config = Config(copy_path=str(tmp_path), allow_mature=False,
                        blacklist_genres=["Casino"],
                        blacklist_categories=["Maze / Misc."],
                        blacklist_roms=["pong"])
        manifest.write(str(tmp_path), config, "0.282", Fake())
        record = manifest.read(str(tmp_path))
        assert record["mame_version"] == "0.282"
        assert record["stats"]["machines"] == 9
        assert manifest.settings_from(record) == {
            "allow_mature": False, "mature_rom_folder": "ZZ-Adult",
            "blacklist_genres": ["Casino"],
            "blacklist_categories": ["Maze / Misc."],
            "blacklist_roms": ["pong"]}

    def test_history_records_the_upgrade(self, tmp_path):
        config = Config(copy_path=str(tmp_path))
        manifest.write(str(tmp_path), config, "0.282", Fake())
        manifest.write(str(tmp_path), config, "0.289", Fake())
        history = manifest.read(str(tmp_path))["history"]
        assert len(history) == 2
        assert history[0]["from"] is None and history[0]["to"] == "0.282"
        assert history[1]["from"] == "0.282" and history[1]["to"] == "0.289"

    def test_history_is_capped(self, tmp_path):
        config = Config(copy_path=str(tmp_path))
        for _ in range(manifest.MAX_HISTORY + 8):
            manifest.write(str(tmp_path), config, "0.289", Fake())
        assert len(manifest.read(str(tmp_path))["history"]) == manifest.MAX_HISTORY

    def test_a_corrupt_file_reads_as_nothing(self, tmp_path):
        (tmp_path / manifest.NAME).write_text("{not json")
        assert manifest.read(str(tmp_path)) is None

    def test_an_unknown_format_reads_as_nothing(self, tmp_path):
        (tmp_path / manifest.NAME).write_text(json.dumps({"format": 99}))
        assert manifest.read(str(tmp_path)) is None

    def test_a_read_only_destination_does_not_raise(self, tmp_path):
        """A sync that worked must not fail because the record could not be written."""
        import os
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        try:
            assert manifest.write(str(locked), Config(), "0.289", Fake()) is None
        finally:
            os.chmod(locked, 0o700)

    def test_a_path_the_filesystem_rejects_does_not_raise(self, tmp_path):
        assert manifest.write(str(tmp_path) + "/\x00bad", Config(), "0.289",
                              Fake()) is None

    def test_describe_summarises(self, tmp_path):
        manifest.write(str(tmp_path), Config(), "0.282", Fake())
        described = manifest.describe(manifest.read(str(tmp_path)))
        assert described["mame_version"] == "0.282"
        assert described["machines"] == 9 and described["runs"] == 1

    def test_describe_of_nothing(self):
        assert manifest.describe(None) is None
        assert manifest.settings_from(None) == {}


class TestThroughThePipeline:
    def test_a_sync_records_the_version(self, categorised, config, romset):
        from marquee import catalog, plan as planning
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        pipeline.execute(built, config, mame_version="0.252")
        assert manifest.read(config.copy_path)["mame_version"] == "0.252"

    def test_the_next_plan_sees_what_is_there(self, categorised, config, romset,
                                              xml_path, catlist_path):
        from marquee import catalog, plan as planning
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        pipeline.execute(built, config, mame_version="0.252")

        _again, resolution = pipeline.build_plan(
            config, pipeline.SourceOptions(xml=xml_path, catlist=catlist_path))
        assert resolution.destination["mame_version"] == "0.252"
        assert resolution.destination["machines"] == len(built.items)


class TestLegacyName:
    """A library written before the rename still has to be recognised.

    Missing it would look like a fresh install, and the next run would re-copy a
    library it already holds.
    """

    def test_a_record_under_the_old_name_is_read(self, tmp_path):
        import json
        (tmp_path / ".mameparser.json").write_text(json.dumps({
            "format": manifest.FORMAT, "mame_version": "0.282",
            "settings": {"blacklist_genres": ["Quiz"]}}))
        record = manifest.read(str(tmp_path))
        assert record["mame_version"] == "0.282"
        assert manifest.settings_from(record)["blacklist_genres"] == ["Quiz"]

    def test_the_new_name_wins_when_both_are_there(self, tmp_path):
        import json
        (tmp_path / ".mameparser.json").write_text(
            json.dumps({"format": manifest.FORMAT, "mame_version": "0.282"}))
        (tmp_path / manifest.NAME).write_text(
            json.dumps({"format": manifest.FORMAT, "mame_version": "0.289"}))
        assert manifest.read(str(tmp_path))["mame_version"] == "0.289"

    def test_writing_uses_the_current_name(self, tmp_path):
        manifest.write(str(tmp_path), Config(copy_path=str(tmp_path)), "0.289", Fake())
        assert (tmp_path / manifest.NAME).exists()

    def test_no_record_at_all_is_still_none(self, tmp_path):
        assert manifest.read(str(tmp_path)) is None


class TestEveryRemoteSchemeIsRemote:
    """Only smb:// used to count. An ftp:// library had its record written into a
    local folder literally named after the URL -- password included."""

    def test_ftp_and_sftp_are_skipped_like_smb(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        for url in ("ftp://user:secret@nas/share/mame", "sftp://user@nas/mame",
                    "ftps://nas/mame", "ssh://nas/mame"):
            assert manifest.read(url) is None
            assert manifest.write(url, Config(), "0.289", Fake()) is None
        assert list(tmp_path.iterdir()) == [], "nothing was written relative to cwd"

    def test_free_space_is_unknowable_for_any_remote(self):
        from marquee import plan as planning
        assert planning.check_free_space(None, "ftp://nas/mame") is None
        assert Config(copy_path="sftp://nas/mame").is_remote is True
