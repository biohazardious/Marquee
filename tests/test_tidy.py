"""Empty folders in the library, and how much room is left behind a share.

A game that leaves the library takes its files but, over a share, not its folders:
a real Batocera library had 85 of them -- `Unlisted/dlair`, `Music/Drum Machine`,
whole Quiz categories -- each one an empty entry on the console. And the Overview
could not say how full that library's disk was (99%) because a share cannot be
measured with disk_usage from here.
"""
import types

import pytest

from marquee import backends, catalog, pipeline, plan as planning, sync
from marquee.backends import smb as smb_module
from marquee.backends.local import LocalCopy
from marquee.backends.sftp import SftpCopy


class TestWhichFoldersAreEmpty:
    def test_a_folder_with_nothing_beneath_it(self):
        folders = ["Unlisted", "Unlisted/dlair", "Maze", "Maze/Misc"]
        assert backends.empty_folders(folders, {"Maze/Misc"}) == ["Unlisted/dlair", "Unlisted"]

    def test_deepest_first_so_a_parent_goes_after_its_children(self):
        folders = ["Music", "Music/Drum Machine", "Music/Player"]
        found = backends.empty_folders(folders, set())
        assert found.index("Music") > found.index("Music/Drum Machine")

    def test_any_file_keeps_a_folder_and_every_folder_above_it(self):
        folders = ["Notes", "Notes/deep", "Notes/deep/deeper"]
        assert backends.empty_folders(folders, {"Notes/deep/deeper"}) == []

    def test_media_folders_and_the_root_are_never_offered(self):
        folders = ["", "images", "videos", "manuals", "images/old"]
        assert backends.empty_folders(folders, set()) == []


class TestATransferTidiesThem:
    @pytest.fixture
    def built(self, categorised, config, romset):
        return planning.build(categorised, config.rom_dir, config.chd_dir,
                              catalog.folder_namer(config), config.allow_mature), romset

    def test_empty_folders_are_found_and_removed(self, built, config):
        plan, romset = built
        out = romset["out_dir"]
        (out / "Unlisted" / "dlair").mkdir(parents=True)
        (out / "Music" / "Drum Machine").mkdir(parents=True)
        (out / "images").mkdir()
        (out / "Notes").mkdir()
        (out / "Notes" / "readme.txt").write_text("mine")

        plan.sync = pipeline.compare_destination(plan, config)
        assert set(plan.sync.empty_folders) == {
            "Unlisted/dlair", "Unlisted", "Music/Drum Machine", "Music"}

        summary = pipeline.execute(plan, config)
        assert summary.folders_removed >= 3
        assert not (out / "Unlisted" / "dlair").exists()
        assert not (out / "Music").exists()
        assert (out / "images").is_dir(), "a media folder is where ES looks, even empty"
        assert (out / "Notes" / "readme.txt").read_text() == "mine"
        # Unlisted itself holds nocat.zip after the run, so it stays.
        assert (out / "Unlisted" / "nocat.zip").exists()

    def test_a_stopped_run_leaves_them(self, built, config):
        plan, romset = built
        (romset["out_dir"] / "Unlisted" / "dlair").mkdir(parents=True)
        plan.sync = pipeline.compare_destination(plan, config)
        summary = pipeline.execute(plan, config, should_continue=lambda: False)
        assert summary.cancelled
        assert (romset["out_dir"] / "Unlisted" / "dlair").is_dir()


class TestTidyingIsOnlyEverAnEmptyFolder:
    def test_a_folder_the_backend_refuses_is_not_counted(self):
        class Refusing:
            def remove_folder(self, relpath):
                return False
        assert pipeline.tidy_folders(Refusing(), ["a/b", "a"]) == 0

    def test_a_parent_emptied_by_its_children_follows_them(self):
        gone = []

        class Accepting:
            def remove_folder(self, relpath):
                gone.append(relpath)
                return True
        assert pipeline.tidy_folders(Accepting(), ["Quiz/German/quizard"]) == 3
        assert gone == ["Quiz/German/quizard", "Quiz/German", "Quiz"]

    def test_local_refuses_a_folder_with_a_file(self, tmp_path):
        (tmp_path / "keep").mkdir()
        (tmp_path / "keep" / "a.txt").write_text("x")
        (tmp_path / "empty").mkdir()
        backend = LocalCopy(str(tmp_path))
        assert backend.remove_folder("keep") is False
        assert backend.remove_folder("empty") is True
        assert (tmp_path / "keep" / "a.txt").exists()


class FolderSftp:
    """listdir_attr, rmdir and statvfs over a local folder, the way a server answers."""

    def __init__(self, root):
        self.root = str(root)

    def _local(self, remote):
        import os
        return os.path.join(self.root, remote.lstrip("/"))

    def listdir_attr(self, remote):
        import os
        import stat
        out = []
        for name in os.listdir(self._local(remote)):
            info = os.stat(os.path.join(self._local(remote), name))
            out.append(types.SimpleNamespace(filename=name, st_mode=info.st_mode,
                                             st_size=info.st_size))
            assert stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        return out

    def rmdir(self, remote):
        import os
        try:
            os.rmdir(self._local(remote))
        except OSError as error:
            raise IOError(str(error)) from error

    def statvfs(self, _remote):
        return types.SimpleNamespace(f_bavail=10, f_blocks=1000, f_frsize=4096, f_bsize=4096)


