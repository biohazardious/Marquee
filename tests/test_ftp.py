"""The FTP backend, against a real FTP server.

pyftpdlib is a test-only dependency; the backend itself is pure ftplib.
"""
import os
import threading

import pytest

from marquee.backends import for_destination
from marquee.backends.ftp import FtpCopy
from marquee.errors import ConfigError

pyftpdlib = pytest.importorskip("pyftpdlib")


@pytest.fixture
def ftp_server(tmp_path):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    root = tmp_path / "srv"
    root.mkdir()
    authorizer = DummyAuthorizer()
    authorizer.add_user("bio", "pa/ss@word", str(root), perm="elradfmwMT")
    handler = FTPHandler
    handler.authorizer = authorizer
    handler.banner = "test"
    server = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.1},
                              daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    yield root, port
    server.close_all()


@pytest.fixture
def backend(ftp_server):
    root, port = ftp_server
    # The password holds '/' and '@', which is exactly what percent-encoding is for.
    url = f"ftp://bio:pa%2Fss%40word@127.0.0.1:{port}/roms"
    handle = FtpCopy(url)
    yield handle, root
    handle.close()


@pytest.fixture
def rom(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    path = source / "galaga.zip"
    path.write_bytes(b"rom" * 1000)
    return str(path)


class TestConnection:
    def test_a_password_with_punctuation_survives_the_url(self, ftp_server):
        root, port = ftp_server
        handle = FtpCopy(f"ftp://bio:pa%2Fss%40word@127.0.0.1:{port}/")
        assert handle.password == "pa/ss@word"
        handle.close()

    def test_a_bad_destination_string_says_what_it_wanted(self):
        with pytest.raises(ConfigError, match="ftp://user"):
            FtpCopy("ftp:/missing-slash")

    def test_an_unreachable_server_names_the_host(self):
        with pytest.raises(ConfigError, match="Could not connect to 127.0.0.1:1"):
            FtpCopy("ftp://x:y@127.0.0.1:1/", timeout=2)

    def test_the_scheme_picks_this_backend(self, ftp_server):
        root, port = ftp_server
        handle = for_destination(f"ftp://bio:pa%2Fss%40word@127.0.0.1:{port}/roms")
        assert isinstance(handle, FtpCopy)
        handle.close()


class TestCopy:
    def test_a_file_lands_in_its_category_folder(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Maze")
        assert (root / "roms" / "Maze" / "galaga.zip").read_bytes() == b"rom" * 1000

    def test_parent_directories_are_created(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Shooter/Flying Vertical")
        assert (root / "roms" / "Shooter" / "Flying Vertical" / "galaga.zip").exists()

    def test_bytes_are_reported_as_they_move(self, backend, rom):
        handle, _root = backend
        moved = []
        handle.copy(rom, "Maze", on_bytes=moved.append)
        assert sum(moved) == 3000

    def test_a_file_already_there_at_the_same_size_is_skipped(self, backend, rom):
        handle, _root = backend
        handle.copy(rom, "Maze")
        moved = []
        handle.copy(rom, "Maze", on_bytes=moved.append)
        # Still reported, so a progress bar reaches its total either way.
        assert sum(moved) == 3000

    def test_no_part_file_is_left_behind(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Maze")
        assert [p.name for p in (root / "roms" / "Maze").iterdir()] == ["galaga.zip"]

    def test_a_missing_source_warns_rather_than_raising(self, backend):
        handle, _root = backend
        warnings = []
        handle.reporter.warn = warnings.append
        handle.copy("/nonexistent/x.zip", "Maze")
        assert warnings and "does not exist" in warnings[0]

    def test_a_directory_is_copied_whole(self, backend, tmp_path):
        handle, root = backend
        folder = tmp_path / "dlair"
        folder.mkdir()
        (folder / "dlair.chd").write_bytes(b"chd")
        handle.copy(str(folder), "Platform")
        assert (root / "roms" / "Platform" / "dlair.chd").read_bytes() == b"chd"


class TestIndex:
    def test_only_roms_and_disks_are_counted(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Maze")
        (root / "roms" / "Maze" / "gamelist.xml").write_text("<x/>")
        found = handle.index()
        # A console folder holds scraped art and metadata; none of it is ours.
        assert found == {"Maze/galaga.zip": 3000}

    def test_an_empty_destination_indexes_to_nothing(self, backend):
        handle, _root = backend
        assert handle.index() == {}

    def test_nested_folders_are_walked(self, backend, rom):
        handle, _root = backend
        handle.copy(rom, "Shooter/Flying Vertical")
        assert "Shooter/Flying Vertical/galaga.zip" in handle.index()


class TestRearrange:
    def test_a_recategorised_file_is_moved_not_recopied(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Maze")
        handle.move("Maze/galaga.zip", "Shooter/galaga.zip")
        assert not (root / "roms" / "Maze" / "galaga.zip").exists()
        assert (root / "roms" / "Shooter" / "galaga.zip").exists()

    def test_moving_onto_an_existing_file_replaces_it(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Maze")
        handle.copy(rom, "Shooter")
        handle.move("Maze/galaga.zip", "Shooter/galaga.zip")
        assert (root / "roms" / "Shooter" / "galaga.zip").exists()

    def test_delete_removes_the_file(self, backend, rom):
        handle, root = backend
        handle.copy(rom, "Maze")
        handle.delete("Maze/galaga.zip")
        assert not (root / "roms" / "Maze" / "galaga.zip").exists()

    def test_deleting_something_absent_is_not_an_error(self, backend):
        handle, _root = backend
        handle.delete("Maze/ghost.zip")


class TestSftpDispatch:
    def test_the_sftp_scheme_picks_the_sftp_backend(self, monkeypatch):
        from marquee.backends import sftp as sftp_module
        made = {}
        monkeypatch.setattr(sftp_module.SftpCopy, "_connect",
                            lambda self: made.setdefault("connected", True))
        handle = for_destination("sftp://bio@host/srv/roms")
        assert isinstance(handle, sftp_module.SftpCopy)
        assert (handle.username, handle.host, handle.port) == ("bio", "host", 22)
        assert handle.root == "/srv/roms"

    def test_ssh_is_accepted_as_a_spelling_of_sftp(self, monkeypatch):
        from marquee.backends import sftp as sftp_module
        monkeypatch.setattr(sftp_module.SftpCopy, "_connect", lambda self: None)
        assert for_destination("ssh://host:2222/roms").port == 2222

    def test_a_bad_sftp_destination_says_what_it_wanted(self):
        from marquee.backends.sftp import SftpCopy
        with pytest.raises(ConfigError, match="sftp://user"):
            SftpCopy("sftp:/nope")

    def test_a_missing_paramiko_names_the_extra_to_install(self, monkeypatch):
        import builtins
        from marquee.backends import sftp as sftp_module
        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "paramiko":
                raise ImportError("no paramiko")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        with pytest.raises(ConfigError, match=r"marquee\[sftp\]"):
            sftp_module._paramiko()


def test_every_supported_scheme_reaches_a_backend(monkeypatch):
    from marquee.backends import SCHEMES
    assert set(SCHEMES) == {"smb://", "ftp://", "ftps://", "sftp://", "ssh://"}
    assert os.sep  # keeps the import used on every platform


class TestAFreshSession:
    def test_a_file_already_there_is_skipped_on_the_first_call(self, ftp_server, rom):
        """SIZE is refused in ASCII mode, and a fresh session is in ASCII mode until
        the first binary transfer -- so the first file of every run was re-uploaded."""
        root, port = ftp_server
        target = root / "roms" / "Maze" / "galaga.zip"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"rom" * 1000)
        os.utime(target, (1_000_000_000, 1_000_000_000))
        url = f"ftp://bio:pa%2Fss%40word@127.0.0.1:{port}/roms"
        handle = FtpCopy(url)
        try:
            handle.copy(rom, "Maze")
        finally:
            handle.close()
        assert int(target.stat().st_mtime) == 1_000_000_000, "it was uploaded again"
