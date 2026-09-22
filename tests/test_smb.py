"""SMB backend behaviour, driven by a stand-in connection."""
import pytest

from marquee.backends import smb as RemoteCopy
from marquee.reporting import CollectingReporter


class Entry:
    def __init__(self, filename, file_size):
        self.filename, self.file_size = filename, file_size


class FakeConnection:
    """Records what the backend asked the server to do."""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.listed, self.stored, self.created = [], [], []

    def listPath(self, _share, path):
        self.listed.append(path)
        return [Entry(name, size) for name, size in self.existing.get(path, {}).items()]

    def createDirectory(self, _share, path):
        self.created.append(path)

    def storeFile(self, _share, path, _handle, timeout=30):
        # The real signature is (service_name, path, file_obj, timeout=30,
        # show_progress=False, ...): a fake that took a fifth positional hid a
        # tqdm bar being switched on for every upload.
        self.stored.append(path)

    def rename(self, _share, old, new):
        self.renamed = getattr(self, "renamed", []) + [(old, new)]
        if old in self.stored:
            self.stored[self.stored.index(old)] = new

    def deleteFiles(self, _share, path):
        self.deleted = getattr(self, "deleted", []) + [path]

    def close(self):
        pass


def build(conn_str, connection, reporter=None):
    """A RemoteCopy wired to a fake server, skipping the real connect."""
    copier = RemoteCopy.RemoteCopy.__new__(RemoteCopy.RemoteCopy)
    copier._listing_cache, copier._known_dirs = {}, set()
    copier.reporter = reporter or CollectingReporter()
    copier._parse_connection_string(conn_str)
    copier.conn = connection
    return copier


class TestConnectionString:
    def test_bare_host_and_share(self):
        copier = build("smb://192.168.1.105/Batocera3/roms/mame/", FakeConnection())
        assert (copier.server, copier.share_name, copier.remote_path) == \
            ("192.168.1.105", "Batocera3", "roms/mame")
        assert copier.username == copier.password == ""

    def test_hostname_instead_of_ip(self):
        assert build("smb://nas.local/Share/x", FakeConnection()).server == "nas.local"

    def test_explicit_port(self):
        assert build("smb://nas.local:445/Share/x", FakeConnection()).port == 445

    def test_credentials(self):
        copier = build("smb://bob:secret@nas/Share/x", FakeConnection())
        assert (copier.username, copier.password) == ("bob", "secret")

    def test_password_containing_an_at_sign(self):
        """The userinfo has to end at the last '@', not the first."""
        copier = build("smb://bob:p@ss-w0rd!@nas.local/Share/x", FakeConnection())
        assert (copier.username, copier.password, copier.server) == \
            ("bob", "p@ss-w0rd!", "nas.local")

    def test_share_path_with_spaces_and_dots(self):
        copier = build("smb://nas/Share/My Roms/mame.v2", FakeConnection())
        assert copier.remote_path == "My Roms/mame.v2"

    def test_share_root_without_subpath(self):
        copier = build("smb://nas/Share", FakeConnection())
        assert (copier.share_name, copier.remote_path) == ("Share", "")

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            build("not-an-smb-url", FakeConnection())


