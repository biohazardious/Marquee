"""Version headers, discovery, derived data and the parse cache."""
import os
import shutil

import pytest

from marquee import sources as MameSources


class TestVersions:
    def test_reads_mame_extras_header(self, catlist_path):
        file_version, mame_version = MameSources.read_ini_version(catlist_path)
        assert (file_version, mame_version) == ("0.252", "0.252")

    def test_reads_a_real_catlist_header(self):
        """Whatever the user has dropped in MameFiles/, its header must be readable.

        Not pinned to a version: nothing is shipped there any more, and asserting
        0.252 would fail for the person who put their own 0.289 copy in it.
        """
        root = os.path.dirname(os.path.dirname(MameSources.__file__))
        real = os.path.join(root, "MameFiles", "catlist.ini")
        if not os.path.isfile(real):
            return
        _file_version, mame_version = MameSources.read_ini_version(real)
        assert mame_version, "a real catlist.ini carries a MAME version in its header"

    def test_missing_header_is_not_an_error(self, tmp_path):
        plain = tmp_path / "plain.ini"
        plain.write_text("[ROOT_FOLDER]\nfoo\n")
        assert MameSources.read_ini_version(str(plain)) == (None, None)

    def test_reads_build_without_parsing_document(self, xml_path):
        assert MameSources.read_xml_build(xml_path) == "0.252"

    def test_build_of_unreadable_file_is_none(self, tmp_path):
        broken = tmp_path / "broken.xml"
        broken.write_text("not xml at all")
        assert MameSources.read_xml_build(str(broken)) is None

    def test_matching_versions_produce_no_warning(self):
        assert MameSources.describe_version_match("0.252", "0.252") is None

    def test_unknown_version_produces_no_warning(self):
        assert MameSources.describe_version_match(None, "0.252") is None
        assert MameSources.describe_version_match("0.252", None) is None

    def test_mismatch_is_reported_with_both_versions(self):
        warning = MameSources.describe_version_match("0.251", "0.252")
        assert warning and "0.251" in warning and "0.252" in warning


class TestDiscovery:
    def test_finds_file_in_hint_directory(self, tmp_path):
        (tmp_path / "catlist.ini").write_text("[ROOT_FOLDER]\n")
        assert MameSources.find_support_file("catlist.ini", [str(tmp_path)]) == \
            str(tmp_path / "catlist.ini")

    def test_search_is_case_insensitive(self, tmp_path):
        (tmp_path / "CatList.INI").write_text("[ROOT_FOLDER]\n")
        assert MameSources.find_support_file("catlist.ini", [str(tmp_path)]) is not None

    def test_returns_none_when_absent(self, tmp_path):
        assert MameSources.find_support_file("catlist.ini", [str(tmp_path)]) is None

    def test_hint_directories_win_over_system_ones(self, tmp_path):
        (tmp_path / "catlist.ini").write_text("[ROOT_FOLDER]\n")
        found = MameSources.find_support_file("catlist.ini", [str(tmp_path)])
        assert found.startswith(str(tmp_path))

    def test_prefers_highest_xml_version(self, tmp_path, xml_path, isolated_search):
        """Filenames encode 0.252 as "0252", which has no dot for BUILD_RE to find."""
        for name in ("mame0245.xml", "mame0252.xml", "mame0198.xml"):
            shutil.copy(xml_path, tmp_path / name)
            os.utime(tmp_path / name, (0, 0))  # equal mtimes, so version must decide
        found = MameSources.find_mame_xml([str(tmp_path)])
        assert os.path.basename(found) == "mame0252.xml"

    def test_real_listxml_beats_a_higher_looking_name(self, tmp_path, xml_path,
                                                      isolated_search):
        """A file MAME wrote outranks one that merely looks like a newer dump."""
        shutil.copy(xml_path, tmp_path / "mame0100.xml")
        (tmp_path / "mame0300.xml").write_text("<notmame/>")
        assert os.path.basename(MameSources.find_mame_xml([str(tmp_path)])) == "mame0100.xml"

    def test_version_key_reads_the_build_attribute(self, xml_path):
        assert MameSources.xml_version_key(xml_path) == (1, 0.252)

    def test_version_key_falls_back_to_the_filename(self, tmp_path):
        unreadable = tmp_path / "mame0251.xml"
        unreadable.write_text("not xml")
        assert MameSources.xml_version_key(str(unreadable)) == (0, 0.251)

    def test_version_key_handles_a_two_digit_version(self, tmp_path):
        old_dump = tmp_path / "mame098.xml"
        old_dump.write_text("not xml")
        assert MameSources.xml_version_key(str(old_dump)) == (0, 0.98)

    def test_version_key_of_an_unversioned_name(self, tmp_path):
        anonymous = tmp_path / "mamedump.xml"
        anonymous.write_text("not xml")
        assert MameSources.xml_version_key(str(anonymous)) == (0, 0.0)

    def test_no_xml_anywhere_returns_none(self, tmp_path, isolated_search):
        assert MameSources.find_mame_xml([str(tmp_path)]) is None

    def test_listxml_without_mame_on_path_returns_none(self, monkeypatch):
        monkeypatch.setattr(MameSources.shutil, "which", lambda _name: None)
        assert MameSources.generate_listxml() is None


