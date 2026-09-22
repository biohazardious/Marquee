"""Planning a run: what gets copied, what is missing, and whether it fits."""
import csv
import json
import os

import pytest

from marquee import catalog, pipeline
from marquee import plan as CopyPlan
from marquee.web.job import describe, machine_rows


def build_from(mame_list, config):
    return CopyPlan.build(mame_list, config.rom_dir, config.chd_dir,
                          catalog.folder_namer(config), config.allow_mature)


@pytest.fixture
def plan(categorised, config, romset):
    return build_from(categorised, config)


def names(plan):
    return {item.name for item in plan.items}


class TestBuild:
    def test_plans_the_machines_with_files(self, plan):
        assert names(plan) == {"goodgame", "impgame", "dotgame", "nocat", "twodisk",
                               "parentchd", "clonemerged", "cloneown", "clonestray"}

    def test_records_the_destination_folder(self, plan):
        by_name = {item.name: item for item in plan.items}
        assert by_name["goodgame"].folder == "Maze/Misc"
        assert by_name["dotgame"].folder == "Fighter/25D"
        assert by_name["nocat"].folder == "Unlisted"

    def test_mature_is_listed_separately_not_planned(self, plan):
        assert plan.mature_filtered == ["maturegame"]
        assert "maturegame" not in names(plan)

    def test_mature_is_planned_when_allowed(self, categorised, config, romset):
        config.allow_mature = True
        built = build_from(categorised, config)
        assert built.mature_filtered == []
        by_name = {item.name: item for item in built.items}
        assert by_name["maturegame"].folder == "ZZ-Adult/Casino/Misc"

    def test_missing_rom_is_recorded(self, plan):
        """betamax survives the filters but has no zip; boardgame1 was blacklisted earlier."""
        assert plan.missing_roms == ["betamax"]

    def test_missing_chd_is_recorded_not_substituted(self, categorised, config, romset):
        for entry in (romset["chd_dir"] / "cloneown").iterdir():
            entry.unlink()
        (romset["chd_dir"] / "cloneown").rmdir()
        built = build_from(categorised, config)
        assert "odisk" in built.missing_chds
        by_name = {item.name: item for item in built.items}
        assert by_name["cloneown"].chd_sources == []

    def test_clone_disk_kept_in_the_parent_folder_is_found(self, plan):
        """Real CHD sets store a clone's own disk beside the parent's; searching for a
        folder named after the clone reported every one of them missing."""
        by_name = {item.name: item for item in plan.items}
        stray = by_name["clonestray"]
        assert stray.chd_sources and stray.chd_sources[0].endswith("parentchd/sdisk.chd")
        assert "sdisk" not in plan.missing_chds
        assert stray.chd_name == "clonestray"

    def test_only_the_machines_own_disks_are_planned(self, plan):
        """parentchd's folder also holds clonestray's disk; it must not be dragged in."""
        by_name = {item.name: item for item in plan.items}
        assert [os.path.basename(p) for p in by_name["parentchd"].chd_sources] == ["pdisk.chd"]

    def test_merged_clone_points_at_the_parent_folder(self, plan):
        by_name = {item.name: item for item in plan.items}
        assert by_name["clonemerged"].chd_name == "parentchd"
        assert by_name["clonemerged"].chd_sources[0].endswith("parentchd/pdisk.chd")

    def test_sizes_are_measured(self, plan):
        by_name = {item.name: item for item in plan.items}
        assert by_name["goodgame"].rom_bytes == 300
        assert by_name["twodisk"].chd_bytes == 600
        assert by_name["twodisk"].total_bytes == 900

    def test_totals_count_each_destination_file_once(self, plan):
        """A merged clone names the parent's disk on purpose; summing per machine
        counted that file twice, inflating both the size and the file count."""
        per_machine = sum(item.total_bytes for item in plan.items)
        assert plan.total_bytes < per_machine
        assert plan.total_bytes == sum(size for _s, _r, size in plan.files())
        # nine roms and four distinct disks: clonemerged shares parentchd's.
        assert plan.file_count == 9 + 4

    def test_a_machine_with_nothing_on_disk_is_not_planned(self, plan):
        assert "betamax" not in names(plan)


