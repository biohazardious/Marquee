"""Diffing a plan against the destination: new, changed, moved, orphaned."""

import pytest

from marquee import catalog, pipeline, sync
from marquee import plan as planning


@pytest.fixture
def plan(categorised, config, romset):
    return planning.build(categorised, config.rom_dir, config.chd_dir,
                          catalog.folder_namer(config), config.allow_mature)


def kinds(report):
    return {action.relpath: action.kind for action in report.actions}


class TestPlanFiles:
    def test_names_the_destination_of_every_file(self, plan):
        paths = {relpath for _src, relpath, _size in plan.files()}
        assert "Maze/Misc/goodgame.zip" in paths
        assert "Maze/Misc/twodisk/ok.chd" in paths
        assert "Fighter/25D/dotgame.zip" in paths

    def test_one_entry_per_file_not_per_machine(self, plan):
        assert len(list(plan.files())) == plan.file_count

    def test_sizes_come_through(self, plan):
        sizes = {relpath: size for _src, relpath, size in plan.files()}
        assert sizes["Maze/Misc/goodgame.zip"] == 300
        assert sizes["Maze/Misc/twodisk/ok.chd"] == 600


class TestCompare:
    def test_empty_destination_is_all_new(self, plan):
        report = sync.compare(plan, {})
        assert report.counts[sync.NEW] == plan.file_count
        assert report.counts[sync.KEEP] == 0
        assert report.to_transfer == plan.total_bytes

    def test_matching_file_is_kept(self, plan):
        existing = {relpath: size for _s, relpath, size in plan.files()}
        report = sync.compare(plan, existing)
        assert report.counts[sync.KEEP] == plan.file_count
        assert report.to_transfer == 0

    def test_wrong_size_is_an_update(self, plan):
        existing = {relpath: size for _s, relpath, size in plan.files()}
        existing["Maze/Misc/goodgame.zip"] = 999
        report = sync.compare(plan, existing)
        assert kinds(report)["Maze/Misc/goodgame.zip"] == sync.UPDATE
        assert report.counts[sync.UPDATE] == 1

    def test_same_file_in_another_folder_is_a_move(self, plan):
        """A recategorised machine should be renamed, not copied again."""
        existing = {relpath: size for _s, relpath, size in plan.files()}
        size = existing.pop("Maze/Misc/goodgame.zip")
        existing["Puzzle/Old/goodgame.zip"] = size
        report = sync.compare(plan, existing)
        moves = report.of(sync.MOVE)
        assert len(moves) == 1
        assert moves[0].from_relpath == "Puzzle/Old/goodgame.zip"
        assert moves[0].relpath == "Maze/Misc/goodgame.zip"
        assert report.counts[sync.ORPHAN] == 0

    def test_a_move_does_not_count_as_bytes_to_transfer(self, plan):
        existing = {relpath: size for _s, relpath, size in plan.files()}
        size = existing.pop("Maze/Misc/goodgame.zip")
        existing["Puzzle/Old/goodgame.zip"] = size
        assert sync.compare(plan, existing).to_transfer == 0

    def test_same_name_but_different_size_elsewhere_is_not_a_move(self, plan):
        existing = {"Puzzle/Old/goodgame.zip": 12345}
        report = sync.compare(plan, existing)
        assert kinds(report)["Maze/Misc/goodgame.zip"] == sync.NEW
        assert kinds(report)["Puzzle/Old/goodgame.zip"] == sync.ORPHAN

    def test_unwanted_file_is_an_orphan(self, plan):
        existing = {relpath: size for _s, relpath, size in plan.files()}
        existing["Puzzle/Old/leftover.zip"] = 10
        report = sync.compare(plan, existing)
        assert report.counts[sync.ORPHAN] == 1
        assert report.of(sync.ORPHAN)[0].relpath == "Puzzle/Old/leftover.zip"

    def test_one_source_file_cannot_satisfy_two_moves(self, plan):
        """Two machines of the same size must not both claim the same stray file."""
        existing = {"Old/goodgame.zip": 300}
        report = sync.compare(plan, existing)
        assert report.counts[sync.MOVE] <= 1

    def test_counts_and_bytes_are_tallied(self, plan):
        report = sync.compare(plan, {})
        assert set(report.counts) == {sync.NEW, sync.UPDATE, sync.MOVE, sync.KEEP,
                                      sync.ORPHAN}
        assert report.bytes[sync.NEW] == plan.total_bytes


