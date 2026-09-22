"""FTP and FTPS destinations.

ftplib is in the standard library, so this costs nothing to support. The only real
work is that FTP has no "make these parent directories" call and no reliable
recursive listing, so both are built here.
"""
import ftplib
import os
import posixpath
import re
import ssl
from urllib.parse import unquote

from ..errors import ConfigError
from ..reporting import Reporter
from . import BackendError, CopyBackend, is_managed, redact

# ftp[s]://[user[:password]@]host[:port]/path
CONN_STR_RE = re.compile(r'(ftps?)://(?:([^/]*)@)?([^/:]+)(?::(\d+))?(?:/(.*))?$')

CHUNK_SIZE = 8 * 1024 * 1024


class FtpCopy(CopyBackend):
    def __init__(self, conn_str, reporter=None, timeout=30):
        self.reporter = reporter or Reporter()
        self.timeout = timeout
        self._known_dirs = set()
        self._parse(conn_str)
        self._connect()

    def _parse(self, conn_str):
        match = CONN_STR_RE.match(conn_str)
        if not match:
            raise ConfigError(
                f"Not a usable FTP destination: {redact(conn_str)!r}. Expected "
                f"something like ftp://user:password@host/roms/mame")
        scheme, userinfo, host, port, path = match.groups()
        self.secure = scheme == "ftps"
        username, _, password = (userinfo or "").partition(":")
        # A password may legitimately contain '@' or '/', so it is percent-encoded
        # in the URL and has to be decoded back.
        self.username = unquote(username) or "anonymous"
        self.password = unquote(password)
        self.host = host
        self.port = int(port) if port else 21
        self.root = "/" + (path or "").strip("/")

    def _connect(self):
        try:
            if self.secure:
                self.conn = ftplib.FTP_TLS(timeout=self.timeout,
                                           context=ssl.create_default_context())
            else:
                self.conn = ftplib.FTP(timeout=self.timeout)
            self.conn.connect(self.host, self.port)
            self.conn.login(self.username, self.password)
            if self.secure:
                # Without this the control channel is encrypted and the data channel
                # is not, which most servers reject outright.
                self.conn.prot_p()
            self.conn.set_pasv(True)
            # SIZE is refused in ASCII mode, and a fresh session is in ASCII mode until
            # the first binary transfer -- so the first file of every run used to be
            # re-uploaded because its size could not be asked for.
            self.conn.voidcmd("TYPE I")
        except ftplib.all_errors as error:
            raise ConfigError(f"Could not connect to {self.host}:{self.port} - {error}")

    # -- writing ------------------------------------------------------------- #

    def copy(self, source, destination, on_bytes=None, replace=False):
        target = posixpath.join(self.root, destination.replace(os.sep, "/"))
        if os.path.isfile(source):
            self._put(source, posixpath.join(target, os.path.basename(source)),
                      on_bytes, replace)
        elif os.path.isdir(source):
            for root, _dirs, names in os.walk(source):
                for name in names:
                    local = os.path.join(root, name)
                    relative = os.path.relpath(local, source).replace(os.sep, "/")
                    self._put(local, posixpath.join(target, relative), on_bytes,
                              replace)
        else:
            self.reporter.warn(f"Source '{source}' does not exist, skipping")

    def _put(self, local, remote, on_bytes=None, replace=False):
        """Upload one file. Returns False when it was already there at the same size."""
        size = os.path.getsize(local)
        if not replace and self._size(remote) == size:
            self.reporter.info(f"Skipping '{local}', already there at the same size")
            if on_bytes:
                on_bytes(size)
            return False

        self._makedirs(posixpath.dirname(remote))
        # Uploaded to a partial name so an interrupted transfer does not leave a file
        # the next run mistakes for complete on a size check.
        partial = remote + ".part"
        moved = 0

        def block(chunk):
            nonlocal moved
            moved += len(chunk)
            if on_bytes:
                on_bytes(len(chunk))

        try:
            with open(local, "rb") as handle:
                self.conn.storbinary(f"STOR {partial}", handle, blocksize=CHUNK_SIZE,
                                     callback=block)
            self._delete_quietly(remote)
            self.conn.rename(partial, remote)
        except ftplib.all_errors as error:
            self._delete_quietly(partial)
            if on_bytes and moved:
                on_bytes(-moved)
            raise BackendError(f"Upload of '{local}' failed: {error}")
        self.reporter.info(f"Uploaded '{local}' to '{remote}'")
        return True

    def _makedirs(self, directory):
        if not directory or directory in self._known_dirs:
            return
        parts = directory.strip("/").split("/")
        built = ""
        for part in parts:
            built = f"{built}/{part}"
            if built in self._known_dirs:
                continue
            try:
                self.conn.mkd(built)
            except ftplib.all_errors:
                # Already there, or a permission problem that the upload will report
                # far more clearly than a stray "cannot create directory" would.
                pass
            self._known_dirs.add(built)

    # -- reading ------------------------------------------------------------- #

    def _size(self, remote):
        try:
            return self.conn.size(remote)
        except ftplib.all_errors:
            return None

    def _binary(self):
        """Back to TYPE I after a listing. ftplib sends TYPE A for every NLST and
        MLSD, and SIZE is refused in ASCII mode: after one, every size read as
        unknown -- a library indexed at 0 bytes a file, a move's check followed by
        re-uploading a file that was already there."""
        try:
            self.conn.voidcmd("TYPE I")
        except ftplib.all_errors:
            pass

    # RETR streams from a REST offset, but there is no way to stop part-way without
    # tearing the transfer down; reading a zip's tail that way is not worth it.
    READS_BACK = False

    def open_read(self, relpath):
        raise BackendError("Files on an FTP library cannot be read back piecemeal; "
                           "check it over SFTP or SMB, or mount it locally.")

    def probe(self):
        try:
            names = self.conn.nlst(self.root)
            self._binary()
            return sorted(posixpath.basename(name) for name in names)
        except ftplib.all_errors as error:
            raise BackendError(f"Connected, but {self.root} could not be listed: "
                               f"{error}") from error

    def index(self, on_progress=None):
        found = {}
        self._walk(self.root, "", found, on_progress)
        return found

    def _walk(self, directory, prefix, found, on_progress):
        entries = []
        try:
            # MLSD gives a type for each entry; without it there is no way to tell a
            # directory from a file short of trying to enter it.
            entries = list(self.conn.mlsd(directory))
            self._binary()
        except ftplib.error_perm:
            # Not supported (vsftpd, among others): names only, and whether each
            # is a folder has to be found out by entering it.
            try:
                names = self.conn.nlst(directory)
            except ftplib.error_perm:
                return                    # an empty folder, on some servers
            except ftplib.all_errors as error:
                raise self._lost(directory, error) from error
            self._binary()
            entries = [(posixpath.basename(n), {}) for n in names]
        except ftplib.all_errors as error:
            raise self._lost(directory, error) from error

        for name, facts in entries:
            if name in (".", ".."):
                continue
            kind = facts.get("type")
            path = posixpath.join(directory, name)
            if kind is None and not is_managed(name) and self._is_dir(path):
                kind = "dir"
            if kind == "dir":
                self._walk(path, f"{prefix}{name}/", found, on_progress)
            elif kind == "file" or (kind is None and is_managed(name)):
                if not is_managed(name):
                    continue
                size = facts.get("size")
                found[prefix + name] = int(size) if size else (self._size(path) or 0)
        if on_progress:
            on_progress(len(found))

    # Plainly files: not worth a CWD each to find out. A library's images folder
    # alone holds thousands of them.
    FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".xml", ".txt",
                     ".json", ".ini", ".cfg", ".dat", ".part", ".7z", ".rar", ".pdf")

    def _is_dir(self, path):
        """Whether `path` is a folder, for a server that will not say (no MLSD).

        Without this the walk never went below the top level, and a library laid
        out in genre folders indexed as empty: every game planned as new.
        """
        if path.lower().endswith(self.FILE_SUFFIXES):
            return False
        try:
            here = self.conn.pwd()
            self.conn.cwd(path)
        except ftplib.all_errors:
            return False
        try:
            self.conn.cwd(here)
        except ftplib.all_errors:
            pass
        return True

    @staticmethod
    def _lost(directory, error):
        # A half-walked library reads as missing everything it did not reach.
        return BackendError(f"Lost the server while listing {directory}: {error}")

    # -- rearranging --------------------------------------------------------- #

    def move(self, from_relpath, to_relpath):
        source = posixpath.join(self.root, from_relpath)
        target = posixpath.join(self.root, to_relpath)
        self._makedirs(posixpath.dirname(target))
        # Most servers rename over an existing file without a word, and deleting it
        # first did the same by hand: either way a file the plan wanted kept is gone.
        # Refuse, the way the SMB and local backends do.
        if self._exists(target):
            raise BackendError(f"{to_relpath} is already there; {from_relpath} was "
                               f"left where it is")
        try:
            self.conn.rename(source, target)
        except ftplib.all_errors as error:
            raise BackendError(f"Could not rename {from_relpath}: {error}") from error

    def _exists(self, remote):
        # A listing rather than SIZE, which some servers refuse in ASCII mode and
        # which would then read as "not there".
        try:
            names = self.conn.nlst(posixpath.dirname(remote))
            self._binary()
        except ftplib.all_errors:
            return False
        wanted = posixpath.basename(remote)
        return any(posixpath.basename(name) == wanted for name in names)

    def delete(self, relpath):
        self._delete_quietly(posixpath.join(self.root, relpath))

    def _delete_quietly(self, remote):
        try:
            self.conn.delete(remote)
        except ftplib.all_errors:
            pass

    def read_file(self, relpath):
        # No random access over FTP, but a whole small file is one RETR.
        import io
        buffer = io.BytesIO()
        try:
            self.conn.retrbinary(f"RETR {posixpath.join(self.root, relpath)}",
                                 buffer.write)
        except ftplib.error_perm as error:
            if str(error).startswith("550"):
                return None
            raise BackendError(f"Could not read {relpath}: {error}") from error
        except ftplib.all_errors as error:
            raise BackendError(f"Could not read {relpath}: {error}") from error
        return buffer.getvalue()

    def write_text(self, relpath, text):
        import io
        remote = posixpath.join(self.root, relpath)
        self._makedirs(posixpath.dirname(remote))
        partial = remote + ".part"
        try:
            self.conn.storbinary(f"STOR {partial}", io.BytesIO(text.encode("utf-8")))
            self._delete_quietly(remote)
            self.conn.rename(partial, remote)
        except ftplib.all_errors as error:
            self._delete_quietly(partial)
            raise BackendError(f"Could not write {relpath}: {error}") from error
        return remote

    def put_file(self, source, relpath):
        # The same upload as a ROM's: skipped at the same size, through a partial name.
        return self._put(source, posixpath.join(self.root, relpath))

    def close(self):
        try:
            self.conn.quit()
        except Exception:
            try:
                self.conn.close()
            except Exception:
                pass
