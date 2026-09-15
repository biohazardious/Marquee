"""Mapping a selection onto torrent files, and costing it.

The numbers in TestRealSets come from the live Pleasuredome torrents, measured on
2026-09-14. They are the claims this project makes, so they are asserted.
"""
import pytest

from marquee.acquire import (TorrentFile, chd_selection, delta, disks_for,
                                index_files, piece_length, rom_selection)

PIECE = 1024


def rom_files(names, size=PIECE, start=0):
    """One 1 KiB file per machine, each its own piece: costs are then exact."""
    return [{"index": start + i, "path": f"MAME 0.289 ROMs (non-merged)/{n}.zip",
             "size": size, "piece_range": [start + i, start + i], "progress": 0.0}
            for i, n in enumerate(names)]


class TestRomSelection:
    def test_each_machine_maps_to_its_own_zip(self):
        files = rom_files(["galaga", "mslug", "pacman"])
        chosen = rom_selection(files, ["galaga", "pacman"])
        assert chosen.indices == [0, 2]
        assert chosen.selected_bytes == 2 * PIECE
        assert chosen.missing == []

    def test_a_machine_with_no_file_is_reported_not_silently_dropped(self):
        chosen = rom_selection(rom_files(["galaga"]), ["galaga", "notinset"])
        assert chosen.indices == [0]
        assert chosen.missing == ["notinset"]

    def test_the_set_total_is_kept_alongside_the_selection(self):
        chosen = rom_selection(rom_files(["a", "b", "c", "d"]), ["a"])
        assert chosen.selected_bytes == PIECE
        assert chosen.total_bytes == 4 * PIECE

    def test_non_zip_entries_are_ignored(self):
        files = rom_files(["galaga"]) + [
            {"index": 9, "path": "MAME 0.289 ROMs (non-merged)/readme.txt",
             "size": 10, "piece_range": [9, 9], "progress": 0.0}]
        assert rom_selection(files, ["galaga", "readme"]).missing == ["readme"]

    def test_selecting_nothing_costs_nothing(self):
        chosen = rom_selection(rom_files(["a", "b"]), [])
        assert chosen.indices == []
        assert chosen.download_bytes == 0


class TestPieceCost:
    def test_files_sharing_a_piece_are_charged_once(self):
        # Two 400-byte files inside one 1 KiB piece.
        files = [{"index": 0, "path": "s/a.zip", "size": 400, "piece_range": [0, 0],
                  "progress": 0.0},
                 {"index": 1, "path": "s/b.zip", "size": 400, "piece_range": [0, 0],
                  "progress": 0.0}]
        chosen = rom_selection(files, ["a", "b"], total_pieces=1)
        assert chosen.piece_bytes == 1024
        assert chosen.selected_bytes == 800

    def test_one_wanted_file_still_pulls_its_neighbour_s_piece(self):
        files = [{"index": 0, "path": "s/a.zip", "size": 400, "piece_range": [0, 0],
                  "progress": 0.0},
                 {"index": 1, "path": "s/b.zip", "size": 400, "piece_range": [0, 0],
                  "progress": 0.0}]
        chosen = rom_selection(files, ["a"], total_pieces=1)
        # This overhead is unavoidable -- BitTorrent's unit is the piece, not the file.
        assert chosen.piece_bytes == 1024
        assert chosen.overhead_bytes == 624

    def test_already_complete_files_are_not_counted_as_download(self):
        files = rom_files(["a", "b"])
        files[0]["progress"] = 1.0
        chosen = rom_selection(files, ["a", "b"])
        assert chosen.present == [0]
        assert chosen.download_bytes == PIECE

    def test_piece_length_is_inferred_from_the_file_table(self):
        files = index_files(rom_files(["a", "b", "c", "d"], size=4 * 1024 * 1024))
        assert piece_length(files, total_pieces=4) == 4 * 1024 * 1024

    def test_a_table_without_piece_ranges_falls_back_to_plain_sizes(self):
        files = [{"index": 0, "path": "s/a.zip", "size": 500, "progress": 0.0}]
        chosen = rom_selection(files, ["a"])
        assert chosen.piece_bytes == 500
        assert chosen.overhead_bytes == 0


class TestChdSelection:
    def files(self):
        return [{"index": 0, "path": "MAME 0.288 CHDs (merged)/dlair/dlair.chd",
                 "size": PIECE, "piece_range": [0, 0], "progress": 0.0},
                {"index": 1, "path": "MAME 0.288 CHDs (merged)/area51/area51.chd",
                 "size": PIECE, "piece_range": [1, 1], "progress": 0.0}]

    def test_a_disk_in_the_machines_own_folder_is_found(self):
        chosen = chd_selection(self.files(), [("dlair", None, "dlair")])
        assert chosen.indices == [0]

    def test_a_clones_disk_is_found_in_the_parents_folder(self):
        # In a merged CHD set a clone's disk lives under the parent. Looking only in
        # the clone's own folder reports every clone as missing, which it is not.
        chosen = chd_selection(self.files(), [("dlaire", "dlair", "dlair")])
        assert chosen.indices == [0]

    def test_the_machines_own_folder_wins_over_the_parents(self):
        files = self.files() + [
            {"index": 2, "path": "MAME 0.288 CHDs (merged)/dlaire/dlair.chd",
             "size": PIECE, "piece_range": [2, 2], "progress": 0.0}]
        assert chd_selection(files, [("dlaire", "dlair", "dlair")]).indices == [2]

    def test_clones_sharing_one_merged_disk_fetch_it_once(self):
        disks = [("dlair", None, "dlair"), ("dlaire", "dlair", "dlair"),
                 ("dlairf", "dlair", "dlair")]
        chosen = chd_selection(self.files(), disks)
        assert chosen.indices == [0]
        assert chosen.selected_bytes == PIECE

    def test_a_disk_the_set_does_not_have_is_reported(self):
        # Normal, not an error: the CHD set routinely lags the ROM set by a release.
        chosen = chd_selection(self.files(), [("newgame", None, "newdisk")])
        assert chosen.missing == ["newgame/newdisk"]
        assert chosen.indices == []