class TestSurvey:
    def test_counts_and_sizes(self, romset):
        result = sync.survey({"roms": str(romset["rom_dir"]),
                              "chds": str(romset["chd_dir"])})
        assert result["roms"]["files"] == 11
        assert result["roms"]["bytes"] == 3300
        assert result["chds"]["files"] == 4

    def test_missing_path_is_zero_not_an_error(self):
        assert sync.survey({"x": "/nonexistent"})["x"] == {
            "files": 0, "bytes": 0, "path": "/nonexistent"}

    def test_none_path(self):
        assert sync.survey({"x": None})["x"]["files"] == 0


class TestBackendIndex:
    def test_empty_destination(self, romset, config):
        from marquee.backends import for_destination
        assert for_destination(config.copy_path).index() == {}

    def test_indexes_what_was_copied(self, plan, config, romset):
        from marquee.backends import for_destination
        pipeline.execute(plan, config)
        index = for_destination(config.copy_path).index()
        assert index["Maze/Misc/goodgame.zip"] == 300
        assert index["Maze/Misc/twodisk/ok.chd"] == 600
        assert len(index) == plan.file_count

    def test_partial_files_are_ignored(self, config, romset):
        from marquee.backends import for_destination
        (romset["out_dir"] / "half.chd.part").write_bytes(b"x")
        assert for_destination(config.copy_path).index() == {}

    def test_move_relocates_and_prunes(self, plan, config, romset):
        from marquee.backends import for_destination
        pipeline.execute(plan, config)
        backend = for_destination(config.copy_path)
        backend.move("Fighter/25D/dotgame.zip", "Maze/Misc/dotgame.zip")
        assert (romset["out_dir"] / "Maze" / "Misc" / "dotgame.zip").exists()
        assert not (romset["out_dir"] / "Fighter").exists()

    def test_delete_removes_and_prunes(self, plan, config, romset):
        from marquee.backends import for_destination
        pipeline.execute(plan, config)
        backend = for_destination(config.copy_path)
        backend.delete("Fighter/25D/dotgame.zip")
        assert not (romset["out_dir"] / "Fighter").exists()

    def test_delete_of_an_absent_file_is_quiet(self, config, romset):
        from marquee.backends import for_destination
        for_destination(config.copy_path).delete("nope/nothing.zip")


class TestExecuteSync:
    def test_second_run_copies_nothing(self, plan, config, romset):
        first = pipeline.execute(plan, config)
        assert first.copied == plan.file_count
        plan.sync = None
        second = pipeline.execute(plan, config)
        assert second.copied == 0 and second.skipped == plan.file_count

    def test_recategorised_machine_is_moved_not_recopied(self, plan, config, romset,
                                                         reporter):
        pipeline.execute(plan, config)
        # Pretend the catlist moved dotgame into another category.
        moved_plan = plan
        for item in moved_plan.items:
            if item.name == "dotgame":
                item.folder = "Puzzle/Misc"
        moved_plan.sync = None
        summary = pipeline.execute(moved_plan, config, reporter)
        assert summary.moved == 1
        assert summary.copied == 0
        assert (romset["out_dir"] / "Puzzle" / "Misc" / "dotgame.zip").exists()
        assert not (romset["out_dir"] / "Fighter").exists()

    def test_orphans_are_left_alone_by_default(self, plan, config, romset):
        pipeline.execute(plan, config)
        stray = romset["out_dir"] / "Old" / "leftover.zip"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"x")
        plan.sync = None
        summary = pipeline.execute(plan, config)
        assert summary.deleted == 0
        assert stray.exists()

    def test_orphans_can_be_deleted(self, plan, config, romset):
        pipeline.execute(plan, config)
        stray = romset["out_dir"] / "Old" / "leftover.zip"
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"x")
        plan.sync = None
        summary = pipeline.execute(plan, config, delete_orphans=True)
        assert summary.deleted == 1
        assert not stray.exists()

    def test_changed_file_is_replaced(self, plan, config, romset):
        pipeline.execute(plan, config)
        target = romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip"
        target.write_bytes(b"short")
        plan.sync = None
        summary = pipeline.execute(plan, config)
        assert summary.updated == 1
        assert target.stat().st_size == 300

    def test_unreadable_destination_is_survivable(self, plan, config, reporter):
        config.copy_path = "/proc/1/root/nowhere"
        report = pipeline.compare_destination(plan, config, reporter)
        assert report.counts[sync.NEW] == plan.file_count


