"""Checking a library against the release it is supposed to be.

A file with the right name is not the right file. MAME rebuilds ROM sets between
releases -- a bad dump replaced, a chip renamed -- and a library that decides "we have
it" from the filename alone carries those changes for ever.
"""
import os
import zipfile

import pytest

from marquee import sources, verify


def make_zip(path, entries):
    """entries: {name: bytes}. The CRC comes out of the data, as MAME's does."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def crc_of(data):
    import zlib
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"


@pytest.fixture
def rom(tmp_path):
    data = b"the original dump"
    path = make_zip(str(tmp_path / "Maze" / "galaga.zip"), {"galaga.1": data})
    return path, [("galaga.1", crc_of(data), False)]


class TestOneMachine:
    def test_the_right_contents_are_current(self, rom):
        path, expected = rom
        assert verify.check(path, expected) == (verify.CURRENT, [])

    def test_a_changed_dump_is_stale(self, rom, tmp_path):
        path, expected = rom
        wrong = [("galaga.1", "deadbeef", False)]
        state, detail = verify.check(path, wrong)
        assert state == verify.STALE
        assert "galaga.1" in detail[0] and "deadbeef" in detail[0]

    def test_a_rom_of_its_own_that_is_missing_is_stale(self, rom):
        path, expected = rom
        state, detail = verify.check(path, expected + [("galaga.2", "abcd1234", False)])
        assert state == verify.STALE
        assert detail == ["galaga.2: missing"]

    def test_a_missing_inherited_rom_is_only_incomplete(self, rom):
        """That is what a merged or split set looks like: the parent's zip has it."""
        path, expected = rom
        state, detail = verify.check(path, expected + [("parent.1", "abcd1234", True)])
        assert state == verify.INCOMPLETE
        assert detail == ["parent.1: missing"]

    def test_extra_entries_are_fine(self, tmp_path):
        """A non-merged zip carries its BIOS and device ROMs too."""
        data = b"the original dump"
        path = make_zip(str(tmp_path / "x" / "a.zip"),
                        {"galaga.1": data, "neogeo.bios": b"something else"})
        assert verify.check(path, [("galaga.1", crc_of(data), False)])[0] == verify.CURRENT

    def test_a_file_that_is_not_there(self, tmp_path):
        assert verify.check(str(tmp_path / "nope.zip"),
                            [("a", "0", False)])[0] == verify.ABSENT

    def test_something_that_is_not_a_zip(self, tmp_path):
        path = tmp_path / "broken.zip"
        path.write_bytes(b"this is not an archive")
        state, detail = verify.check(str(path), [("a", "0", False)])
        assert state == verify.DAMAGED
        assert detail

    def test_a_release_that_records_no_crc_is_not_called_wrong(self, rom):
        path, _expected = rom
        assert verify.check(path, [("galaga.1", "", False)])[0] == verify.CURRENT

    def test_a_machine_nothing_is_known_about_is_left_alone(self, rom):
        path, _expected = rom
        assert verify.check(path, None) == (verify.CURRENT, [])

    def test_a_foldered_zip_is_matched_by_name(self, tmp_path):
        data = b"the original dump"
        path = make_zip(str(tmp_path / "y" / "b.zip"), {"galaga/galaga.1": data})
        assert verify.check(path, [("galaga.1", crc_of(data), False)])[0] == verify.CURRENT


class TestARun:
    class Item:
        def __init__(self, name, folder):
            self.name, self.folder = name, folder
            self.state, self.state_detail = "", []

    def test_it_marks_each_machine_and_counts_them(self, tmp_path):
        data = b"a"
        make_zip(str(tmp_path / "Maze" / "good.zip"), {"good.1": data})
        make_zip(str(tmp_path / "Maze" / "old.zip"), {"old.1": b"something else"})
        items = [self.Item("good", "Maze"), self.Item("old", "Maze"),
                 self.Item("gone", "Maze")]
        manifests = {"good": [("good.1", crc_of(data), False)],
                     "old": [("old.1", crc_of(data), False)],
                     "gone": [("gone.1", crc_of(data), False)]}
        counts = verify.check_library(items, manifests, str(tmp_path))
        assert [item.state for item in items] == [verify.CURRENT, verify.STALE,
                                                  verify.ABSENT]
        assert counts == {verify.CURRENT: 1, verify.STALE: 1, verify.ABSENT: 1}

    def test_stopping_leaves_what_it_had_and_does_not_invent_the_rest(self, tmp_path):
        data = b"a"
        for name in ("one", "two", "three"):
            make_zip(str(tmp_path / "Maze" / f"{name}.zip"), {f"{name}.1": data})
        items = [self.Item(name, "Maze") for name in ("one", "two", "three")]
        manifests = {item.name: [(f"{item.name}.1", crc_of(data), False)]
                     for item in items}
        asked = []

        def keep_going():
            asked.append(1)
            return len(asked) <= 2       # the third one is never looked at

        counts = verify.check_library(items, manifests, str(tmp_path),
                                      should_continue=keep_going)
        assert [item.state for item in items] == [verify.CURRENT, verify.CURRENT, ""]
        assert counts == {verify.CURRENT: 2}

    def test_the_summary_reads_worst_first(self):
        text = verify.summarise({verify.CURRENT: 10, verify.STALE: 2, verify.ABSENT: 1})
        assert text.startswith("2 stale")
        assert "10 current" in text