class TestFreeSpace:
    def test_reports_need_and_availability(self, plan, romset):
        needed, free = CopyPlan.check_free_space(plan, str(romset["out_dir"]))
        assert needed == plan.total_bytes
        assert free > 0

    def test_already_copied_bytes_do_not_count_again(self, plan, config, romset):
        pipeline.execute(plan, config)
        needed, _free = CopyPlan.check_free_space(plan, str(romset["out_dir"]) + os.sep)
        assert needed == 0

    def test_smb_destination_is_unknowable(self, plan):
        assert CopyPlan.check_free_space(plan, "smb://nas/Share/roms") is None

    def test_walks_up_to_an_existing_parent(self, plan, romset):
        deep = str(romset["out_dir"] / "not" / "created" / "yet")
        assert CopyPlan.check_free_space(plan, deep) is not None


class TestHumanBytes:
    @pytest.mark.parametrize("count,expected", [
        (0, "0 B"), (512, "512 B"), (1024, "1.0 KB"),
        (1536, "1.5 KB"), (1048576, "1.0 MB"), (1073741824, "1.0 GB"),
    ])
    def test_formats(self, count, expected):
        assert CopyPlan.human_bytes(count) == expected


class TestReport:
    def test_json_contains_summary_and_items(self, plan, tmp_path):
        path = tmp_path / "report.json"
        CopyPlan.write_report(plan, str(path))
        payload = json.loads(path.read_text())
        assert payload["summary"]["machines"] == len(plan.items)
        assert payload["summary"]["bytes"] == plan.total_bytes
        assert payload["mature_filtered"] == ["maturegame"]
        assert {item["name"] for item in payload["items"]} == names(plan)

    def test_json_lists_missing_files(self, plan, tmp_path):
        path = tmp_path / "report.json"
        CopyPlan.write_report(plan, str(path))
        payload = json.loads(path.read_text())
        # The written report is read offline, so it carries the whole list; only the
        # page's poll payload is trimmed to counts.
        assert "betamax" in payload["missing_roms"]

    def test_csv_has_a_row_per_outcome(self, plan, tmp_path):
        path = tmp_path / "report.csv"
        CopyPlan.write_report(plan, str(path))
        with open(path, newline="") as handle:
            rows = list(csv.reader(handle))
        assert rows[0] == ["status", "machine", "description", "category", "folder", "bytes"]
        statuses = {row[0] for row in rows[1:]}
        assert statuses == {"copy", "missing-rom", "mature-filtered"}
        assert sum(1 for row in rows[1:] if row[0] == "copy") == len(plan.items)

    def test_extension_picks_the_format(self, plan, tmp_path):
        json_path = tmp_path / "r.json"
        csv_path = tmp_path / "r.CSV"
        CopyPlan.write_report(plan, str(json_path))
        CopyPlan.write_report(plan, str(csv_path))
        assert json_path.read_text().lstrip().startswith("{")
        assert csv_path.read_text().startswith("status,")


class TestGenreAndCategoryTotals:
    def test_selection_totals_match_what_would_be_copied(self, plan):
        """The Selection tab must not promise machines the run cannot deliver."""
        included = [c for c in plan.categories if not c["excluded"]]
        assert sum(c["machines"] for c in included) == len(plan.items)
        assert sum(c["bytes"] for c in included) == plan.total_bytes

    def test_a_machine_with_no_files_is_not_counted(self, plan):
        """betamax survives the filters but has no zip on disk, so it weighs nothing."""
        assert "betamax" in plan.missing_roms
        assert not any(c["name"].startswith("Maze") and "betamax" in c["name"]
                       for c in plan.categories)
        # Every counted machine is either planned or excluded by genre; none are phantoms.
        counted = sum(c["machines"] for c in plan.categories)
        excluded = sum(c["machines"] for c in plan.categories if c["excluded"])
        assert counted - excluded == len(plan.items)

    def test_categories_roll_up_into_genres(self, plan):
        by_genre = {}
        for entry in plan.categories:
            by_genre.setdefault(entry["genre"], 0)
            by_genre[entry["genre"]] += entry["bytes"]
        for genre in plan.genres:
            assert genre["bytes"] == by_genre[genre["name"]]

    def test_category_carries_its_genre(self, plan):
        maze = next(c for c in plan.categories if c["name"] == "Maze / Misc.")
        assert maze["genre"] == "Maze"

    def test_excluded_category_marks_only_itself(self, machines, catlist, config, romset):
        config.blacklist_categories = ["Fighter / 2.5D"]
        from marquee import catalog
        screenless = catalog.derive_screenless(machines)
        kept = catalog.categorize(
            catalog.filter_machines(machines, screenless, config), catlist, config)
        built = build_from(kept, config)
        by_name = {c["name"]: c for c in built.categories}
        assert by_name["Fighter / 2.5D"]["excluded"] is True
        assert by_name["Maze / Misc."]["excluded"] is False
        assert "dotgame" not in {item.name for item in built.items}