class TestDestinationSafety:
    """A destination is somebody's console folder, not ours alone."""

    def test_index_ignores_files_we_never_wrote(self, plan, config, romset):
        from marquee.backends import for_destination
        pipeline.execute(plan, config)
        (romset["out_dir"] / "gamelist.xml").write_text("<gameList/>")
        (romset["out_dir"] / ".DS_Store").write_bytes(b"junk")
        (romset["out_dir"] / "images").mkdir()
        (romset["out_dir"] / "images" / "goodgame.png").write_bytes(b"png")
        index = for_destination(config.copy_path).index()
        assert all(name.endswith((".zip", ".chd")) for name in index)

    def test_delete_orphans_leaves_scraped_data_alone(self, plan, config, romset):
        pipeline.execute(plan, config)
        gamelist = romset["out_dir"] / "gamelist.xml"
        gamelist.write_text("<gameList/>")
        art = romset["out_dir"] / "images" / "goodgame.png"
        art.parent.mkdir()
        art.write_bytes(b"png")

        plan.sync = None
        summary = pipeline.execute(plan, config, delete_orphans=True)
        assert summary.deleted == 0
        assert gamelist.exists() and art.exists()

    def test_our_own_record_is_not_an_orphan(self, plan, config, romset):
        from marquee import manifest
        pipeline.execute(plan, config, mame_version="0.252")
        assert (romset["out_dir"] / manifest.NAME).exists()
        plan.sync = None
        summary = pipeline.execute(plan, config, delete_orphans=True)
        assert summary.deleted == 0
        assert (romset["out_dir"] / manifest.NAME).exists()

    def test_a_stale_rom_is_still_removed(self, plan, config, romset):
        pipeline.execute(plan, config)
        stale = romset["out_dir"] / "Old" / "leftover.zip"
        stale.parent.mkdir()
        stale.write_bytes(b"x")
        plan.sync = None
        assert pipeline.execute(plan, config, delete_orphans=True).deleted == 1
        assert not stale.exists()


class TestAnExistingLibraryIsCredited:
    """Pointing the library at a romset you already have and leaving the source folder
    empty used to report every file in it as an orphan -- and, with "remove what the
    library no longer wants" ticked, delete the lot."""

    def build_with_empty_source(self, categorised, config, tmp_path):
        empty = tmp_path / "no-source"
        empty.mkdir(exist_ok=True)
        return planning.build(categorised, str(empty), str(empty),
                              catalog.folder_namer(config), config.allow_mature)

    def test_what_the_library_holds_is_kept_not_orphaned(self, categorised, config,
                                                         tmp_path):
        built = self.build_with_empty_source(categorised, config, tmp_path)
        assert built.items == []
        wanted = next(iter(built.absent_items))
        existing = {relpath: 100 for relpath in wanted.wanted_paths()}
        report = sync.compare(built, existing)
        assert report.counts.get(sync.ORPHAN, 0) == 0
        assert report.counts[sync.KEEP] == len(existing)
        assert wanted.in_library is True

    def test_it_is_not_something_to_download(self, categorised, config, tmp_path):
        built = self.build_with_empty_source(categorised, config, tmp_path)
        wanted = next(iter(built.absent_items))
        assert wanted.name in built.needed
        sync.compare(built, {relpath: 100 for relpath in wanted.wanted_paths()})
        assert wanted.name not in built.needed
        assert wanted.name in built.missing_roms      # the source folder still has none

    def test_half_a_machine_is_not_counted_as_having_it(self, categorised, config,
                                                        tmp_path):
        """A zip without its disk is not a game you can play."""
        built = self.build_with_empty_source(categorised, config, tmp_path)
        needs_disk = next(item for item in built.absent_items
                          if len(list(item.wanted_paths())) > 1)
        first = next(iter(needs_disk.wanted_paths()))
        sync.compare(built, {first: 100})
        assert needs_disk.in_library is False
        assert needs_disk.name in built.needed

    def test_a_file_nothing_wants_is_still_an_orphan(self, categorised, config,
                                                     tmp_path):
        built = self.build_with_empty_source(categorised, config, tmp_path)
        report = sync.compare(built, {"Maze/Misc/nobodywantsthis.zip": 100})
        assert report.counts[sync.ORPHAN] == 1


