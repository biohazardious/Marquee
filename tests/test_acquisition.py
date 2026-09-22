"""The acquisition pipeline: costing a download, and driving a client to make it."""
import pytest

from marquee import acquisition
from marquee.acquisition import AcquisitionPlan, parse_mappings, remap
from marquee.errors import ConfigError, MarqueeError
from marquee.indexers.release import NON_MERGED, ROMS, Release

MAGNET_HASH = "deadbeef00000000000000000000000000000008"


def xml(tmp_path, machines, name="mame.xml"):
    """A minimal -listxml with one <rom> per machine, so signatures differ."""
    body = "".join(
        f'<machine name="{n}"><description>{n}</description>'
        f'<driver status="good" emulation="good"/><display type="raster"/>'
        f'<rom name="{n}.bin" size="8" sha1="{h}"/>'
        + "".join(f'<disk name="{d}"/>' for d in disks) +
        '</machine>'
        for n, h, disks in machines)
    path = tmp_path / name
    path.write_text(f'<?xml version="1.0"?><mame build="0.289 (mame0289)">{body}</mame>')
    return str(path)


def rom_files(names):
    return [{"index": i, "path": f"MAME 0.289 ROMs (non-merged)/{n}.zip",
             "size": 1000, "piece_range": [i, i], "progress": 0.0}
            for i, n in enumerate(names)]


class FakeClient:
    def __init__(self):
        self.added = []
        self.selected = []
        self.started = []
        self.categories_made = []
        self.files_list = [{"index": i, "path": f"s/{n}.zip", "size": 10,
                            "piece_range": [i, i], "progress": 0.0}
                           for i, n in enumerate("abc")]
        self.entry = {"hash": MAGNET_HASH, "content_path": "/downloads/MAME 0.289",
                      "save_path": "/downloads"}

    def ensure_category(self, name, save_path=None):
        self.categories_made.append((name, save_path))

    def add(self, source, save_path=None, category=None, tags=None, stopped=True):
        self.added.append({"source": source, "save_path": save_path,
                           "category": category, "tags": tags, "stopped": stopped})
        return MAGNET_HASH

    def wait_for_metadata(self, infohash, on_wait=None):
        return self.files_list

    def files(self, infohash):
        return self.files_list

    def narrow(self, infohash, indices):
        indices = list(indices)
        self.selected.append(indices)
        return {"selected": len(indices),
                "skipped": len(self.files_list) - len(indices),
                "wanted": len(indices), "kept_complete": 0}

    def select_only(self, infohash, indices):
        raise AssertionError("select_only stops a seeding torrent; never call it")

    def start(self, infohash):
        self.started.append(infohash)

    def one(self, infohash):
        return self.entry if infohash == MAGNET_HASH else None


@pytest.fixture
def client():
    return FakeClient()


class TestFreshPlan:
    def test_everything_wanted_is_fetched_when_the_library_is_empty(self, tmp_path):
        path = xml(tmp_path, [("a", "11", []), ("b", "22", []), ("c", "33", [])])
        made = acquisition.plan(["a", "b"], path, rom_files(["a", "b", "c"]))
        assert made.roms.indices == [0, 1]
        assert made.added == ["a", "b"]
        assert made.unchanged == []
        assert not made.is_upgrade

    def test_the_published_total_is_kept_next_to_the_download(self, tmp_path):
        path = xml(tmp_path, [("a", "11", []), ("b", "22", []), ("c", "33", [])])
        made = acquisition.plan(["a"], path, rom_files(["a", "b", "c"]))
        assert made.set_bytes == 3000
        assert made.library_bytes == 1000

    def test_describe_states_the_saving(self, tmp_path):
        path = xml(tmp_path, [("a", "11", []), ("b", "22", []), ("c", "33", [])])
        made = acquisition.plan(["a"], path, rom_files(["a", "b", "c"]))
        assert "% less" in made.describe()