class TestPollCost:
    """The page polls the plan description every few seconds while a copy runs.

    Recomputing a 12,000-machine plan's totals, folder breakdown and 270 formatted
    genre entries every time cost 48 ms of pure repetition, so it is memoised. These
    tests pin the behaviour, not the timing.
    """

    def test_the_same_plan_is_described_once(self, plan, config):
        cache = {}
        first = describe(plan, config, cache)
        second = describe(plan, config, cache)
        assert first is second

    def test_without_a_cache_nothing_is_reused(self, plan, config):
        assert describe(plan, config) is not describe(plan, config)

    def test_comparing_against_the_destination_invalidates_it(self, plan, config):
        from marquee.sync import SyncReport
        cache = {}
        before = describe(plan, config, cache)
        plan.sync = SyncReport()
        after = describe(plan, config, cache)
        assert after is not before
        assert "sync" in after

    def test_a_different_destination_invalidates_it(self, plan, config):
        from dataclasses import replace
        cache = {}
        first = describe(plan, config, cache)
        second = describe(plan, replace(config, copy_path="/somewhere/else"), cache)
        assert second is not first

    def test_a_different_plan_is_not_confused_for_the_cached_one(self, plan, config):
        cache = {}
        describe(plan, config, cache)
        other = CopyPlan.CopyPlan()
        assert describe(other, config, cache)["machines"] == 0


class TestMachineRowsPaging:
    def test_only_the_page_is_built(self, plan):
        # Filtering items and building rows for the slice alone, rather than building
        # a dictionary for every machine and throwing most of them away.
        answer = machine_rows(plan, limit=2)
        assert len(answer["rows"]) == 2
        assert answer["total"] == len(plan.wanted)

    def test_sorting_happens_before_the_slice(self, plan):
        biggest = machine_rows(plan, limit=1, sort="size", descending=True)["rows"][0]
        every = machine_rows(plan, limit=500, sort="size", descending=True)["rows"]
        assert biggest["name"] == every[0]["name"]
        assert every[0]["bytes"] >= every[-1]["bytes"]

    def test_offset_walks_without_repeating(self, plan):
        first = machine_rows(plan, limit=2, offset=0, sort="name", descending=False)
        second = machine_rows(plan, limit=2, offset=2, sort="name", descending=False)
        names = [row["name"] for row in first["rows"] + second["rows"]]
        assert len(names) == len(set(names))

    def test_a_filter_narrows_the_total_not_just_the_page(self, plan):
        everything = machine_rows(plan, limit=500)["total"]
        narrowed = machine_rows(plan, limit=500, query="goodgame")["total"]
        assert 0 < narrowed < everything


class TestCondition:
    """"Working" is a filter, not a description.

    Everything planned has passed MAME's working filter, so the useful question is not
    whether a machine runs but how well: `impgame` runs with imperfect sound and no
    graphics emulation at all, and a library page that cannot say so is hiding the
    only thing that distinguishes it from `goodgame`.
    """

    def test_a_flawless_machine_says_so(self, plan):
        by_name = {item.name: item for item in plan.items}
        assert by_name["goodgame"].flawless is True
        assert by_name["goodgame"].condition() == []
        assert by_name["goodgame"].savestate == "supported"

    def test_the_caveats_are_carried_through(self, plan):
        by_name = {item.name: item for item in plan.items}
        imperfect = by_name["impgame"]
        assert imperfect.flawless is False
        assert imperfect.driver_status == "imperfect"
        assert sorted(imperfect.condition()) == ["imperfect sound", "unemulated graphics"]
        assert imperfect.savestate == "unsupported"

    def test_the_row_the_page_reads_carries_them(self, plan):
        rows = {row["name"]: row for row in machine_rows(plan, limit=500)["rows"]}
        # Sorted by area, so the order is the same every time the page is drawn.
        assert rows["impgame"]["condition"] == ["unemulated graphics", "imperfect sound"]
        assert rows["impgame"]["flawless"] is False
        assert rows["goodgame"]["flawless"] is True

    def test_the_library_can_be_narrowed_to_either(self, plan):
        flawless = machine_rows(plan, limit=500, condition="good")
        flawed = machine_rows(plan, limit=500, condition="flawed")
        every = machine_rows(plan, limit=500)
        assert flawless["total"] + flawed["total"] == every["total"]
        assert "impgame" in {row["name"] for row in flawed["rows"]}
        assert "goodgame" in {row["name"] for row in flawless["rows"]}