class TestDisksFor:
    def test_only_machines_with_disks_are_returned(self):
        records = {"dlair": {"dk": [["dlair", None]], "c": None},
                   "galaga": {"dk": [], "c": None}}
        assert disks_for(["dlair", "galaga"], records) == [("dlair", None, "dlair", None)]

    def test_a_clone_carries_its_parent(self):
        records = {"dlaire": {"dk": [["dlair", "dlair"]], "c": "dlair"}}
        assert disks_for(["dlaire"], records) == [("dlaire", "dlair", "dlair", "dlair")]

    def test_a_disk_merged_under_another_name_is_found_in_the_parents_folder(self):
        """popn1k's 803kaa11 is stored as popn1/803_ta_hdd.chd in a merged set."""
        files = [{"index": 0, "path": "MAME CHDs/popn1/803_ta_hdd.chd", "size": 10,
                  "piece_range": [0, 0], "progress": 0.0}]
        chosen = chd_selection(files, [("popn1k", "popn1", "803kaa11", "803_ta_hdd")],
                               total_pieces=1)
        assert chosen.indices == [0]
        assert chosen.missing == []

    def test_a_machine_with_several_disks_yields_each(self):
        records = {"a51site4": {"dk": [["d1", None], ["d2", None]], "c": None}}
        assert len(disks_for(["a51site4"], records)) == 2

    def test_an_unknown_machine_is_skipped(self):
        assert disks_for(["ghost"], {}) == []


class TestDelta:
    def test_an_unchanged_signature_means_nothing_to_fetch(self):
        result = delta({"galaga": "aa"}, {"galaga": "aa"}, ["galaga"])
        assert result == {"changed": [], "added": [], "unchanged": ["galaga"]}

    def test_a_changed_signature_is_fetched(self):
        result = delta({"galaga": "aa"}, {"galaga": "bb"}, ["galaga"])
        assert result["changed"] == ["galaga"]

    def test_a_machine_the_old_release_never_had_counts_as_new(self):
        result = delta({}, {"newgame": "aa"}, ["newgame"])
        assert result["added"] == ["newgame"]

    def test_machines_outside_the_selection_are_ignored(self):
        # The saving comes from only ever considering what the profile wants.
        result = delta({"a": "1", "b": "1"}, {"a": "2", "b": "2"}, ["a"])
        assert result["changed"] == ["a"]
        assert "b" not in result["changed"] + result["added"] + result["unchanged"]


class TestSelectionReporting:
    def test_describe_names_files_and_bytes(self):
        chosen = rom_selection(rom_files(["a", "b"]), ["a"])
        assert "1 files" in chosen.describe()

    def test_a_fully_present_selection_needs_no_download(self):
        files = rom_files(["a", "b"])
        for handle in files:
            handle["progress"] = 1.0
        chosen = rom_selection(files, ["a", "b"])
        assert chosen.download_bytes == 0
        assert chosen.present == [0, 1]

    def test_the_piece_cost_of_the_whole_selection_is_still_reported(self):
        # Needed to answer "how big is this library", separately from "what is left".
        files = rom_files(["a", "b"])
        files[0]["progress"] = 1.0
        chosen = rom_selection(files, ["a", "b"])
        assert chosen.piece_bytes == 2 * PIECE
        assert chosen.remaining_bytes == PIECE


class TestRealSets:
    """The measured claims, asserted.

    Piece sizes and totals are the live Pleasuredome 0.289 non-merged ROM set and
    0.288 merged CHD set as at 2026-09-14.
    """

    def test_the_rom_sets_piece_size_is_recovered_from_its_totals(self):
        files = [TorrentFile(index=0, path="s/a.zip", size=163_225_373_747,
                             piece_range=(0, 38_915))]
        assert piece_length(files, total_pieces=38_916) == 4 * 1024 * 1024

    def test_the_chd_sets_piece_size_is_recovered_from_its_totals(self):
        files = [TorrentFile(index=0, path="s/a.chd", size=1_126_649_399_834,
                             piece_range=(0, 67_153))]
        assert piece_length(files, total_pieces=67_154) == 16 * 1024 * 1024


class TestTorrentFile:
    def test_basename_strips_the_set_folder(self):
        handle = TorrentFile(index=0, path="MAME 0.289 ROMs (non-merged)/galaga.zip",
                             size=1)
        assert handle.basename == "galaga.zip"

    def test_already_built_handles_pass_straight_through(self):
        handle = TorrentFile(index=3, path="s/a.zip", size=9)
        assert index_files([handle]) == [handle]


@pytest.mark.parametrize("names,wanted,expected", [
    (["a", "b", "c"], ["a", "b", "c"], [0, 1, 2]),
    (["a", "b", "c"], ["c"], [2]),
    (["a", "b", "c"], ["z"], []),
])
def test_selection_indices(names, wanted, expected):
    assert rom_selection(rom_files(names), wanted).indices == expected
