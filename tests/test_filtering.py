"""Which machines survive the romset filters, and how CHD folders are chosen."""
from marquee import catalog, sources as MameSources


def names(machines, screenless=frozenset(), config=None):
    from marquee.config import Config
    return set(catalog.filter_machines(
        machines, screenless,
        config or Config(blacklist_roms=["blockedgame"], blacklist_genres=["Board Game"])))


class TestMachineFilters:
    def test_keeps_a_plain_working_game(self, machines, config):
        assert "goodgame" in names(machines, frozenset(), config)

    def test_keeps_imperfect_status_when_emulation_is_good(self, machines, config):
        """Graphics or sound glitches still make a playable game."""
        assert "impgame" in names(machines, frozenset(), config)

    def test_drops_preliminary_driver(self, machines, config):
        assert "deadgame" not in names(machines, frozenset(), config)

    def test_drops_blacklisted_rom(self, machines, config):
        assert "blockedgame" not in names(machines, frozenset(), config)

    def test_drops_bios_set(self, machines, config):
        assert "biosset" not in names(machines, frozenset(), config)

    def test_drops_mechanical_machine(self, machines, config):
        assert "mechgame" not in names(machines, frozenset(), config)

    def test_drops_device(self, machines, config):
        assert "devthing" not in names(machines, frozenset(), config)

    def test_drops_prototype(self, machines, config):
        assert "protogame" not in names(machines, frozenset(), config)

    def test_drops_beta(self, machines, config):
        assert "betagame" not in names(machines, frozenset(), config)

    def test_drops_prototype_with_region_prefix(self, machines, config):
        """'(Japan, prototype)' is a prototype too."""
        assert "jpproto" not in names(machines, frozenset(), config)

    def test_keeps_title_merely_containing_beta(self, machines, config):
        """'Betamax' is not a beta; the word boundary has to hold."""
        assert "betamax" in names(machines, frozenset(), config)

    def test_drops_beta_bootleg_by_manufacturer(self, machines, config):
        assert "bootlegbeta" not in names(machines, frozenset(), config)

    def test_drops_screenless_machine(self, machines, config):
        assert "blindgame" not in names(machines, {"blindgame"}, config)

    def test_keeps_screenless_machine_when_not_listed(self, machines, config):
        assert "blindgame" in names(machines, frozenset(), config)

    def test_full_keeper_set(self, machines, config):
        screenless = catalog.derive_screenless(machines)
        assert names(machines, screenless, config) == {
            "goodgame", "impgame", "betamax", "boardgame1", "maturegame", "dotgame",
            "nocat", "twodisk", "parentchd", "clonemerged", "cloneown", "clonestray"}


class TestChdSelection:
    def test_game_without_disk_has_no_chd_fields(self, machines, config):
        result = catalog.filter_machines(machines, frozenset(), config)
        assert result["goodgame"]["chd_req"] is False
        assert "chd_folder" not in result["goodgame"]

    def test_dumped_disk_behind_a_nodump_is_still_required(self, machines, config):
        result = catalog.filter_machines(machines, frozenset(), config)
        assert result["twodisk"]["chd_req"] is True
        assert result["twodisk"]["chd_disks"] == ["ok"]
        assert result["twodisk"]["chd_folder"] == "twodisk"

    def test_parent_uses_its_own_folder(self, machines, config):
        result = catalog.filter_machines(machines, frozenset(), config)
        assert result["parentchd"]["chd_folder"] == "parentchd"

    def test_merged_clone_goes_under_the_parent(self, machines, config):
        """Duplicating a multi-gigabyte disk is the only alternative."""
        result = catalog.filter_machines(machines, frozenset(), config)
        assert result["clonemerged"]["chd_folder"] == "parentchd"

    def test_unmerged_clone_gets_its_own_folder(self, machines, config):
        """The parent's CHD is a different file; sharing would deliver the wrong disk."""
        result = catalog.filter_machines(machines, frozenset(), config)
        assert result["cloneown"]["chd_folder"] == "cloneown"
        assert result["cloneown"]["chd_disks"] == ["odisk"]

    def test_parent_is_carried_for_the_source_lookup(self, machines, config):
        result = catalog.filter_machines(machines, frozenset(), config)
        assert result["cloneown"]["parent"] == "parentchd"
        assert result["parentchd"]["parent"] is None


class TestDerivedScreenless:
    def test_derives_machines_without_display(self, machines):
        assert catalog.derive_screenless(machines) == {"blindgame", "mechgame", "devthing"}

    def test_matches_the_shipped_screenless_ini(self, machines, screenless_path, reporter):
        catalog.derive_screenless(machines, screenless_path, reporter)
        assert "matches" in reporter.text

    def test_reports_a_difference_instead_of_hiding_it(self, machines, tmp_path, reporter):
        divergent = tmp_path / "screenless.ini"
        divergent.write_text("[ROOT_FOLDER]\nblindgame\ngoodgame\n")
        result = catalog.derive_screenless(machines, str(divergent), reporter)
        assert reporter.warnings and "differs" in reporter.warnings[0]
        # The derived list still wins, because it always matches this XML.
        assert "goodgame" not in result

    def test_ignores_ini_entries_for_absent_machines(self, machines, tmp_path, reporter):
        """An ini for a different romset lists machines this XML never mentions."""
        other = tmp_path / "screenless.ini"
        other.write_text("[ROOT_FOLDER]\nblindgame\nmechgame\ndevthing\nsomeoldmachine\n")
        catalog.derive_screenless(machines, str(other), reporter)
        assert "matches" in reporter.text
        assert reporter.warnings == []

    def test_missing_file_is_not_an_error(self, machines):
        assert catalog.derive_screenless(machines, "/nonexistent/screenless.ini")


class TestCacheRoundTripKeepsFiltering:
    def test_records_survive_json(self, xml_path, config):
        """Cached records are JSON, so booleans and None must come back intact."""
        fresh = MameSources.extract_machines(xml_path)["machines"]
        cached = MameSources.load_machines(xml_path)["machines"]
        reloaded = MameSources.load_machines(xml_path)["machines"]
        assert (names(fresh, frozenset(), config) == names(cached, frozenset(), config)
                == names(reloaded, frozenset(), config))