class TestDerivedGenres:
    def test_genre_is_leading_component(self):
        assert MameSources.genre_of("Arcade: Maze / Misc.") == "Maze"
        assert MameSources.genre_of("Board Game / Cards") == "Board Game"

    def test_mature_marker_does_not_leak_into_genre(self):
        assert MameSources.genre_of("Arcade: Casino / Misc. * Mature *") == "Casino"

    def test_section_without_slash_is_its_own_genre(self):
        assert MameSources.genre_of("Arcade: Puzzle") == "Puzzle"

    def test_clean_category_strips_only_the_prefix(self):
        assert MameSources.clean_category("Arcade: Casino / Cards") == "Casino / Cards"
        # lstrip("Arcade:") would have eaten the leading 'a' here.
        assert MameSources.clean_category("Arcade: arcade / Misc") == "arcade / Misc"

    def test_derives_expected_buckets(self, catlist):
        genres = MameSources.derive_genres(catlist)
        assert genres["Board Game"] == {"boardgame1"}
        assert genres["Casino"] == {"maturegame"}
        assert "goodgame" in genres["Maze"]
        assert "FOLDER_SETTINGS" not in genres

    def test_matches_real_genre_ini_exactly(self):
        """The claim that genre.ini is redundant, checked against the shipped files."""
        import configparser
        base = os.path.join(os.path.dirname(os.path.dirname(MameSources.__file__)), "MameFiles")
        catlist_file = os.path.join(base, "catlist.ini")
        genre_file = os.path.join(base, "genre.ini")
        if not (os.path.isfile(catlist_file) and os.path.isfile(genre_file)):
            return

        catlist = configparser.ConfigParser(allow_no_value=True)
        catlist.read(catlist_file)
        genre = configparser.ConfigParser(allow_no_value=True)
        genre.read(genre_file)

        derived = MameSources.derive_genres(catlist)
        actual = {section: {name for name, _ in genre.items(section)}
                  for section in genre.sections()
                  if section not in MameSources.CATLIST_SKIP_SECTIONS}
        assert derived == actual


