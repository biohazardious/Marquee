"""Local copying, progress reporting, and running the whole job."""
import json
import os

import pytest

from marquee import catalog, cli, pipeline
from marquee import config as configuration
from marquee import plan as planning
from marquee.backends import for_destination
from marquee.backends.local import LocalCopy
from marquee.errors import ConfigError
from marquee.reporting import CollectingReporter, TerminalReporter


class TestLocalCopy:
    def test_copies_a_file_into_the_destination_folder(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"data")
        LocalCopy(str(tmp_path / "out")).copy(str(source), "Maze/Misc")
        assert (tmp_path / "out" / "Maze" / "Misc" / "a.zip").read_bytes() == b"data"

    def test_copies_a_directory_tree(self, tmp_path):
        source = tmp_path / "chd"
        (source / "sub").mkdir(parents=True)
        (source / "a.chd").write_bytes(b"one")
        (source / "sub" / "b.chd").write_bytes(b"two")
        LocalCopy(str(tmp_path / "out")).copy(str(source), "Maze/chd")
        assert (tmp_path / "out" / "Maze" / "chd" / "a.chd").read_bytes() == b"one"
        assert (tmp_path / "out" / "Maze" / "chd" / "sub" / "b.chd").read_bytes() == b"two"

    def test_same_size_file_is_skipped(self, tmp_path, reporter):
        source = tmp_path / "a.zip"
        source.write_bytes(b"data")
        copier = LocalCopy(str(tmp_path / "out"), reporter=reporter)
        copier.copy(str(source), "X")
        destination = tmp_path / "out" / "X" / "a.zip"
        before = destination.stat().st_mtime_ns
        copier.copy(str(source), "X")
        assert "already there" in reporter.text
        assert destination.stat().st_mtime_ns == before

    def test_different_size_file_is_replaced(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"data")
        copier = LocalCopy(str(tmp_path / "out"))
        copier.copy(str(source), "X")
        source.write_bytes(b"much longer data")
        copier.copy(str(source), "X")
        assert (tmp_path / "out" / "X" / "a.zip").read_bytes() == b"much longer data"

    def test_missing_source_warns_rather_than_raising(self, tmp_path, reporter):
        LocalCopy(str(tmp_path / "out"), reporter=reporter).copy(str(tmp_path / "no.zip"), "X")
        assert reporter.warnings and "does not exist" in reporter.warnings[0]

    def test_empty_file_does_not_divide_by_zero(self, tmp_path):
        """A copy can finish inside the timer's resolution."""
        source = tmp_path / "empty.zip"
        source.write_bytes(b"")
        LocalCopy(str(tmp_path / "out")).copy(str(source), "X")
        assert (tmp_path / "out" / "X" / "empty.zip").exists()

    def test_instance_is_reusable(self, tmp_path):
        copier = LocalCopy(str(tmp_path / "out"))
        for name in ("a", "b", "c"):
            source = tmp_path / f"{name}.zip"
            source.write_bytes(name.encode())
            copier.copy(str(source), name.upper())
        assert {p.name for p in (tmp_path / "out").rglob("*.zip")} == {"a.zip", "b.zip", "c.zip"}

    def test_says_nothing_without_a_reporter(self, tmp_path, capsys):
        """Backends must not print; the CLI decides what reaches the terminal."""
        source = tmp_path / "a.zip"
        source.write_bytes(b"data")
        LocalCopy(str(tmp_path / "out")).copy(str(source), "X")
        assert capsys.readouterr().out == ""


class TestBackendSelection:
    def test_local_path(self, tmp_path):
        assert isinstance(for_destination(str(tmp_path)), LocalCopy)

    def test_ftp_now_reaches_the_ftp_backend(self):
        # It used to be refused outright. It now connects, so the only error left is
        # the connection itself failing.
        with pytest.raises(ConfigError, match="Could not connect"):
            for_destination("ftp://127.0.0.1:1/path")

    def test_no_destination(self):
        with pytest.raises(ConfigError):
            for_destination("")


