"""The SFTP backend's rearranging, against a stand-in for paramiko's SFTPClient.

There is no SFTP server in the test dependencies; what matters here is which calls
the backend makes, and a local folder answers them the way a server would.
"""
import os
import types

import pytest

from marquee.backends import BackendError
from marquee.backends.sftp import SftpCopy


class FolderSftp:
    """The handful of SFTPClient calls the backend uses, over a local folder."""

    def __init__(self, root):
        self.root = str(root)

    def _local(self, remote):
        return os.path.join(self.root, remote.lstrip("/"))

    def stat(self, remote):
        try:
            return types.SimpleNamespace(st_size=os.path.getsize(self._local(remote)))
        except OSError as error:
            raise IOError(str(error)) from error

    def mkdir(self, remote):
        try:
            os.mkdir(self._local(remote))
        except OSError as error:
            raise IOError(str(error)) from error

    def rename(self, source, target):
        # SFTP's own rename refuses an existing target; the danger was the backend
        # deleting it first.
        if os.path.exists(self._local(target)):
            raise IOError("Failure")
        os.rename(self._local(source), self._local(target))

    def remove(self, remote):
        try:
            os.remove(self._local(remote))
        except OSError as error:
            raise IOError(str(error)) from error


@pytest.fixture
def backend(tmp_path):
    handle = SftpCopy.__new__(SftpCopy)
    handle._known_dirs = set()
    handle.root = "/roms"
    handle.conn = FolderSftp(tmp_path)
    (tmp_path / "roms").mkdir()
    return handle, tmp_path / "roms"


class TestMove:
    def test_a_recategorised_file_is_moved(self, backend):
        handle, root = backend
        (root / "Maze").mkdir()
        (root / "Maze" / "galaga.zip").write_bytes(b"rom")
        handle.move("Maze/galaga.zip", "Shooter/galaga.zip")
        assert (root / "Shooter" / "galaga.zip").read_bytes() == b"rom"
        assert not (root / "Maze" / "galaga.zip").exists()

    def test_moving_onto_an_existing_file_is_refused(self, backend):
        # The file already there may be the one the plan keeps; deleting it first
        # used to destroy it. SMB and local refuse; so does SFTP.
        handle, root = backend
        for folder, body in (("Maze", b"rom"), ("Shooter", b"kept")):
            (root / folder).mkdir()
            (root / folder / "galaga.zip").write_bytes(body)
        with pytest.raises(BackendError, match="already there"):
            handle.move("Maze/galaga.zip", "Shooter/galaga.zip")
        assert (root / "Shooter" / "galaga.zip").read_bytes() == b"kept"
        assert (root / "Maze" / "galaga.zip").exists()


class TestALostConnection:
    def test_a_dropped_transport_is_a_backend_error_not_a_crash(self, backend):
        import paramiko
        handle, root = backend
        (root / "Maze").mkdir()
        (root / "Maze" / "galaga.zip").write_bytes(b"rom")

        def dropped(*_args):
            raise paramiko.SSHException("Server connection dropped")
        handle.conn.rename = dropped
        with pytest.raises(BackendError, match="dropped"):
            handle.move("Maze/galaga.zip", "Shooter/galaga.zip")

    def test_an_index_that_loses_the_connection_stops(self, backend):
        handle, _root = backend

        def gone(_path):
            raise EOFError()
        handle.conn.listdir_attr = gone
        with pytest.raises(BackendError, match="Lost the connection"):
            handle.index()