class TestReadingTheReleasesRomLists:
    def test_it_pulls_the_roms_out_of_the_xml(self, tmp_path):
        xml = tmp_path / "mame.xml"
        xml.write_text(
            '<?xml version="1.0"?><mame build="0.289 (mame0289)">'
            '<machine name="galaga">'
            '<rom name="gg1_1.3p" size="4096" crc="ab3a0f3d"/>'
            '<rom name="gg1_2.3m" size="4096" crc="1b280831" merge="gg1_2.3m"/>'
            '<rom name="bad.rom" size="4096" status="nodump"/>'
            '</machine>'
            '<machine name="nothing"><description>No roms</description></machine>'
            '</mame>')
        found = sources.rom_manifests(str(xml))
        assert found == {"galaga": [("gg1_1.3p", "ab3a0f3d", False),
                                    ("gg1_2.3m", "1b280831", True)]}

    def test_it_can_be_asked_for_only_what_is_being_checked(self, tmp_path):
        xml = tmp_path / "mame.xml"
        xml.write_text(
            '<?xml version="1.0"?><mame build="0.289">'
            '<machine name="a"><rom name="a.1" crc="1111" size="1"/></machine>'
            '<machine name="b"><rom name="b.1" crc="2222" size="1"/></machine>'
            '</mame>')
        assert set(sources.rom_manifests(str(xml), wanted={"b"})) == {"b"}


class TestWhereTheFileActuallyIs:
    """A library built to an older layout keeps files one folder from where this
    release wants them. The next run relocates them -- but until it has, looking only
    at the destination path calls a file that is right there absent. On the real
    library that was 871 machines reported missing that were not missing at all.
    """

    class Item:
        def __init__(self, name, folder):
            self.name, self.folder = name, folder
            self.state, self.state_detail = "", []

    def test_it_looks_where_the_file_is_now(self, tmp_path):
        data = b"a"
        make_zip(str(tmp_path / "Casino" / "Cards" / "jolycdcy.zip"), {"j.1": data})
        item = self.Item("jolycdcy", "Gambling/Cards")     # where 0.289 files it
        manifests = {"jolycdcy": [("j.1", crc_of(data), False)]}

        counts = verify.check_library([item], manifests, str(tmp_path))
        assert item.state == verify.ABSENT

        counts = verify.check_library(
            [item], manifests, str(tmp_path),
            where={"jolycdcy": "Casino/Cards/jolycdcy.zip"})
        assert item.state == verify.CURRENT
        assert counts == {verify.CURRENT: 1}

    def test_a_machine_not_in_the_map_is_read_where_it_belongs(self, tmp_path):
        data = b"a"
        make_zip(str(tmp_path / "Maze" / "here.zip"), {"h.1": data})
        item = self.Item("here", "Maze")
        counts = verify.check_library([item], {"here": [("h.1", crc_of(data), False)]},
                                      str(tmp_path), where={"somethingelse": "x/y.zip"})
        assert counts == {verify.CURRENT: 1}

    def test_the_old_copy_can_still_be_wrong(self, tmp_path):
        """Relocating it does not make it the right version."""
        make_zip(str(tmp_path / "Casino" / "Cards" / "old.zip"), {"o.1": b"yesterday"})
        item = self.Item("old", "Gambling/Cards")
        verify.check_library([item], {"old": [("o.1", crc_of(b"today"), False)]},
                             str(tmp_path), where={"old": "Casino/Cards/old.zip"})
        assert item.state == verify.STALE