class TestTerminalReporter:
    def test_zero_total_does_not_divide_by_zero(self, capsys):
        TerminalReporter().progress("copy", 0, 0)
        assert capsys.readouterr().out == ""

    def test_prints_at_the_step(self, capsys):
        TerminalReporter().progress("copy", 50, 100)
        assert "50%" in capsys.readouterr().out

    def test_does_not_reprint_within_the_step(self, capsys):
        term = TerminalReporter(step=2)
        term.progress("copy", 50, 100)
        capsys.readouterr()
        term.progress("copy", 51, 100)
        assert capsys.readouterr().out == ""

    def test_separate_stages_keep_separate_counters(self, capsys):
        term = TerminalReporter(step=50)
        term.progress("parse", 50, 100)
        term.progress("copy", 50, 100)
        assert capsys.readouterr().out.count("50%") == 2

    def test_quiet_keeps_warnings(self, capsys):
        term = TerminalReporter(quiet=True)
        term.info("chatter")
        term.warn("trouble")
        out = capsys.readouterr().out
        assert "chatter" not in out and "trouble" in out


class TestExecute:
    def copied(self, out_dir):
        """The ROMs and disks a run put there, not the files it generates about them."""
        from marquee import gamelist, manifest
        generated = {manifest.NAME, gamelist.NAME}
        return {str(p.relative_to(out_dir)) for p in out_dir.rglob("*")
                if p.is_file() and p.name not in generated
                and gamelist.IMAGE_DIR not in p.parts}

    @pytest.fixture
    def built(self, categorised, config, romset):
        return planning.build(categorised, config.rom_dir, config.chd_dir,
                              catalog.folder_namer(config), config.allow_mature), romset

    def test_copies_the_expected_tree(self, built, config):
        plan, romset = built
        pipeline.execute(plan, config)
        assert self.copied(romset["out_dir"]) == {
            "Maze/Misc/goodgame.zip",
            "Maze/Misc/impgame.zip",
            "Fighter/25D/dotgame.zip",
            "Unlisted/nocat.zip",
            "Maze/Misc/twodisk.zip",
            "Maze/Misc/twodisk/ok.chd",
            "Maze/Misc/parentchd.zip",
            "Maze/Misc/parentchd/pdisk.chd",
            "Maze/Misc/clonemerged.zip",
            "Maze/Misc/cloneown.zip",
            "Maze/Misc/cloneown/odisk.chd",
            "Maze/Misc/clonestray.zip",
            # Its disk lives in the parent's folder at the source, but it gets its own
            # folder here because the disk is not a merged copy of the parent's.
            "Maze/Misc/clonestray/sdisk.chd",
        }

    def test_mature_is_absent(self, built, config):
        plan, romset = built
        pipeline.execute(plan, config)
        assert not any("maturegame" in name for name in self.copied(romset["out_dir"]))

    def test_merged_clone_shares_the_parent_folder(self, built, config):
        plan, romset = built
        pipeline.execute(plan, config)
        assert not (romset["out_dir"] / "Maze" / "Misc" / "clonemerged").exists()

    def test_summary_counts_what_happened(self, built, config):
        plan, _romset = built
        summary = pipeline.execute(plan, config)
        assert summary.machines == len(plan.items)
        assert summary.copied_bytes == plan.total_bytes
        assert summary.missing_roms == ["betamax"]

    def test_rerun_copies_nothing_new(self, built, config, reporter):
        plan, romset = built
        pipeline.execute(plan, config)
        first = self.copied(romset["out_dir"])
        pipeline.execute(plan, config, reporter)
        assert self.copied(romset["out_dir"]) == first
        assert "Copied" not in reporter.text

    def test_a_gamelist_is_written_for_the_library(self, built, config):
        plan, romset = built
        pipeline.execute(plan, config)
        from marquee import gamelist
        body = (romset["out_dir"] / gamelist.NAME).read_text()
        # Paths carry the genre folder, because there is one list for the whole system.
        assert "./Maze/Misc/goodgame.zip" in body
        assert "Good Game" in body

    def test_the_gamelist_can_be_turned_off(self, built, config):
        from dataclasses import replace
        from marquee import gamelist
        plan, romset = built
        pipeline.execute(plan, replace(config, write_gamelist=False))
        assert not (romset["out_dir"] / gamelist.NAME).exists()

    def test_progress_is_counted_in_bytes(self, built, config, reporter):
        """Machine-based progress leaves the bar still through one huge CHD."""
        from dataclasses import replace
        plan, _romset = built
        # The gamelist reports its own progress afterwards; this is about the copy.
        pipeline.execute(plan, replace(config, write_gamelist=False), reporter)
        assert reporter.last_progress[:3] == ("copy", plan.total_bytes, plan.total_bytes)

    def test_cancelling_stops_between_machines(self, built, config, reporter):
        plan, romset = built
        pipeline.execute(plan, config, reporter, should_continue=lambda: False)
        assert self.copied(romset["out_dir"]) == set()

    def test_cancelled_summary_says_so(self, built, config):
        plan, _romset = built
        calls = {"n": 0}

        def keep_going():
            calls["n"] += 1
            return calls["n"] <= 2

        summary = pipeline.execute(plan, config, should_continue=keep_going)
        assert summary.cancelled is True
        # Work is counted in files now, not machines.
        assert summary.copied == 2

    def test_summary_counts_the_actions(self, built, config):
        plan, _romset = built
        first = pipeline.execute(plan, config)
        assert first.copied == plan.file_count
        assert first.skipped == 0

        plan.sync = None
        second = pipeline.execute(plan, config)
        assert second.copied == 0
        assert second.skipped == plan.file_count