class TestExtraction:
    def test_reads_every_machine(self, machines):
        assert len(machines) == 22

    def test_captures_build(self, xml_path):
        assert MameSources.extract_machines(xml_path)["build"] == "0.252"

    def test_records_machine_attributes(self, machines):
        by_name = {record["n"]: record for record in machines}
        assert by_name["biosset"]["bios"] is True
        assert by_name["mechgame"]["mech"] is True
        assert by_name["devthing"]["dev"] is True
        assert by_name["devthing"]["run"] is False
        assert by_name["goodgame"]["run"] is True
        assert by_name["clonemerged"]["c"] == "parentchd"
        assert by_name["goodgame"]["c"] is None

    def test_screenless_is_absence_of_display(self, machines):
        by_name = {record["n"]: record for record in machines}
        assert by_name["blindgame"]["scr"] is True
        assert by_name["mechgame"]["scr"] is True
        assert by_name["goodgame"]["scr"] is False
        # An LCD is still a display.
        assert by_name["boardgame1"]["scr"] is False

    def test_nodump_disk_does_not_hide_a_dumped_one(self, machines):
        by_name = {record["n"]: record for record in machines}
        assert by_name["twodisk"]["dk"] == [["ok", None]]

    def test_disk_names_are_recorded(self, machines):
        """The name is what the file is called; a folder guess is not enough."""
        by_name = {record["n"]: record for record in machines}
        assert by_name["parentchd"]["dk"] == [["pdisk", None]]
        assert by_name["cloneown"]["dk"] == [["odisk", None]]

    def test_merge_attribute_is_recorded(self, machines):
        by_name = {record["n"]: record for record in machines}
        assert by_name["clonemerged"]["dk"] == [["pdisk", "pdisk"]]
        assert by_name["cloneown"]["dk"] == [["odisk", None]]

    def test_machine_without_disk(self, machines):
        by_name = {record["n"]: record for record in machines}
        assert by_name["goodgame"]["dk"] == []

    def test_progress_callback_reports_completion(self, xml_path):
        calls = []
        MameSources.extract_machines(xml_path, on_progress=lambda p, t, c: calls.append((p, t, c)))
        assert calls and calls[-1][0] == calls[-1][1]
        assert calls[-1][2] == 22


class TestCache:
    def test_roundtrip(self, xml_path):
        data = MameSources.load_machines(xml_path)
        again = MameSources.load_machines(xml_path)
        assert again["machines"] == data["machines"]

    def test_second_read_does_not_touch_the_xml(self, xml_path, monkeypatch):
        MameSources.load_machines(xml_path)

        def explode(*_args, **_kwargs):
            raise AssertionError("XML was re-parsed instead of using the cache")

        monkeypatch.setattr(MameSources, "extract_machines", explode)
        assert len(MameSources.load_machines(xml_path)["machines"]) == 22

    def test_modified_xml_invalidates_cache(self, tmp_path, xml_path):
        copy = tmp_path / "mame.xml"
        shutil.copy(xml_path, copy)
        first = MameSources.cache_path_for(str(copy))
        os.utime(copy, (0, 0))
        assert MameSources.cache_path_for(str(copy)) != first

    def test_refresh_rewrites_the_cache(self, xml_path, monkeypatch):
        MameSources.load_machines(xml_path)
        calls = []
        original = MameSources.extract_machines
        monkeypatch.setattr(MameSources, "extract_machines",
                            lambda *a, **k: (calls.append(1), original(*a, **k))[1])
        MameSources.load_machines(xml_path, refresh=True)
        assert len(calls) == 1

    def test_no_cache_writes_nothing(self, xml_path):
        MameSources.load_machines(xml_path, use_cache=False)
        assert not os.path.exists(MameSources.cache_path_for(xml_path))

    def test_stale_cache_version_is_rejected(self, tmp_path):
        stale = tmp_path / "c.json"
        stale.write_text('{"cache_version": -1, "machines": []}')
        assert MameSources.load_cache(str(stale)) is None

    def test_corrupt_cache_is_rejected(self, tmp_path):
        broken = tmp_path / "c.json"
        broken.write_text("{not json")
        assert MameSources.load_cache(str(broken)) is None