class TestSftp:
    @pytest.fixture
    def backend(self, tmp_path):
        handle = SftpCopy.__new__(SftpCopy)
        handle._known_dirs = set()
        handle.root = "/roms"
        handle.conn = FolderSftp(tmp_path)
        (tmp_path / "roms").mkdir()
        return handle, tmp_path / "roms"

    def test_the_index_sees_empty_folders(self, backend):
        handle, root = backend
        (root / "Unlisted" / "dlair").mkdir(parents=True)
        (root / "Maze").mkdir()
        (root / "Maze" / "galaga.zip").write_bytes(b"x")
        assert handle.index() == {"Maze/galaga.zip": 1}
        assert backends.empty_folders(handle.folders, handle.occupied) == \
            ["Unlisted/dlair", "Unlisted"]

    def test_rmdir_of_a_full_folder_is_refused(self, backend):
        handle, root = backend
        (root / "Maze").mkdir()
        (root / "Maze" / "galaga.zip").write_bytes(b"x")
        (root / "Empty").mkdir()
        assert handle.remove_folder("Maze") is False
        assert handle.remove_folder("Empty") is True

    def test_free_space_from_statvfs(self, backend):
        handle, _root = backend
        assert handle.free_space() == 40960
        assert handle.disk_space() == (40960, 4096000)


class Entry:
    def __init__(self, filename, file_size=0, is_dir=False):
        self.filename, self.file_size, self.isDirectory = filename, file_size, is_dir


class TreeConnection:
    """listPath and deleteDirectory over a dict tree: {folder: [entries]}."""

    def __init__(self, tree):
        self.tree = tree
        self.removed = []

    def listPath(self, _share, path):
        return [Entry(".", 0, True), Entry("..", 0, True)] + list(self.tree.get(path, []))

    def deleteDirectory(self, _share, path):
        if self.tree.get(path):
            raise smb_module.smb_structs.OperationFailure("not empty", [])
        self.removed.append(path)

    def close(self):
        pass


def smb_backend(connection):
    copier = smb_module.RemoteCopy.__new__(smb_module.RemoteCopy)
    copier._listing_cache, copier._known_dirs = {}, set()
    copier.reporter = None
    copier._parse_connection_string("smb://nas/Batocera3/roms/mame")
    copier.conn = connection
    return copier


class TestSmb:
    def test_the_index_sees_empty_folders(self):
        connection = TreeConnection({
            "roms/mame": [Entry("Unlisted", is_dir=True), Entry("Maze", is_dir=True)],
            "roms/mame/Unlisted": [Entry("dlair", is_dir=True)],
            "roms/mame/Unlisted/dlair": [],
            "roms/mame/Maze": [Entry("galaga.zip", 5)],
        })
        backend = smb_backend(connection)
        assert backend.index() == {"Maze/galaga.zip": 5}
        assert backends.empty_folders(backend.folders, backend.occupied) == \
            ["Unlisted/dlair", "Unlisted"]

    def test_an_unreadable_folder_is_not_called_empty(self):
        class Unreadable(TreeConnection):
            def listPath(self, share, path):
                if path.endswith("Secret"):
                    raise smb_module.smb_structs.OperationFailure("denied", [])
                return super().listPath(share, path)
        connection = Unreadable({"roms/mame": [Entry("Secret", is_dir=True)]})
        backend = smb_backend(connection)
        backend.index()
        assert backends.empty_folders(backend.folders, backend.occupied) == []

    def test_the_server_refusing_a_full_folder_is_not_a_failure(self):
        connection = TreeConnection({"roms/mame/Maze": [Entry("galaga.zip", 5)]})
        backend = smb_backend(connection)
        assert backend.remove_folder("Maze") is False
        assert backend.remove_folder("Unlisted/dlair") is True
        assert connection.removed == ["roms/mame/Unlisted/dlair"]

    def test_free_space_that_cannot_be_asked_is_unknown(self):
        # A stand-in connection has none of pysmb's internals: unknown, not a crash.
        backend = smb_backend(TreeConnection({}))
        assert backend.free_space() is None


class TestFreeSpaceBehindAShare:
    def plan_with(self, free, to_transfer):
        report = sync.SyncReport()
        report.free_bytes = free
        report.bytes = {sync.NEW: to_transfer}
        return types.SimpleNamespace(sync=report)

    def test_a_share_that_said_is_measured(self):
        plan = self.plan_with(free=54 * 2**30, to_transfer=2**30)
        needed, free = planning.check_free_space(plan, "smb://nas/Batocera3/roms/mame/")
        assert free == 54 * 2**30
        assert needed == plan.sync.to_transfer

    def test_how_full_the_disk_is_comes_with_it(self):
        """53.8 GB free said nothing until it was "of 3.6 TB, 99% full"."""
        plan = self.plan_with(free=54 * 2**30, to_transfer=0)
        plan.sync.disk_bytes = 3600 * 2**30
        assert planning.disk_size(plan, "smb://nas/Batocera3/roms/mame/") == 3600 * 2**30
        assert planning.disk_size(self.plan_with(free=1, to_transfer=0), "ftp://nas/r") is None

    def test_a_share_that_could_not_say_is_unknown(self):
        plan = self.plan_with(free=None, to_transfer=2**30)
        assert planning.check_free_space(plan, "ftp://nas/roms") is None