def settings_for(config, tmp_path):
    path = str(tmp_path / "run-settings.ini")
    config.settings_path = path
    configuration.write_settings_file(path, config)
    return path


def run_args(settings_path, xml_path, catlist_path, **overrides):
    argv = ["--config", settings_path, "--xml", xml_path, "--catlist", catlist_path]
    parsed = cli.build_arg_parser().parse_args(argv)
    for key, value in overrides.items():
        setattr(parsed, key, value)
    return parsed


class TestRunModes:
    @pytest.fixture
    def ready(self, config, romset, tmp_path, xml_path, catlist_path):
        return (settings_for(config, tmp_path), xml_path, catlist_path, romset)

    def copied(self, out_dir):
        from marquee import manifest
        return {str(p.relative_to(out_dir)) for p in out_dir.rglob("*")
                if p.is_file() and p.name != manifest.NAME}

    def test_dry_run_copies_nothing(self, ready, capsys):
        settings, xml, catlist, romset = ready
        assert cli.run(run_args(settings, xml, catlist, dry_run=True)) == 0
        assert self.copied(romset["out_dir"]) == set()
        assert "Dry run" in capsys.readouterr().out

    def test_dry_run_never_prompts(self, ready, monkeypatch):
        settings, xml, catlist, _romset = ready

        def explode(_prompt):
            raise AssertionError("a dry run asked for confirmation")

        monkeypatch.setattr("builtins.input", explode)
        cli.run(run_args(settings, xml, catlist, dry_run=True))

    def test_dry_run_reports_the_plan(self, ready, capsys):
        settings, xml, catlist, _romset = ready
        cli.run(run_args(settings, xml, catlist, dry_run=True))
        output = capsys.readouterr().out
        assert "files" in output and "Missing ROMs" in output
        assert "already there" in output and "new" in output

    def test_yes_skips_the_prompt_and_copies(self, ready, monkeypatch):
        settings, xml, catlist, romset = ready

        def explode(_prompt):
            raise AssertionError("--yes still asked for confirmation")

        monkeypatch.setattr("builtins.input", explode)
        assert cli.run(run_args(settings, xml, catlist, yes=True)) == 0
        assert "Maze/Misc/goodgame.zip" in self.copied(romset["out_dir"])

    def test_declining_the_prompt_copies_nothing(self, ready, monkeypatch):
        settings, xml, catlist, romset = ready
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        assert cli.run(run_args(settings, xml, catlist)) == 0
        assert self.copied(romset["out_dir"]) == set()

    def test_report_is_written(self, ready, tmp_path):
        settings, xml, catlist, _romset = ready
        report = tmp_path / "plan.json"
        cli.run(run_args(settings, xml, catlist, dry_run=True, report=str(report)))
        assert json.loads(report.read_text())["summary"]["machines"] > 0

    def test_summary_is_printed(self, ready, capsys):
        settings, xml, catlist, _romset = ready
        cli.run(run_args(settings, xml, catlist, yes=True))
        assert "Done in" in capsys.readouterr().out

    def test_not_enough_space_stops_the_run(self, ready, monkeypatch, capsys):
        settings, xml, catlist, romset = ready
        monkeypatch.setattr(planning, "check_free_space", lambda _p, _d: (10 ** 12, 1))
        assert cli.run(run_args(settings, xml, catlist)) == 1
        assert self.copied(romset["out_dir"]) == set()
        assert "Not enough room" in capsys.readouterr().out

    def test_yes_overrides_the_space_check(self, ready, monkeypatch):
        settings, xml, catlist, romset = ready
        monkeypatch.setattr(planning, "check_free_space", lambda _p, _d: (10 ** 12, 1))
        assert cli.run(run_args(settings, xml, catlist, yes=True)) == 0
        assert "Maze/Misc/goodgame.zip" in self.copied(romset["out_dir"])

    def test_quiet_still_prints_the_plan_and_summary(self, ready, capsys):
        settings, xml, catlist, _romset = ready
        cli.run(run_args(settings, xml, catlist, yes=True, quiet=True))
        output = capsys.readouterr().out
        assert "in the set" in output and "Done in" in output
        assert "Parsing the MAME XML" not in output