class TestWhatTheCheckFound:
    """A size comparison cannot see a redump.

    MAME replaces a bad dump with a good one of the same length often enough that
    "same name, same size, leave it alone" quietly keeps the wrong file for ever. Once
    something has read what is inside, what it found has to outrank what the sizes say
    -- otherwise the library page reports a game as out of date and the transfer that
    is supposed to fix it does nothing.
    """

    def here(self, plan):
        return {relpath: size for _src, relpath, size in plan.files()}

    def test_a_stale_file_is_replaced_even_at_the_same_size(self, plan):
        item = next(one for one in plan.items if one.name == "goodgame")
        item.state = "stale"
        report = sync.compare(plan, self.here(plan))
        assert kinds(report)[f"{item.folder}/goodgame.zip"] == sync.UPDATE

    def test_a_damaged_file_is_replaced_too(self, plan):
        item = next(one for one in plan.items if one.name == "goodgame")
        item.state = "damaged"
        report = sync.compare(plan, self.here(plan))
        assert kinds(report)[f"{item.folder}/goodgame.zip"] == sync.UPDATE

    def test_its_disks_are_not_dragged_along(self, plan):
        """The state comes from reading the zip and says nothing about the disks."""
        item = next(one for one in plan.items if one.name == "twodisk")
        item.state = "stale"
        found = kinds(sync.compare(plan, self.here(plan)))
        assert found[f"{item.folder}/twodisk.zip"] == sync.UPDATE
        assert found[f"{item.folder}/twodisk/ok.chd"] == sync.KEEP

    def test_a_checked_library_that_is_current_is_still_left_alone(self, plan):
        for item in plan.items:
            item.state = "current"
        report = sync.compare(plan, self.here(plan))
        assert report.counts[sync.UPDATE] == 0
        assert report.counts[sync.KEEP] == plan.file_count

    def test_every_action_says_which_machine_wants_it(self, plan):
        report = sync.compare(plan, {})
        owners = {action.relpath: action.machine for action in report.actions}
        assert owners["Maze/Misc/goodgame.zip"] == "goodgame"
        assert owners["Maze/Misc/twodisk/ok.chd"] == "twodisk"

    def test_an_orphan_belongs_to_nobody(self, plan):
        report = sync.compare(plan, {"Maze/Misc/whoisthis.zip": 10})
        stray = next(action for action in report.actions if action.kind == sync.ORPHAN)
        assert stray.machine is None

    def test_the_machines_of_a_kind_are_listed_once_each(self, plan):
        report = sync.compare(plan, {})
        names = report.machines(sync.NEW)
        assert "twodisk" in names
        assert len(names) == len(set(names))