class TestReadingThroughAnOpener:
    """A library on a share is not on this machine's disk: the files come through
    the backend, as seekable objects, by their library-relative path."""

    def test_the_same_verdicts_come_back(self, tmp_path):
        import io, zipfile
        from marquee import verify
        good = io.BytesIO()
        with zipfile.ZipFile(good, "w") as archive:
            archive.writestr("a.bin", b"ok")
        crc = f"{zipfile.ZipFile(io.BytesIO(good.getvalue())).getinfo('a.bin').CRC:08x}"
        files = {"Maze/Misc/pacman.zip": good.getvalue(), "Maze/Misc/junk.zip": b"nope"}

        def opener(path):
            if path not in files:
                raise FileNotFoundError(path)
            return io.BytesIO(files[path])

        class Item:
            def __init__(self, name):
                self.name, self.folder, self.state, self.state_detail = name, "Maze/Misc", "", []

        items = [Item("pacman"), Item("junk"), Item("gone")]
        manifests = {"pacman": [("a.bin", crc, False)], "junk": [("a.bin", crc, False)],
                     "gone": [("a.bin", crc, False)]}
        counts = verify.check_library(items, manifests, "/not/used", opener=opener)
        assert {item.name: item.state for item in items} == {
            "pacman": verify.CURRENT, "junk": verify.DAMAGED, "gone": verify.ABSENT}
        assert counts == {verify.CURRENT: 1, verify.DAMAGED: 1, verify.ABSENT: 1}


class TestADiskInTheLibrary:
    """A transfer that copied a disk before the download finished leaves a CHD-sized
    run of zeros with a real header. Sizes cannot tell; the check has to."""

    def test_a_whole_disk_is_current(self, tmp_path):
        path = tmp_path / "ok.chd"
        path.write_bytes(b"MComprHD" + b"c" * 200000)
        assert verify.check_disk(str(path)) == (verify.CURRENT, [])

    def test_a_zero_filled_disk_is_damaged(self, tmp_path):
        path = tmp_path / "zeros.chd"
        path.write_bytes(b"MComprHD" + b"\x00" * 200000)
        state, detail = verify.check_disk(str(path))
        assert state == verify.DAMAGED and "zero-filled" in detail[0]

    def test_something_that_is_not_a_chd_is_damaged(self, tmp_path):
        path = tmp_path / "not.chd"
        path.write_bytes(b"x" * 100)
        assert verify.check_disk(str(path))[0] == verify.DAMAGED

    def test_a_disk_that_is_not_there(self, tmp_path):
        assert verify.check_disk(str(tmp_path / "nope.chd")) == (verify.ABSENT, [])

    def test_it_reads_through_an_opener_too(self, tmp_path):
        path = tmp_path / "zeros.chd"
        path.write_bytes(b"MComprHD" + b"\x00" * 200000)
        opened = []

        def opener(relpath):
            opened.append(relpath)
            return open(path, "rb")
        assert verify.check_disk("Genre/x/zeros.chd", opener)[0] == verify.DAMAGED
        assert opened == ["Genre/x/zeros.chd"]

    def test_the_library_check_names_the_damaged_disk(self, tmp_path, categorised, config,
                                                      romset):
        from marquee import catalog
        from marquee import plan as planning
        built = planning.build(categorised, str(romset["rom_dir"]), str(romset["chd_dir"]),
                               catalog.folder_namer(config), config.allow_mature)
        item = next(one for one in built.items if one.name == "twodisk")
        root = romset["out_dir"]
        (root / item.folder / "twodisk").mkdir(parents=True)
        (root / item.folder / "twodisk" / "ok.chd").write_bytes(b"MComprHD" + b"\x00" * 200000)
        counts = verify.check_library([item], {}, str(root))
        assert counts == {verify.DAMAGED: 1}
        assert item.damaged_disks == [f"{item.folder}/twodisk/ok.chd"]
        assert any("ok.chd: zero-filled" in line for line in item.state_detail)

    def test_a_disk_on_its_way_to_a_new_folder_is_checked_where_it_is(self, tmp_path,
                                                                        categorised, config,
                                                                        romset):
        from marquee import catalog
        from marquee import plan as planning
        built = planning.build(categorised, str(romset["rom_dir"]), str(romset["chd_dir"]),
                               catalog.folder_namer(config), config.allow_mature)
        item = next(one for one in built.items if one.name == "twodisk")
        root = romset["out_dir"]
        (root / "Old" / "twodisk").mkdir(parents=True)
        (root / "Old" / "twodisk" / "ok.chd").write_bytes(b"MComprHD" + b"c" * 200000)
        counts = verify.check_library([item], {}, str(root),
                                      moved={f"{item.folder}/twodisk/ok.chd": "Old/twodisk/ok.chd"})
        assert item.damaged_disks == []
        assert verify.DAMAGED not in counts