class TestRemotePaths:
    def test_uses_forward_slashes_everywhere(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"x")
        connection = FakeConnection()
        build("smb://nas/Share/roms/mame", connection).copy(str(source), "Maze/Misc")
        assert connection.stored == ["roms/mame/Maze/Misc/a.zip"]
        assert all("\\" not in path for path in connection.created + connection.stored)

    def test_creates_each_directory_level(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"x")
        connection = FakeConnection()
        build("smb://nas/Share/roms/mame", connection).copy(str(source), "Maze/Misc")
        assert connection.created == ["roms", "roms/mame", "roms/mame/Maze", "roms/mame/Maze/Misc"]

    def test_share_root_destination_has_no_leading_slash(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"x")
        connection = FakeConnection()
        build("smb://nas/Share", connection).copy(str(source), "Maze")
        assert connection.stored == ["Maze/a.zip"]

    def test_directory_upload_recurses(self, tmp_path):
        source = tmp_path / "chd"
        (source / "sub").mkdir(parents=True)
        (source / "a.chd").write_bytes(b"one")
        (source / "sub" / "b.chd").write_bytes(b"two")
        connection = FakeConnection()
        build("smb://nas/Share/roms", connection).copy(str(source), "Maze/chd")
        assert set(connection.stored) == {"roms/Maze/chd/a.chd", "roms/Maze/chd/sub/b.chd"}

    def test_missing_source_is_reported_not_raised(self, tmp_path):
        connection, reporter = FakeConnection(), CollectingReporter()
        build("smb://nas/Share/roms", connection, reporter).copy(str(tmp_path / "nope"), "X")
        assert reporter.warnings and "does not exist" in reporter.warnings[0]
        assert connection.stored == []


class TestSkipLogic:
    def test_same_size_is_skipped(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"12345")
        connection, reporter = FakeConnection({"roms/X": {"a.zip": 5}}), CollectingReporter()
        build("smb://nas/Share/roms", connection, reporter).copy(str(source), "X")
        assert connection.stored == []
        assert "already there" in reporter.text

    def test_different_size_is_uploaded_without_claiming_to_skip(self, tmp_path):
        """The message used to print on any name match, even while re-uploading."""
        source = tmp_path / "a.zip"
        source.write_bytes(b"12345")
        connection, reporter = FakeConnection({"roms/X": {"a.zip": 99}}), CollectingReporter()
        build("smb://nas/Share/roms", connection, reporter).copy(str(source), "X")
        assert connection.stored == ["roms/X/a.zip"]
        assert "already there" not in reporter.text

    def test_absent_file_is_uploaded(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"12345")
        connection = FakeConnection({"roms/X": {"other.zip": 5}})
        build("smb://nas/Share/roms", connection).copy(str(source), "X")
        assert connection.stored == ["roms/X/a.zip"]


class TestListingCache:
    def test_one_listing_per_directory(self, tmp_path):
        source = tmp_path / "chd"
        source.mkdir()
        for index in range(50):
            (source / f"f{index}.chd").write_bytes(b"x" * 10)
        connection = FakeConnection()
        build("smb://nas/Share/roms", connection).copy(str(source), "Maze/chd")
        assert len(connection.stored) == 50
        assert len(connection.listed) == 1

    def test_upload_updates_the_cache(self, tmp_path):
        source = tmp_path / "a.zip"
        source.write_bytes(b"12345")
        connection, reporter = FakeConnection(), CollectingReporter()
        copier = build("smb://nas/Share/roms", connection, reporter)
        copier.copy(str(source), "X")
        copier.copy(str(source), "X")
        assert connection.stored == ["roms/X/a.zip"]
        assert "already there" in reporter.text

    def test_directories_are_created_once(self, tmp_path):
        connection = FakeConnection()
        copier = build("smb://nas/Share/roms", connection)
        for name in ("a", "b"):
            source = tmp_path / f"{name}.zip"
            source.write_bytes(b"x")
            copier.copy(str(source), "Maze/Misc")
        assert connection.created == ["roms", "roms/Maze", "roms/Maze/Misc"]