class TestUpgradePlan:
    def library(self, tmp_path):
        """A 0.282-era library: `a` unchanged, `b` about to change, `c` not yet born."""
        old = xml(tmp_path, [("a", "11", []), ("b", "22", [])], "old.xml")
        new = xml(tmp_path, [("a", "11", []), ("b", "99", []), ("c", "33", [])],
                  "new.xml")
        return acquisition.signatures(old), new

    def test_unchanged_machines_are_not_fetched_again(self, tmp_path):
        current, new = self.library(tmp_path)
        made = acquisition.plan(["a", "b", "c"], new, rom_files(["a", "b", "c"]),
                                current_signatures=current,
                                from_version="0.282", to_version="0.289")
        assert made.unchanged == ["a"]
        assert made.changed == ["b"]
        assert made.added == ["c"]
        # Only the two that moved are downloaded, however many releases were crossed.
        assert made.roms.indices == [1, 2]
        assert made.is_upgrade

    def test_an_upgrade_that_changes_nothing_downloads_nothing(self, tmp_path):
        same = xml(tmp_path, [("a", "11", [])], "same.xml")
        current = acquisition.signatures(same)
        made = acquisition.plan(["a"], same, rom_files(["a"]),
                                current_signatures=current,
                                from_version="0.288", to_version="0.289")
        assert made.roms.indices == []
        assert made.download_bytes == 0

    def test_describe_counts_what_was_spared(self, tmp_path):
        current, new = self.library(tmp_path)
        made = acquisition.plan(["a", "b", "c"], new, rom_files(["a", "b", "c"]),
                                current_signatures=current,
                                from_version="0.282", to_version="0.289")
        assert "1 machines already correct" in made.describe()


class TestDisks:
    def test_chds_are_planned_for_every_wanted_machine_not_just_changed_ones(
            self, tmp_path):
        # A disk is not rebuilt between releases the way a zip is, so the signature
        # diff says nothing about it: wanting the machine is what matters.
        old = xml(tmp_path, [("dlair", "11", ["dlair"])], "old.xml")
        new = xml(tmp_path, [("dlair", "11", ["dlair"])], "new.xml")
        chds = [{"index": 0, "path": "MAME 0.288 CHDs (merged)/dlair/dlair.chd",
                 "size": 500, "piece_range": [0, 0], "progress": 0.0}]
        made = acquisition.plan(["dlair"], new, rom_files(["dlair"]), chd_files=chds,
                                current_signatures=acquisition.signatures(old))
        assert made.roms.indices == []          # the zip has not changed
        assert made.chds.indices == [0]         # the disk is still wanted

    def test_a_disk_the_chd_set_has_not_published_yet_is_reported(self, tmp_path):
        path = xml(tmp_path, [("newgame", "11", ["newdisk"])])
        made = acquisition.plan(["newgame"], path, rom_files(["newgame"]),
                                chd_files=[])
        assert made.missing == ["chds:newgame/newdisk"]


class TestStart:
    def release(self):
        return Release(kind=ROMS, version="0.289", variant=NON_MERGED,
                       infohash=MAGNET_HASH, name="MAME 0.289 ROMs (non-merged)",
                       magnet=f"magnet:?xt=urn:btih:{MAGNET_HASH}")

    def selection(self, indices):
        from marquee.acquire import Selection
        return Selection(indices=indices)

    def test_the_torrent_is_added_stopped_then_selected_then_started(self, client):
        acquisition.start(client, self.release(), self.selection([1]), "/downloads")
        assert client.added[0]["stopped"] is True
        assert client.selected == [[1]]
        assert client.started == [MAGNET_HASH]

    def test_it_is_filed_under_its_own_category(self, client):
        acquisition.start(client, self.release(), self.selection([1]), "/downloads")
        assert client.categories_made == [(acquisition.CATEGORY, "/downloads")]
        assert client.added[0]["category"] == acquisition.CATEGORY

    def test_the_release_is_tagged_with_its_kind_and_version(self, client):
        acquisition.start(client, self.release(), self.selection([0]), "/downloads")
        assert client.added[0]["tags"] == ["roms", "0.289"]

    def test_an_empty_selection_is_refused_rather_than_fetching_everything(self, client):
        with pytest.raises(MarqueeError, match="Nothing to fetch"):
            acquisition.start(client, self.release(), self.selection([]), "/downloads")
        assert client.added == []

    def test_resume_reselects_without_re_adding(self, client):
        acquisition.resume(client, MAGNET_HASH, [0, 2])
        assert client.added == []
        assert client.selected == [[0, 2]]
        assert client.started == [MAGNET_HASH]