class TestALibraryBuiltToTheOldLayout:
    """A clone with a disk of its own gets its own folder; the old code always used
    the parent's name. The same file, one folder out -- and until this it was reported
    as something to delete and then download again, several gigabytes at a time.
    """

    def test_a_disk_in_the_parents_folder_is_relocated_not_deleted(self, categorised,
                                                                   config, tmp_path):
        config.rom_dir = str(tmp_path / "empty")       # nothing downloaded at all
        config.chd_dir = str(tmp_path / "empty")
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        item = next(one for one in built.absent_items if one.name == "clonestray")
        wanted = list(item.wanted_paths())
        disk = next(path for path in wanted if path.endswith(".chd"))
        old = f"{item.folder}/parentchd/{disk.rsplit('/', 1)[-1]}"
        assert old != disk, "the fixture no longer exercises the layout change"

        report = sync.compare(built, {old: 4096})
        moved = report.of(sync.MOVE)
        assert [(action.from_relpath, action.relpath) for action in moved] \
            == [(old, disk)]
        assert report.counts[sync.ORPHAN] == 0
        assert report.to_transfer == 0

    def test_it_is_not_something_to_download_again(self, categorised, config, tmp_path):
        config.rom_dir = str(tmp_path / "empty")
        config.chd_dir = str(tmp_path / "empty")
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        item = next(one for one in built.absent_items if one.name == "clonestray")
        here = {}
        for path in item.wanted_paths():
            name = path.rsplit("/", 1)[-1]
            here[f"{item.folder}/parentchd/{name}" if name.endswith(".chd")
                 else path] = 4096
        sync.compare(built, here)
        assert item.in_library is True
        assert item.name not in built.needed

    def test_one_stray_file_cannot_satisfy_two_machines(self, categorised, config,
                                                        tmp_path):
        config.rom_dir = str(tmp_path / "empty")
        config.chd_dir = str(tmp_path / "empty")
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        item = next(one for one in built.absent_items if one.name == "clonestray")
        disk = next(path for path in item.wanted_paths() if path.endswith(".chd"))
        name = disk.rsplit("/", 1)[-1]
        report = sync.compare(built, {f"Somewhere/Else/{name}": 4096})
        assert len(report.of(sync.MOVE)) == 1

    def test_a_file_nothing_wants_is_still_an_orphan(self, categorised, config,
                                                     tmp_path):
        config.rom_dir = str(tmp_path / "empty")
        config.chd_dir = str(tmp_path / "empty")
        built = planning.build(categorised, config.rom_dir, config.chd_dir,
                               catalog.folder_namer(config), config.allow_mature)
        report = sync.compare(built, {"Maze/Misc/whoisthis.zip": 10})
        assert [action.relpath for action in report.of(sync.ORPHAN)] \
            == ["Maze/Misc/whoisthis.zip"]


class TestAStaleFileOfTheSameSize:
    """A redump that weighs exactly what it replaces is invisible to a size check."""

    def test_an_update_replaces_it_even_at_the_same_size(self, plan, config, romset):
        from marquee import verify
        pipeline.execute(plan, config)
        target = romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip"
        target.write_bytes(b"OLD" * 100)            # 300 bytes, like the real one
        item = next(entry for entry in plan.items if entry.name == "goodgame")
        item.state = verify.STALE
        plan.sync = None
        summary = pipeline.execute(plan, config)
        assert summary.updated == 1
        assert target.read_bytes() == b"rom" * 100, "the backend's same-size skip won"


class TestDecliningTheComparison:
    def test_a_blind_report_is_everything_as_new(self, plan):
        report = sync.blind(plan)
        assert report.counts[sync.NEW] == plan.file_count
        assert report.counts[sync.ORPHAN] == 0
        assert report.counts[sync.MOVE] == 0

    def test_no_compare_is_honoured_by_the_run(self, plan, config, romset, monkeypatch):
        def unexpected(*_a, **_k):
            raise AssertionError("the destination was indexed despite --no-compare")

        monkeypatch.setattr(pipeline, "compare_destination", unexpected)
        plan.sync = sync.blind(plan)
        summary = pipeline.execute(plan, config)
        assert summary.copied == plan.file_count


class TestAMoveNeverStealsAKeptFile:
    def test_exact_matches_are_settled_before_moves(self):
        class Plan:
            items, absent_items = [], []

            def files(self):
                yield "/src/a/disk.chd", "f/a/disk.chd", 10
                yield "/src/b/disk.chd", "f/b/disk.chd", 10

        report = sync.compare(Plan(), {"f/b/disk.chd": 10})
        assert kinds(report) == {"f/b/disk.chd": sync.KEEP, "f/a/disk.chd": sync.NEW}


class TestOneBadFileDoesNotTakeTheRun:
    def test_a_failing_copy_is_counted_and_the_rest_still_arrive(self, plan, config, romset):
        from marquee import manifest
        from marquee.backends.local import LocalCopy

        class Flaky(LocalCopy):
            def copy(self, source, destination, on_bytes=None, replace=False):
                if source.endswith("goodgame.zip"):
                    raise OSError("No space left on device")
                super().copy(source, destination, on_bytes, replace)

        backend = Flaky(config.copy_path)
        summary = pipeline.execute(plan, config, backend=backend)
        assert summary.failed == 1
        assert summary.copied == plan.file_count - 1
        assert not (romset["out_dir"] / "Maze" / "Misc" / "goodgame.zip").exists()
        assert manifest.read(config.copy_path) is not None, "the record was still written"