class TestMainExitCodes:
    def test_error_becomes_exit_one(self, capsys):
        assert cli.main(["--config", "/nonexistent/settings.ini"]) == 1
        assert "not found" in capsys.readouterr().err

    def test_success_is_zero(self, config, romset, tmp_path, xml_path, catlist_path):
        settings = settings_for(config, tmp_path)
        assert cli.main(["--config", settings, "--xml", xml_path,
                         "--catlist", catlist_path, "-n"]) == 0

    def test_reporter_collects_instead_of_printing(self, config, romset, xml_path,
                                                   catlist_path):
        """The same pipeline run, driven with no terminal at all."""
        config.mame_xml, config.catlist_ini = xml_path, catlist_path
        collected = CollectingReporter()
        built, resolution = pipeline.build_plan(config, reporter=collected)
        assert built.items and resolution.xml_version == "0.252"
        assert any("machines kept" in message for message in collected.messages)

    def test_a_backup_two_levels_down_is_not_read_from(self, config, romset, tmp_path,
                                                       xml_path, catlist_path):
        """A download folder with a hand-made "mame" backup of the full set in it: the
        plan reads from the download folder only, says the disks are not there, and
        leaves the backup to its owner."""
        nest = tmp_path / "downloads" / "mame"
        nest.mkdir(parents=True)
        romset["chd_dir"].rename(nest / "MAME 0.252 CHDs (merged)")
        config.mame_xml, config.catlist_ini = xml_path, catlist_path
        config.chd_dir = str(tmp_path / "downloads")
        collected = CollectingReporter()
        built, resolution = pipeline.build_plan(config, reporter=collected)
        assert resolution.chd_dir == str(tmp_path / "downloads")
        assert resolution.chd_set_found is False
        assert built.missing_chds
        assert any("No CHD set" in message for message in collected.warnings)

    def test_no_chd_set_under_the_folder_is_said_out_loud(self, config, romset, tmp_path,
                                                         xml_path, catlist_path):
        """Disks wanted and none to be had used to read as "not downloaded yet"."""
        empty = tmp_path / "nothing-here"
        empty.mkdir()
        config.mame_xml, config.catlist_ini = xml_path, catlist_path
        config.chd_dir = str(empty)
        collected = CollectingReporter()
        built, resolution = pipeline.build_plan(config, reporter=collected)
        assert resolution.chd_set_found is False
        assert built.missing_chds
        assert any("No CHD set under" in message and str(empty) in message
                   for message in collected.warnings)