class TestRemotePaths:
    def test_a_mapped_prefix_is_rewritten(self):
        mappings = [("/downloads", "/mnt/nas/dl")]
        assert remap("/downloads/MAME 0.289", mappings) == "/mnt/nas/dl/MAME 0.289"

    def test_the_root_itself_maps(self):
        assert remap("/downloads", [("/downloads", "/mnt/dl")]) == "/mnt/dl"

    def test_an_unmapped_path_is_left_alone(self):
        assert remap("/elsewhere/x", [("/downloads", "/mnt/dl")]) == "/elsewhere/x"

    def test_a_partial_name_match_is_not_a_prefix_match(self):
        # /downloads2 is not inside /downloads.
        assert remap("/downloads2/x", [("/downloads", "/mnt/dl")]) == "/downloads2/x"

    def test_the_first_matching_mapping_wins(self):
        mappings = [("/downloads/a", "/first"), ("/downloads", "/second")]
        assert remap("/downloads/a/x", mappings) == "/first/x"

    def test_torrent_root_uses_the_clients_content_path(self, client):
        found = acquisition.torrent_root(client, MAGNET_HASH,
                                         [("/downloads", "/mnt/dl")])
        assert found == "/mnt/dl/MAME 0.289"

    def test_a_torrent_the_client_does_not_have_is_an_error(self, client):
        with pytest.raises(MarqueeError, match="no torrent"):
            acquisition.torrent_root(client, "0" * 40)


class TestMappingSyntax:
    @pytest.mark.parametrize("text", [
        "/downloads -> /mnt/dl",
        "/downloads => /mnt/dl",
        "/downloads | /mnt/dl",
    ])
    def test_the_usual_separators_are_accepted(self, text):
        assert parse_mappings(text) == [("/downloads", "/mnt/dl")]

    def test_blank_lines_and_comments_are_skipped(self):
        assert parse_mappings("\n# a note\n/downloads -> /mnt/dl\n\n") == \
            [("/downloads", "/mnt/dl")]

    def test_nothing_configured_is_not_an_error(self):
        assert parse_mappings(None) == []

    def test_a_line_without_a_separator_says_what_is_wrong(self):
        with pytest.raises(ConfigError, match="remote -> local"):
            parse_mappings("/downloads /mnt/dl")


class TestPlanArithmetic:
    def test_an_empty_plan_reports_zero_rather_than_dividing_by_zero(self):
        assert AcquisitionPlan().describe().startswith("0")

    def test_missing_entries_name_which_set_they_came_from(self):
        from marquee.acquire import Selection
        made = AcquisitionPlan(roms=Selection(missing=["ghost"]),
                               chds=Selection(missing=["g/d"]))
        assert made.missing == ["roms:ghost", "chds:g/d"]


class TestAWindowsClient:
    """qBittorrent on Windows names its folders D:\\Torrents\\...; only "/" was ever
    looked for, so no mapping could match."""

    def test_a_mapping_typed_for_it_is_used(self):
        from marquee import acquisition
        mapped = acquisition.remap(r"D:\Torrents\MAME 0.289 ROMs (non-merged)",
                                   [(r"D:\Torrents", "/downloads")])
        assert mapped == "/downloads/MAME 0.289 ROMs (non-merged)"

    def test_case_and_slashes_do_not_matter_on_windows(self):
        from marquee import acquisition
        assert acquisition.remap(r"d:\torrents\MAME", [("D:/Torrents/", "/dl")]) == "/dl/MAME"

    def test_found_by_name_under_the_download_folder(self, tmp_path):
        from marquee import acquisition
        (tmp_path / "MAME 0.289 ROMs (non-merged)").mkdir()
        found = acquisition.locate(r"D:\Torrents\MAME 0.289 ROMs (non-merged)",
                                   download_dir=str(tmp_path))
        assert found == str(tmp_path / "MAME 0.289 ROMs (non-merged)")

    def test_unix_paths_are_unchanged(self):
        from marquee import acquisition
        assert acquisition.remap("/data/torrents/MAME", [("/data/torrents", "/dl")]) == "/dl/MAME"
        assert acquisition.remap("/Data/MAME", [("/data", "/dl")]) == "/Data/MAME"