class TestSelectionWeights:
    """The selection tree has to mean something before anything is downloaded.

    `bytes` is what is on disk. On a fresh install that is nothing at all, and a tree
    answering "0 B" to every choice is one nobody can choose from -- so each genre and
    category also carries what it *would* weigh, from the release's own file table.
    """

    def test_every_category_is_listed_even_with_nothing_on_disk(self, categorised, config):
        config.rom_dir = "/nowhere"
        config.chd_dir = "/nowhere"
        built = build_from(categorised, config)
        assert built.items == []
        assert built.absent_items
        assert all(entry["machines"] == 0 for entry in built.categories)
        assert sum(entry["wanted"] for entry in built.categories) > 0

    def test_the_weight_comes_from_the_release_when_the_disk_is_empty(self, categorised,
                                                                     config):
        config.rom_dir = "/nowhere"
        config.chd_dir = "/nowhere"
        built = build_from(categorised, config)
        sizes = {item.name: 1000 for item in built.absent_items}
        payload = describe(built, config, None, sizes)
        assert all(entry["bytes"] == 0 for entry in payload["genres"])
        assert sum(entry["wanted_bytes"] for entry in payload["genres"]) == \
            1000 * len(built.absent_items)
        assert payload["genres"][0]["wanted_bytes_human"]

    def test_what_is_on_disk_is_weighed_by_what_is_on_disk(self, plan, config):
        payload = describe(plan, config, None, {"goodgame": 999999})
        by_name = {entry["name"]: entry for entry in payload["categories"]}
        maze = by_name["Maze / Misc."]
        # goodgame's zip is really 300 bytes; a release figure must not override it.
        assert maze["wanted_bytes"] == maze["bytes"]

    def test_the_memo_notices_when_the_prices_arrive(self, plan, config):
        cache = {}
        before = describe(plan, config, cache)
        after = describe(plan, config, cache, {"betamax": 4096})
        assert after is not before


class TestWholeSelectionWeight:
    """The headline figure has to describe the selection, not the disk.

    A fresh install has nothing downloaded, so "0 games · 0 B selected" under a tree of
    12,000 entries is the wrong answer to the question the page is being asked.
    """

    def test_the_payload_totals_what_is_wanted(self, plan, config):
        payload = describe(plan, config, None, {"betamax": 4096})
        assert payload["wanted"] == len(plan.wanted)
        assert payload["wanted_bytes"] == sum(entry["wanted_bytes"]
                                              for entry in payload["genres"])
        assert payload["wanted_bytes"] >= payload["bytes"]
        assert payload["wanted_bytes_human"]

    def test_with_nothing_on_disk_it_is_the_release_figure(self, categorised, config):
        config.rom_dir = "/nowhere"
        config.chd_dir = "/nowhere"
        built = build_from(categorised, config)
        sizes = {item.name: 2048 for item in built.absent_items}
        payload = describe(built, config, None, sizes)
        assert payload["bytes"] == 0
        assert payload["wanted_bytes"] == 2048 * len(built.absent_items)