class TestChunkedCopy:
    """Files past the threshold are copied in chunks so progress can be reported."""

    def big(self, path, size):
        path.write_bytes(b"x" * size)
        return path

    def test_large_file_reports_incremental_progress(self, tmp_path, monkeypatch):
        from marquee.backends import local as local_backend
        monkeypatch.setattr(local_backend, "CHUNKED_ABOVE", 1024)
        monkeypatch.setattr(local_backend, "CHUNK_SIZE", 512)
        source = self.big(tmp_path / "big.chd", 4096)

        seen = []
        LocalCopy(str(tmp_path / "out")).copy(str(source), "X", seen.append)
        assert sum(seen) == 4096
        assert len(seen) > 1
        assert (tmp_path / "out" / "X" / "big.chd").read_bytes() == b"x" * 4096

    def test_small_file_reports_once(self, tmp_path):
        source = self.big(tmp_path / "small.zip", 100)
        seen = []
        LocalCopy(str(tmp_path / "out")).copy(str(source), "X", seen.append)
        assert seen == [100]

    def test_skipped_file_still_counts_towards_progress(self, tmp_path):
        """Otherwise a re-run's bar would never reach the end."""
        source = self.big(tmp_path / "a.zip", 100)
        copier = LocalCopy(str(tmp_path / "out"))
        copier.copy(str(source), "X")
        seen = []
        copier.copy(str(source), "X", seen.append)
        assert seen == [100]

    def test_no_partial_file_survives_a_failure(self, tmp_path, monkeypatch):
        from marquee.backends import local as local_backend
        monkeypatch.setattr(local_backend, "CHUNKED_ABOVE", 1024)
        source = self.big(tmp_path / "big.chd", 4096)

        def explode(_count):
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            LocalCopy(str(tmp_path / "out")).copy(str(source), "X", explode)
        assert list((tmp_path / "out" / "X").glob("*")) == []

    def test_metadata_is_preserved(self, tmp_path, monkeypatch):
        from marquee.backends import local as local_backend
        monkeypatch.setattr(local_backend, "CHUNKED_ABOVE", 1024)
        source = self.big(tmp_path / "big.chd", 4096)
        os.utime(source, (100000, 100000))
        LocalCopy(str(tmp_path / "out")).copy(str(source), "X", lambda _n: None)
        assert int((tmp_path / "out" / "X" / "big.chd").stat().st_mtime) == 100000