class TestContentSignature:
    """The digest that decides whether a machine's zip changed between releases.

    Pleasuredome's non-merged zips embed the BIOS and device ROMs, so the digest has
    to cover the whole closure, not just the machine's own <rom> entries.
    """

    def sign(self, tmp_path, body, name="sig.xml"):
        path = tmp_path / name
        path.write_text(f'<?xml version="1.0"?>\n<mame build="0.289 (mame0289)">\n'
                        f'{body}\n</mame>\n')
        machines = MameSources.load_machines(str(path), use_cache=False)["machines"]
        return {machine["n"]: machine["sig"] for machine in machines}

    def machine(self, name, roms=(), extra="", children=""):
        rom_tags = "".join(f'<rom name="{n}" size="{s}" sha1="{h}"/>'
                           for n, s, h in roms)
        return (f'<machine name="{name}" {extra}><description>{name}</description>'
                f'<driver status="good" emulation="good"/><display type="raster"/>'
                f'{rom_tags}{children}</machine>')

    def test_identical_roms_give_identical_digests(self, tmp_path):
        one = self.sign(tmp_path, self.machine("a", [("x.bin", "16", "aa")]), "one.xml")
        two = self.sign(tmp_path, self.machine("a", [("x.bin", "16", "aa")]), "two.xml")
        assert one["a"] == two["a"]

    def test_a_changed_rom_changes_the_digest(self, tmp_path):
        before = self.sign(tmp_path, self.machine("a", [("x.bin", "16", "aa")]), "b.xml")
        after = self.sign(tmp_path, self.machine("a", [("x.bin", "16", "bb")]), "a.xml")
        assert before["a"] != after["a"]

    def test_rom_order_in_the_xml_does_not_matter(self, tmp_path):
        one = self.sign(tmp_path, self.machine(
            "a", [("x.bin", "1", "aa"), ("y.bin", "1", "bb")]), "one.xml")
        two = self.sign(tmp_path, self.machine(
            "a", [("y.bin", "1", "bb"), ("x.bin", "1", "aa")]), "two.xml")
        assert one["a"] == two["a"]

    def test_a_bios_change_changes_every_machine_that_embeds_it(self, tmp_path):
        def document(bios_hash):
            return (self.machine("neogeo", [("bios.rom", "128", bios_hash)],
                                 extra='isbios="yes"') +
                    self.machine("mslug", [("m.rom", "64", "cc")],
                                 extra='romof="neogeo"'))
        before = self.sign(tmp_path, document("aa"), "b.xml")
        after = self.sign(tmp_path, document("bb"), "a.xml")
        # The zip carries the BIOS, so it is a different file even though the game's
        # own ROMs are untouched.
        assert before["mslug"] != after["mslug"]

    def test_a_device_change_changes_the_machines_that_use_it(self, tmp_path):
        def document(device_hash):
            return (self.machine("namco51", [("51xx.bin", "32", device_hash)],
                                 extra='isdevice="yes" runnable="no"') +
                    self.machine("galaga", [("g.rom", "64", "cc")],
                                 children='<device_ref name="namco51"/>'))
        before = self.sign(tmp_path, document("aa"), "b.xml")
        after = self.sign(tmp_path, document("bb"), "a.xml")
        assert before["galaga"] != after["galaga"]

    def test_a_parent_that_is_not_a_bios_contributes_nothing(self, tmp_path):
        # A clone's <rom> list already spells out every file it needs, merge attribute
        # and all, so folding the parent in again would double-count it.
        def document(parent_hash):
            return (self.machine("puckman", [("p.rom", "64", parent_hash)]) +
                    self.machine("pacman", [("p.rom", "64", "aa")],
                                 extra='cloneof="puckman" romof="puckman"'))
        before = self.sign(tmp_path, document("aa"), "b.xml")
        after = self.sign(tmp_path, document("zz"), "a.xml")
        assert before["pacman"] == after["pacman"]
        assert before["puckman"] != after["puckman"]

    def test_device_chains_are_followed(self, tmp_path):
        def document(deep_hash):
            return (self.machine("deep", [("d.bin", "8", deep_hash)],
                                 extra='isdevice="yes" runnable="no"') +
                    self.machine("mid", [("m.bin", "8", "bb")],
                                 extra='isdevice="yes" runnable="no"',
                                 children='<device_ref name="deep"/>') +
                    self.machine("game", [("g.bin", "8", "cc")],
                                 children='<device_ref name="mid"/>'))
        before = self.sign(tmp_path, document("aa"), "b.xml")
        after = self.sign(tmp_path, document("bb"), "a.xml")
        assert before["game"] != after["game"]

    def test_a_device_cycle_does_not_hang(self, tmp_path):
        body = (self.machine("one", [("a", "1", "aa")],
                             extra='isdevice="yes" runnable="no"',
                             children='<device_ref name="two"/>') +
                self.machine("two", [("b", "1", "bb")],
                             extra='isdevice="yes" runnable="no"',
                             children='<device_ref name="one"/>'))
        assert set(self.sign(tmp_path, body)) == {"one", "two"}

    def test_nodump_roms_are_ignored(self, tmp_path):
        with_bad = ('<machine name="a"><description>a</description>'
                    '<driver status="good" emulation="good"/><display type="raster"/>'
                    '<rom name="x.bin" size="16" sha1="aa"/>'
                    '<rom name="missing.bin" size="16" status="nodump"/></machine>')
        plain = self.machine("a", [("x.bin", "16", "aa")])
        # A nodump ROM is not in the zip, so its presence in the XML must not move
        # the digest -- otherwise every such machine looks changed at every release.
        assert (self.sign(tmp_path, with_bad, "b.xml")["a"] ==
                self.sign(tmp_path, plain, "p.xml")["a"])

    def test_the_bulky_intermediate_lists_are_not_cached(self, tmp_path):
        path = tmp_path / "c.xml"
        path.write_text('<?xml version="1.0"?><mame build="0.289">' +
                        self.machine("a", [("x", "1", "aa")]) + '</mame>')
        record = MameSources.load_machines(str(path), use_cache=False)["machines"][0]
        assert "_roms" not in record and "_devs" not in record
        assert len(record["sig"]) == 16