class TestTheAdultSplit:
    """catlist marks adult categories in the category name, so they are already
    separate categories filed under their own folder. The tree shows that."""

    def test_a_mature_category_is_marked_and_labelled(self, categorised, config, romset):
        config.allow_mature = True
        built = build_from(categorised, config)
        by_name = {entry["name"]: entry for entry in built.categories}
        mature = [entry for entry in built.categories if entry["mature"]]
        assert mature, "no mature category in the fixture set"
        entry = mature[0]
        # The label is what the folder on disk is called: the genre and the marker
        # both come off.
        assert "Mature" not in entry["label"]
        assert " / " not in entry["label"]
        assert by_name["Maze / Misc."]["mature"] is False

    def test_the_two_halves_of_a_genre_are_different_categories(self, categorised,
                                                                config, romset):
        config.allow_mature = True
        built = build_from(categorised, config)
        for entry in built.categories:
            if not entry["mature"]:
                continue
            twin = entry["name"].replace(" * Mature *", "")
            assert entry["name"] != twin
            # Same genre, so the tree can file it under the same name on the adult side.
            assert entry["genre"]

    def test_nothing_mature_is_listed_when_it_is_not_allowed(self, categorised, config,
                                                             romset):
        config.allow_mature = False
        built = build_from(categorised, config)
        assert not any(entry["mature"] for entry in built.categories)


class TestParentsOnlyDoesNotExcludeCategories:
    def test_a_collapsed_clone_does_not_speak_for_its_category(self, machines, catlist,
                                                               config, romset):
        """Clones sort after their parents, so the last word in most categories used
        to be a set-aside clone's `excluded`, and the category read as excluded."""
        screenless = catalog.derive_screenless(machines)
        kept = catalog.categorize(
            catalog.filter_machines(machines, screenless, config), catlist, config)
        collapsed = catalog.collapse_clones(kept, None)
        assert any(info.get("clone_of_kept") for info in collapsed.values())
        built = build_from(collapsed, config)
        planned = {item.category for item in built.items}
        for entry in built.categories:
            if entry["name"] in planned:
                assert entry["excluded"] is False, entry["name"]


class TestWhatTheLibraryHolds:
    def test_library_items_include_games_already_at_the_destination(self, plan):
        from marquee import gamelist
        item = plan.items.pop()
        item.in_library = True
        plan.absent_items.append(item)
        assert item in plan.library_items
        assert item not in plan.items
        names = [game.findtext("path") for game in gamelist.document(plan.library_items)]
        assert any(item.name in (path or "") for path in names)


class TestAPlaceholderIsNotAFile:
    """qBittorrent allocates every selected file at full size before a byte arrives.
    5,442 of 14,279 zips in a live torrent folder were zeros, and every one of them
    used to count as downloaded."""

    def test_a_zero_filled_zip_is_not_downloaded(self, categorised, config, romset):
        (romset["rom_dir"] / "goodgame.zip").write_bytes(b"\x00" * 300)
        built = build_from(categorised, config)
        by_name = {item.name: item for item in built.wanted}
        assert by_name["goodgame"].rom_source is None
        assert by_name["goodgame"].partial is True
        assert "goodgame" in built.partial_roms
        assert "goodgame" in built.missing_roms
        assert "goodgame" not in {item.name for item in built.items}

    def test_a_zero_filled_chd_is_a_missing_disk(self, categorised, config, romset):
        (romset["chd_dir"] / "twodisk" / "ok.chd").write_bytes(b"\x00" * 600)
        built = build_from(categorised, config)
        item = next(entry for entry in built.wanted if entry.name == "twodisk")
        assert "ok" in item.missing_disks
        assert item.partial is True
        assert not any(path.endswith("ok.chd") for path in item.chd_sources)

    def test_a_finished_zip_still_counts(self, categorised, config, romset):
        built = build_from(categorised, config)
        assert "goodgame" in {item.name for item in built.items}
        assert built.partial_roms == []

    def test_the_page_is_told(self, categorised, config, romset):
        (romset["rom_dir"] / "goodgame.zip").write_bytes(b"\x00" * 300)
        built = build_from(categorised, config)
        assert describe(built, config)["partial_roms"] == 1
        row = next(row for row in machine_rows(built)["rows"] if row["name"] == "goodgame")
        assert row["partial"] is True