class TestHardlinking:
    """Linking the library out of the torrent folder, and the hazard that comes with it.

    A hardlinked file is the same file. That is the whole saving -- a 343 GB library
    costs nothing on top of the torrent folder -- and also the whole danger.
    """

    def source(self, tmp_path, content=b"rom" * 100):
        folder = tmp_path / "torrents"
        folder.mkdir(exist_ok=True)
        path = folder / "galaga.zip"
        path.write_bytes(content)
        return path

    def test_a_linked_file_costs_no_extra_space(self, tmp_path):
        source = self.source(tmp_path)
        backend = LocalCopy(str(tmp_path / "library"), hardlink=True)
        backend.copy(str(source), "Maze")
        target = tmp_path / "library" / "Maze" / "galaga.zip"
        assert target.stat().st_ino == source.stat().st_ino
        assert target.stat().st_nlink == 2
        assert backend.linked == 1

    def test_two_mounts_of_one_filesystem_are_named_not_silently_copied(
            self, tmp_path, monkeypatch):
        """Same device number, and link(2) still says EXDEV: the shipped compose file
        mounts /downloads and /library separately. Every file was copied in full and
        nothing said so."""
        import errno
        source = self.source(tmp_path)
        reporter = CollectingReporter()
        backend = LocalCopy(str(tmp_path / "library"), hardlink=True, reporter=reporter)

        def refused(_src, _dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        monkeypatch.setattr(os, "link", refused)
        backend.copy(str(source), "Maze")
        backend.copy(str(source), "Shooter")
        target = tmp_path / "library" / "Maze" / "galaga.zip"
        assert target.read_bytes() == source.read_bytes()
        assert reporter.text.count("one volume") == 1, "said once, not per file"
        assert not list((tmp_path / "library").rglob("*.link"))

    def test_a_failed_replacement_keeps_the_old_copy(self, tmp_path, monkeypatch):
        """The old file used to be removed before the new one existed: a full disk
        then lost both."""
        import shutil
        source = self.source(tmp_path)
        library = tmp_path / "library" / "Maze"
        library.mkdir(parents=True)
        (library / "galaga.zip").write_bytes(b"old")
        backend = LocalCopy(str(tmp_path / "library"))

        def full(*_args, **_kwargs):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(shutil, "copy2", full)
        with pytest.raises(OSError):
            backend.copy(str(source), "Maze")
        assert (library / "galaga.zip").read_bytes() == b"old"

    def test_relinking_the_same_file_leaves_nothing_behind(self, tmp_path):
        source = self.source(tmp_path)
        backend = LocalCopy(str(tmp_path / "library"), hardlink=True)
        backend.copy(str(source), "Maze")
        backend.copy(str(source), "Maze", replace=True)
        target = tmp_path / "library" / "Maze" / "galaga.zip"
        assert target.stat().st_ino == source.stat().st_ino
        assert not list((tmp_path / "library").rglob("*.link"))

    def test_a_kernel_copy_that_gives_up_half_way_is_finished_by_hand(
            self, tmp_path, monkeypatch):
        """copy_file_range can refuse at any point -- an older kernel across
        filesystems, a FUSE mount. The rest is read and written from where it got to."""
        import errno
        from marquee.backends import local
        monkeypatch.setattr(local, "CHUNKED_ABOVE", 0)
        monkeypatch.setattr(local, "CHUNK_SIZE", 1000)
        body = bytes(range(256)) * 20
        source = self.source(tmp_path, body)
        real = getattr(os, "copy_file_range", None)
        calls = []

        def once_then_refuse(src, dst, count, *args):
            calls.append(count)
            if len(calls) > 1 or real is None:
                raise OSError(errno.EXDEV, "cross-device")
            return real(src, dst, count)
        monkeypatch.setattr(os, "copy_file_range", once_then_refuse, raising=False)
        counted = []
        LocalCopy(str(tmp_path / "library")).copy(str(source), "Maze",
                                                  on_bytes=counted.append)
        target = tmp_path / "library" / "Maze" / "galaga.zip"
        assert target.read_bytes() == body
        assert sum(counted) == len(body)

    def test_removing_the_library_copy_leaves_the_torrent_file(self, tmp_path):
        source = self.source(tmp_path)
        backend = LocalCopy(str(tmp_path / "library"), hardlink=True)
        backend.copy(str(source), "Maze")
        backend.delete("Maze/galaga.zip")
        # Seeding has to survive a filter change.
        assert source.read_bytes() == b"rom" * 100

    def test_replacing_a_file_does_not_write_through_its_links(self, tmp_path):
        source = self.source(tmp_path)
        backend = LocalCopy(str(tmp_path / "library"), hardlink=True)
        backend.copy(str(source), "Maze")
        target = tmp_path / "library" / "Maze" / "galaga.zip"

        # A newer release of the same machine, at a different size.
        replacement = tmp_path / "torrents2"
        replacement.mkdir()
        newer = replacement / "galaga.zip"
        newer.write_bytes(b"newer")
        backend.copy(str(newer), "Maze")

        assert target.read_bytes() == b"newer"
        # The file the first torrent is still seeding must be untouched.
        assert source.read_bytes() == b"rom" * 100
        assert target.stat().st_ino != source.stat().st_ino

    def test_copying_is_the_default(self, tmp_path):
        source = self.source(tmp_path)
        backend = LocalCopy(str(tmp_path / "library"))
        backend.copy(str(source), "Maze")
        target = tmp_path / "library" / "Maze" / "galaga.zip"
        assert target.stat().st_ino != source.stat().st_ino
        assert backend.linked == 0

    def test_a_destination_on_another_filesystem_falls_back_to_copying(self, tmp_path,
                                                                       monkeypatch):
        source = self.source(tmp_path)
        backend = LocalCopy(str(tmp_path / "library"), hardlink=True)
        real_stat = os.stat
        monkeypatch.setattr(
            os, "stat",
            lambda p, *a, **k: _OtherDevice(real_stat(p, *a, **k))
            if str(p) == str(source) else real_stat(p, *a, **k))
        backend.copy(str(source), "Maze")
        assert backend.linked == 0
        assert (tmp_path / "library" / "Maze" / "galaga.zip").read_bytes() == b"rom" * 100

    def test_the_setting_reaches_the_backend(self, tmp_path):
        assert for_destination(str(tmp_path), hardlink=True).hardlink is True
        assert for_destination(str(tmp_path)).hardlink is False


class _OtherDevice:
    """A stat result claiming to live on a different filesystem."""

    def __init__(self, real):
        self._real = real
        self.st_dev = real.st_dev + 1

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestALocalRenameNeverOverwrites:
    def test_the_file_that_is_there_wins(self, tmp_path):
        from marquee.backends.local import LocalCopy
        (tmp_path / "Old").mkdir(); (tmp_path / "New").mkdir()
        (tmp_path / "Old" / "a.chd").write_bytes(b"old")
        (tmp_path / "New" / "a.chd").write_bytes(b"new")
        with pytest.raises(FileExistsError):
            LocalCopy(str(tmp_path)).move("Old/a.chd", "New/a.chd")
        assert (tmp_path / "New" / "a.chd").read_bytes() == b"new"
        assert (tmp_path / "Old" / "a.chd").exists()


class TestAutomaticHardlink:
    """On exactly when a link from the torrent folder into the library works --
    tried, not inferred from device numbers, which two bind mounts share."""

    def setup_method(self):
        from marquee.backends import local
        local._PROBES.clear()

    def folders(self, tmp_path):
        torrents = tmp_path / "torrents" / "MAME 0.289 ROMs (non-merged)"
        torrents.mkdir(parents=True)
        (torrents / "galaga.zip").write_bytes(b"rom")
        library = tmp_path / "library"
        library.mkdir()
        return torrents.parent, library

    def test_one_filesystem_links(self, tmp_path):
        from marquee.backends import effective_hardlink
        torrents, library = self.folders(tmp_path)
        config = configuration.Config(rom_dir=str(torrents), copy_path=str(library))
        assert effective_hardlink(config) is True
        assert list(library.iterdir()) == [], "the probe leaves nothing behind"
        assert sorted(p.name for p in torrents.rglob("*")) == \
            ["MAME 0.289 ROMs (non-merged)", "galaga.zip"], "nothing written to the source"

    def test_two_mounts_copy(self, tmp_path, monkeypatch):
        import errno
        from marquee.backends import effective_hardlink
        torrents, library = self.folders(tmp_path)

        def refused(_src, _dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        monkeypatch.setattr(os, "link", refused)
        config = configuration.Config(rom_dir=str(torrents), copy_path=str(library))
        assert effective_hardlink(config) is False

    def test_a_library_that_does_not_exist_yet_is_tried_at_its_parent(self, tmp_path):
        from marquee.backends import effective_hardlink
        torrents, library = self.folders(tmp_path)
        config = configuration.Config(rom_dir=str(torrents),
                                      copy_path=str(library / "not" / "yet"))
        assert effective_hardlink(config) is True

    def test_pinned_wins_and_a_share_never_links(self, tmp_path):
        from marquee.backends import effective_hardlink
        torrents, library = self.folders(tmp_path)
        assert effective_hardlink(configuration.Config(
            rom_dir=str(torrents), copy_path=str(library), hardlink=False)) is False
        assert effective_hardlink(configuration.Config(
            rom_dir=str(torrents), copy_path="smb://nas/Share/roms")) is False

    def test_an_empty_torrent_folder_copies_until_there_is_something_to_try(self, tmp_path):
        from marquee.backends import effective_hardlink
        empty = tmp_path / "empty"
        empty.mkdir()
        assert effective_hardlink(configuration.Config(
            rom_dir=str(empty), copy_path=str(tmp_path))) is False