class TestLocateSet:
    """A torrent lands in a folder named after itself, one level below the folder the
    download client was pointed at. Planning against the parent finds nothing at all.
    """

    def test_a_folder_of_zips_is_used_as_it_is(self, tmp_path):
        (tmp_path / "galaga.zip").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "roms") == str(tmp_path)

    def test_the_set_one_level_down_is_found(self, tmp_path):
        inner = tmp_path / "MAME 0.289 ROMs (non-merged)"
        inner.mkdir()
        for name in ("galaga", "pacman"):
            (inner / f"{name}.zip").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "roms") == str(inner)

    def test_the_fullest_folder_wins(self, tmp_path):
        """A download folder holds other torrents too."""
        small = tmp_path / "something else"
        small.mkdir()
        (small / "one.zip").write_bytes(b"x")
        big = tmp_path / "MAME 0.289 ROMs (non-merged)"
        big.mkdir()
        for index in range(5):
            (big / f"game{index}.zip").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "roms") == str(big)

    def test_nothing_to_descend_into_leaves_the_path_alone(self, tmp_path):
        assert MameSources.locate_set(str(tmp_path), "roms") == str(tmp_path)

    def test_a_path_that_is_not_there_is_left_alone(self, tmp_path):
        missing = str(tmp_path / "nope")
        assert MameSources.locate_set(missing, "roms") == missing

    def test_chd_folders_are_recognised_by_their_subfolders(self, tmp_path):
        inner = tmp_path / "MAME 0.289 CHDs (merged)"
        (inner / "area51").mkdir(parents=True)
        (inner / "area51" / "area51.chd").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "chds") == str(inner)

    def test_a_set_two_levels_down_is_left_alone(self, tmp_path):
        """A hand-made backup of the full set under "mame/" is not Marquee's: a plan
        that quietly read from it would tie the library to a folder nobody manages."""
        backup = tmp_path / "mame" / "MAME 0.288 CHDs (merged)"
        (backup / "area51").mkdir(parents=True)
        (backup / "area51" / "area51.chd").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "chds") == str(tmp_path)
        roms = tmp_path / "mame" / "MAME 0.289 ROMs (non-merged)"
        roms.mkdir()
        (roms / "galaga.zip").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "roms") == str(tmp_path)

    def test_a_folder_named_for_the_release_beats_a_fuller_one(self, tmp_path):
        """An upgrade's download folder holds the old set too, and the old one is
        the fuller one."""
        old = tmp_path / "MAME 0.288 ROMs (non-merged)"
        old.mkdir()
        for index in range(9):
            (old / f"game{index}.zip").write_bytes(b"x")
        new = tmp_path / "MAME 0.289 ROMs (non-merged)"
        new.mkdir()
        (new / "galaga.zip").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "roms") == str(old)
        assert MameSources.locate_set(str(tmp_path), "roms", version="0.289") == str(new)
        # Named for another release is still better than nothing.
        assert MameSources.locate_set(str(tmp_path), "roms", version="0.290") == str(old)

    def test_holds_disks_is_public(self, tmp_path):
        assert MameSources.holds_disks(str(tmp_path)) is False
        (tmp_path / "area51").mkdir()
        (tmp_path / "area51" / "area51.chd").write_bytes(b"x")
        assert MameSources.holds_disks(str(tmp_path)) is True
        assert MameSources.holds_disks(None) is False

    def test_a_chd_set_already_pointed_at_is_used_as_it_is(self, tmp_path):
        (tmp_path / "area51").mkdir()
        (tmp_path / "area51" / "area51.chd").write_bytes(b"x")
        assert MameSources.locate_set(str(tmp_path), "chds") == str(tmp_path)