class TestAFileStillArriving:
    """A torrent client fetches the first and last pieces early. A 14 GB disk at 0.4%
    carried its header, passed for finished, and was copied into the library as
    fourteen gigabytes of zeros of exactly the right size -- which no size
    comparison would ever question again."""

    def test_a_chd_with_its_header_and_nothing_else_is_partial(self, tmp_path):
        path = tmp_path / "big.chd"
        path.write_bytes(b"MComprHD" + b"\x00" * 200000)
        assert CopyPlan.looks_complete(str(path)) is False

    def test_a_chd_with_a_stretch_of_zeros_is_partial(self, tmp_path):
        """Eight samples cannot catch one small missing piece -- the client's own
        word does that -- but a file that is mostly holes does not get past them."""
        path = tmp_path / "half.chd"
        body = bytearray(b"c" * 200000)
        body[60000:160000] = b"\x00" * 100000             # half of it not yet arrived
        path.write_bytes(b"MComprHD" + bytes(body))
        assert CopyPlan.looks_complete(str(path)) is False

    def test_a_chd_full_of_data_is_finished(self, tmp_path):
        path = tmp_path / "done.chd"
        path.write_bytes(b"MComprHD" + b"c" * 200000)
        assert CopyPlan.looks_complete(str(path)) is True

    def test_a_zip_whose_ends_arrived_first_is_partial(self, tmp_path):
        path = tmp_path / "big.zip"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 200000 + b"PK\x05\x06" + b"\x00" * 18)
        assert CopyPlan.looks_complete(str(path)) is False

    def test_a_small_zip_is_judged_by_its_record_alone(self, tmp_path):
        from tests.conftest import ROM_BYTES
        path = tmp_path / "small.zip"
        path.write_bytes(ROM_BYTES)
        assert CopyPlan.looks_complete(str(path)) is True

    def test_a_finished_file_the_client_vouches_for_is_finished(self, categorised, config,
                                                                 romset):
        """Genuine CHDs carry runs of zeros too; the client's word beats the sample."""
        disk = romset["chd_dir"] / "twodisk" / "ok.chd"
        disk.write_bytes(b"MComprHD" + b"\x00" * 200000)
        assert not CopyPlan.looks_complete(str(disk))
        built = CopyPlan.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature,
                               progress={str(disk): 1.0})
        twodisk = next(item for item in built.items if item.name == "twodisk")
        assert twodisk.partial is False and twodisk.chd_sources == [str(disk)]

    def test_what_the_client_says_is_arriving_outranks_the_file(self, categorised, config,
                                                                 romset):
        """The client knows which pieces it has; the file cannot be trusted at 95%."""
        disk = romset["chd_dir"] / "twodisk" / "ok.chd"
        assert CopyPlan.looks_complete(str(disk))
        built = CopyPlan.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature,
                               progress={str(disk): 0.96,
                                         str(romset["rom_dir"] / "goodgame.zip"): 0.5})
        twodisk = next(item for item in built.items if item.name == "twodisk")
        assert twodisk.partial is True and twodisk.chd_sources == []
        assert "ok" in twodisk.missing_disks
        good = next(item for item in built.wanted if item.name == "goodgame")
        assert good.partial is True and good.rom_source is None
        assert "goodgame" in built.partial_roms


class TestALabelIsNotWorthReadingFor:
    """The left-out catalogue is built with `sample=False`: nothing is copied from
    it, and sampling thousands of rejected zips was minutes on a cold pool."""

    def test_nothing_is_read_without_sampling(self, tmp_path, monkeypatch):
        (tmp_path / "pacman.zip").write_bytes(b"\0" * 100)     # a placeholder
        read = []
        monkeypatch.setattr(CopyPlan, "looks_complete", lambda path: read.append(path))
        listed = {"pacman": {"description": "Pac-Man", "category": "Maze / Misc.",
                             "genre": "Maze"}}
        built = CopyPlan.build(listed, str(tmp_path), str(tmp_path),
                               lambda category: ("Maze/Misc", False), True, sample=False)
        assert read == []
        assert built.items[0].rom_source

    def test_the_clients_word_still_counts(self, tmp_path):
        path = tmp_path / "pacman.zip"
        path.write_bytes(b"PK" + b"x" * 100)
        listed = {"pacman": {"description": "Pac-Man", "category": "Maze / Misc.",
                             "genre": "Maze"}}
        built = CopyPlan.build(listed, str(tmp_path), str(tmp_path),
                               lambda category: ("Maze/Misc", False), True,
                               progress={str(path): 0.3}, sample=False)
        item = built.wanted[0]
        assert item.rom_source is None and item.partial