class TestConnection:
    def test_failure_raises_instead_of_returning_quietly(self, monkeypatch):
        class Unreachable:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self, *_args, **_kwargs):
                raise OSError("no route to host")

        monkeypatch.setattr(RemoteCopy, "SMBConnection", Unreachable)
        with pytest.raises(ConnectionError, match="no route to host"):
            RemoteCopy.RemoteCopy("smb://10.0.0.1/Share/x")

    def test_refusal_raises(self, monkeypatch):
        class Refusing:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self, *_args, **_kwargs):
                return False

        monkeypatch.setattr(RemoteCopy, "SMBConnection", Refusing)
        with pytest.raises(ConnectionError):
            RemoteCopy.RemoteCopy("smb://10.0.0.1/Share/x")

    def test_tries_445_before_139(self, monkeypatch):
        attempts = []

        class Recording:
            def __init__(self, *_args, **kwargs):
                self.direct = kwargs.get("is_direct_tcp")

            def connect(self, _host, port):
                attempts.append((port, self.direct))
                return port == 139

        monkeypatch.setattr(RemoteCopy, "SMBConnection", Recording)
        RemoteCopy.RemoteCopy("smb://10.0.0.1/Share/x")
        assert attempts == [(445, True), (139, False)]

    def test_explicit_port_is_honoured(self, monkeypatch):
        attempts = []

        class Recording:
            def __init__(self, *_args, **_kwargs):
                pass

            def connect(self, _host, port):
                attempts.append(port)
                return True

        monkeypatch.setattr(RemoteCopy, "SMBConnection", Recording)
        RemoteCopy.RemoteCopy("smb://10.0.0.1:139/Share/x")
        assert attempts == [139]

    def test_early_failure_leaves_del_harmless(self):
        """__del__ used to touch self.conn before __init__ had set it."""
        with pytest.raises(ValueError):
            RemoteCopy.RemoteCopy("bogus")
        import gc
        gc.collect()


class TestUploadsAreAllOrNothing:
    def test_a_file_lands_under_a_partial_name_and_is_renamed_over(self, tmp_path):
        source = tmp_path / "galaga.zip"
        source.write_bytes(b"rom" * 10)
        conn = FakeConnection()
        copier = build("smb://u:p@nas/Share/roms", conn)
        copier.copy(str(source), "Maze")
        assert conn.renamed == [("roms/Maze/galaga.zip.part", "roms/Maze/galaga.zip")]
        assert conn.stored == ["roms/Maze/galaga.zip"]


class TestReadingAFileBack:
    """zipfile needs seek, tell and read; pysmb offers ranged fetches. SmbReadable is
    the adapter, and it must fetch only what is asked for."""

    class Attributes:
        def __init__(self, size):
            self.file_size = size

    class RangedConnection(FakeConnection):
        def __init__(self, files):
            super().__init__()
            self.files, self.fetched = files, []

        def getAttributes(self, _share, path):
            if path not in self.files:
                raise RemoteCopy.smb_structs.OperationFailure("no such file", [])
            return TestReadingAFileBack.Attributes(len(self.files[path]))

        def retrieveFileFromOffset(self, _share, path, out, offset=0, max_length=-1, **_kw):
            data = self.files[path][offset:offset + max_length if max_length >= 0 else None]
            self.fetched.append((offset, len(data)))
            out.write(data)

    def test_a_zip_on_the_share_is_indexed_from_its_tail(self):
        import io, zipfile
        from marquee import verify
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("pacman.6e", b"x" * 4000)
            archive.writestr("pacman.6f", b"y" * 4000)
        conn = self.RangedConnection({"roms/Maze/pacman.zip": payload.getvalue()})
        copier = build("smb://u:p@nas/Share/roms", conn)
        with copier.open_read("Maze/pacman.zip") as handle:
            found = verify.zip_index(handle)
        assert set(found) == {"pacman.6e", "pacman.6f"}
        # The central directory, not the whole 8 KB of content.
        assert sum(length for _offset, length in conn.fetched) < 2000

    def test_a_missing_file_is_file_not_found(self):
        conn = self.RangedConnection({})
        copier = build("smb://u:p@nas/Share/roms", conn)
        with pytest.raises(FileNotFoundError):
            copier.open_read("Maze/none.zip")


class TestProgressDuringAnUpload:
    def test_bytes_are_counted_as_pysmb_reads_them(self, tmp_path):
        source = tmp_path / "big.chd"
        source.write_bytes(b"c" * 300_000)

        class Reading(FakeConnection):
            def storeFile(self, _share, path, handle, timeout=30):
                while handle.read(65536):
                    pass
                self.stored.append(path)

        conn = Reading()
        copier = build("smb://u:p@nas/Share/roms", conn)
        moved = []
        copier.copy(str(source), "Maze", on_bytes=moved.append)
        assert sum(moved) == 300_000
        assert len(moved) >= 4, "reported in pieces, not once at the end"