class TestCachePruning:
    """A parse cache is keyed on the XML's path, its timestamp and CACHE_VERSION, so
    every version bump writes a new file. Nothing removed the old ones: 5 MB apiece,
    once per bump, kept for ever in a volume the user never looks in.
    """

    def test_a_superseded_cache_of_the_same_xml_is_removed(self, xml_path, tmp_path,
                                                           monkeypatch):
        monkeypatch.setattr(MameSources, "cache_dir", lambda: str(tmp_path))
        MameSources.load_machines(xml_path, refresh=True)
        live = MameSources.cache_path_for(xml_path)
        import json
        stale = tmp_path / "machines-0000000000000000.json"
        stale.write_text(json.dumps({"cache_version": 1, "machines": [],
                                     "source": os.path.abspath(xml_path)}))
        MameSources.load_machines(xml_path, refresh=True)
        assert os.path.isfile(live)
        assert not stale.exists()

    def test_a_cache_of_another_xml_is_left_alone(self, xml_path, tmp_path, monkeypatch):
        monkeypatch.setattr(MameSources, "cache_dir", lambda: str(tmp_path))
        other = tmp_path / "machines-1111111111111111.json"
        other.write_text('{"cache_version": 7, "machines": [], "source": "/elsewhere.xml"}')
        MameSources.load_machines(xml_path, refresh=True)
        assert other.exists()

    def test_something_that_is_not_a_cache_is_left_alone(self, xml_path, tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(MameSources, "cache_dir", lambda: str(tmp_path))
        junk = tmp_path / "machines-2222222222222222.json"
        junk.write_text("not json at all")
        MameSources.load_machines(xml_path, refresh=True)
        assert junk.exists()


class TestLegacyCaches:
    """Caches written before the source was recorded. One whose version no longer
    matches can never be loaded again, so it is only taking up room."""

    def test_an_unreadable_old_version_is_dropped(self, xml_path, tmp_path, monkeypatch):
        import json
        monkeypatch.setattr(MameSources, "cache_dir", lambda: str(tmp_path))
        old = tmp_path / "machines-3333333333333333.json"
        old.write_text(json.dumps({"cache_version": MameSources.CACHE_VERSION - 1,
                                   "machines": []}))
        MameSources.load_machines(xml_path, refresh=True)
        assert not old.exists()

    def test_one_at_the_current_version_is_kept(self, xml_path, tmp_path, monkeypatch):
        """It has no source recorded, so there is no way to know it is not in use."""
        import json
        monkeypatch.setattr(MameSources, "cache_dir", lambda: str(tmp_path))
        current = tmp_path / "machines-4444444444444444.json"
        current.write_text(json.dumps({"cache_version": MameSources.CACHE_VERSION,
                                       "machines": []}))
        MameSources.load_machines(xml_path, refresh=True)
        assert current.exists()


class TestAStaleFileIsNotAMatch:
    """A catlist.ini for another release is worse than none at all.

    It is found first, used, and the run then stops on a version mismatch -- over a
    file the user very likely did not know was there. MAME leaves one in ~/.mame, and
    this project used to ship a 0.252 copy of its own that did exactly this to anybody
    running a newer romset from a checkout.
    """

    def write(self, directory, name, version):
        path = os.path.join(str(directory), name)
        with open(path, "w", encoding="utf-8") as handle:
            if version:
                handle.write(f";; CATLIST.ini {version} / 06-Mar-23 / MAME {version} ;;\n")
            handle.write("[ROOT_FOLDER]\n")
        return path

    def test_the_matching_one_is_chosen_over_the_nearer_one(self, tmp_path):
        old = tmp_path / "first"
        new = tmp_path / "second"
        old.mkdir()
        new.mkdir()
        self.write(old, "catlist.ini", "0.252")
        right = self.write(new, "catlist.ini", "0.289")
        found = MameSources.find_support_file("catlist.ini", [str(old), str(new)],
                                              wanted_version="0.289")
        assert found == right

    def test_only_a_mismatched_one_raises_rather_than_returning_it(self, tmp_path):
        stale = self.write(tmp_path, "catlist.ini", "0.252")
        with pytest.raises(MameSources.WrongVersionOnDisk) as error:
            MameSources.find_support_file("catlist.ini", [str(tmp_path)],
                                          wanted_version="0.289")
        assert error.value.path == stale
        assert error.value.version == "0.252"

    def test_a_file_with_no_header_is_still_a_candidate(self, tmp_path):
        """Nothing says it is wrong, and refusing it would break hand-made files."""
        plain = self.write(tmp_path, "catlist.ini", None)
        assert MameSources.find_support_file("catlist.ini", [str(tmp_path)],
                                             wanted_version="0.289") == plain

    def test_without_a_wanted_version_nothing_is_rejected(self, tmp_path):
        stale = self.write(tmp_path, "catlist.ini", "0.252")
        assert MameSources.find_support_file("catlist.ini", [str(tmp_path)]) == stale

    def test_the_same_release_written_two_ways_is_one_release(self):
        assert MameSources.same_version("0.289", 0.289)
        assert MameSources.same_version("0.289", "0.2890")
        # "0.9" sorts after "0.289" as a string and is not a newer MAME.
        assert not MameSources.same_version("0.9", "0.289")
        assert not MameSources.same_version(None, "0.289")


class TestTwoOlderDiskSets:
    """CHD sets trail the ROM sets: "0.287 CHDs" and "0.288 CHDs" beside a 0.289
    plan. A disk set measures 1 or 0, so the choice fell to directory order."""

    def make(self, root, name):
        disk = root / name / "area51"
        disk.mkdir(parents=True)
        (disk / "area51.chd").write_bytes(b"MComprHD")

    @pytest.mark.parametrize("order", [("0.287", "0.288"), ("0.288", "0.287")])
    def test_the_newest_not_past_the_plan_wins(self, tmp_path, order):
        for version in order:
            self.make(tmp_path, f"MAME {version} CHDs (merged)")
        chosen = MameSources.locate_set(str(tmp_path), "chds", version="0.289")
        assert chosen.endswith("MAME 0.288 CHDs (merged)")

    def test_an_older_one_beats_a_newer_one(self, tmp_path):
        self.make(tmp_path, "MAME 0.290 CHDs (merged)")
        self.make(tmp_path, "MAME 0.286 CHDs (merged)")
        chosen = MameSources.locate_set(str(tmp_path), "chds", version="0.289")
        assert chosen.endswith("MAME 0.286 CHDs (merged)")