class TestARenameOntoAFileThatIsThere:
    def test_is_refused_in_one_sentence(self):
        conn = FakeConnection({"roms/New": {"a.chd": 5}, "roms/Old": {"a.chd": 5}})
        copier = build("smb://u:p@nas/Share/roms", conn)
        with pytest.raises(Exception) as error:
            copier.move("Old/a.chd", "New/a.chd")
        assert "already there" in str(error.value)
        assert "SMB Message" not in str(error.value)
        assert getattr(conn, "renamed", []) == []


class TestALostSession:
    """pysmb's NotConnectedError and SMBTimeout are plain Exceptions: nothing caught
    them, there was no reconnect, and one stalled write ended a 300 GB sync."""

    class Flaky(FakeConnection):
        def __init__(self, fail, times=1, error=None, existing=None):
            super().__init__(existing)
            from smb.base import NotConnectedError
            self.fail, self.left = fail, times
            self.error = error or NotConnectedError("session dropped")
            self.closed = False

        def _maybe(self, name):
            if name == self.fail and self.left:
                self.left -= 1
                raise self.error

        def storeFile(self, share, path, handle, timeout=30):
            handle.read(4)           # pysmb has read some of it by the time it fails
            self._maybe("storeFile")
            while handle.read(4):
                pass
            super().storeFile(share, path, handle, timeout)

        def listPath(self, share, path):
            self._maybe("listPath")
            return super().listPath(share, path)

        def rename(self, share, old, new):
            self._maybe("rename")
            super().rename(share, old, new)

        def close(self):
            self.closed = True

    def wired(self, conn):
        copier = build("smb://u:p@nas/Share/roms", conn)
        copier.reconnects = 0

        def reopen():
            copier.reconnects += 1
            copier.conn = conn
        copier._open_connection = reopen
        return copier

    def test_an_upload_is_retried_on_a_fresh_session(self, tmp_path):
        source = tmp_path / "galaga.zip"
        source.write_bytes(b"x" * 10)
        conn = self.Flaky("storeFile")
        copier = self.wired(conn)
        counted = []
        copier.copy(str(source), "Shooter", on_bytes=counted.append)
        assert copier.reconnects == 1
        assert conn.stored == ["roms/Shooter/galaga.zip"]
        # What was counted before the drop is taken back: the bar ends at the size.
        assert sum(counted) == 10

    def test_a_second_failure_is_a_backend_error_for_that_file(self, tmp_path):
        from marquee.backends import BackendError
        source = tmp_path / "galaga.zip"
        source.write_bytes(b"x" * 10)
        copier = self.wired(self.Flaky("storeFile", times=2))
        with pytest.raises(BackendError, match="Upload"):
            copier.copy(str(source), "Shooter")

    def test_a_timeout_is_treated_the_same(self, tmp_path):
        from smb.base import SMBTimeout
        conn = self.Flaky("rename", error=SMBTimeout())
        copier = self.wired(conn)
        copier.move("Old/a.zip", "New/a.zip")
        assert copier.reconnects == 1
        assert conn.renamed[-1] == ("roms/Old/a.zip", "roms/New/a.zip")

    def test_an_index_that_loses_the_share_stops_rather_than_half_listing(self):
        from marquee.backends import BackendError
        copier = self.wired(self.Flaky("listPath", times=2,
                                       existing={"roms": {"a.zip": 1}}))
        with pytest.raises(BackendError, match="Lost the share"):
            copier.index()

    def test_close_closes_the_session(self):
        conn = FakeConnection()
        conn.closed = False
        conn.close = lambda: setattr(conn, "closed", True)
        copier = build("smb://nas/Share/roms", conn)
        copier.close()
        assert conn.closed and copier.conn is None
        copier.close()                # twice is harmless
