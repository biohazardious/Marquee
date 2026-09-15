"""SFTP destinations, over SSH.

paramiko is an optional dependency: a local or SMB destination should not force an
SSH stack into the install. When it is missing the error says what to install rather
than raising ImportError from somewhere deep in a copy loop.
"""
import os
import posixpath
import re
import stat as stat_module
from urllib.parse import unquote

from ..errors import ConfigError
from ..reporting import Reporter
from . import BackendError, CopyBackend, is_managed

# sftp|ssh://[user[:password]@]host[:port]/path
CONN_STR_RE = re.compile(r'(?:sftp|ssh)://(?:([^/]*)@)?([^/:]+)(?::(\d+))?(?:/(.*))?$')

CHUNK_SIZE = 8 * 1024 * 1024


def _paramiko():
    try:
        import paramiko
    except ImportError:
        raise ConfigError(
            "SFTP destinations need paramiko. Install it with "
            "'pip install marquee[sftp]', or use a local, SMB or FTP destination.")
    return paramiko


class SftpCopy(CopyBackend):
    def __init__(self, conn_str, reporter=None, timeout=30, key_file=None):
        self.reporter = reporter or Reporter()
        self.timeout = timeout
        self.key_file = key_file
        self._known_dirs = set()
        self._parse(conn_str)
        self._connect()

    def _parse(self, conn_str):
        match = CONN_STR_RE.match(conn_str)
        if not match:
            raise ConfigError(
                f"Not a usable SFTP destination: {conn_str!r}. Expected something like "
                f"sftp://user@host/srv/roms/mame")
        userinfo, host, port, path = match.groups()
        username, _, password = (userinfo or "").partition(":")
        self.username = unquote(username) or os.environ.get("USER", "root")
        self.password = unquote(password) or None
        self.host = host
        self.port = int(port) if port else 22
        self.root = "/" + (path or "").strip("/")

    def _connect(self):
        paramiko = _paramiko()
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        # A console on the LAN is rarely in known_hosts, and refusing to start over
        # that helps nobody. The transfer is still encrypted.
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(
                self.host, port=self.port, username=self.username,
                password=self.password, key_filename=self.key_file,
                timeout=self.timeout, allow_agent=True, look_for_keys=True)
            self.conn = self.client.open_sftp()
        except Exception as error:
            raise ConfigError(
                f"Could not connect to {self.username}@{self.host}:{self.port} - {error}")

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
        partial = remote + ".part"
        moved = 0
        try:
            with open(local, "rb") as reader, self.conn.open(partial, "wb") as writer:
                writer.set_pipelined(True)
                while True:
                    chunk = reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    writer.write(chunk)
                    moved += len(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
            self._delete_quietly(remote)
            self.conn.rename(partial, remote)
        except Exception as error:
            self._delete_quietly(partial)
            if on_bytes and moved:
                on_bytes(-moved)
            raise BackendError(f"Upload of '{local}' failed: {error}")
        self.reporter.info(f"Uploaded '{local}' to '{remote}'")
        return True

    def _makedirs(self, directory):
        if not directory or directory in self._known_dirs:
            return
        built = ""
        for part in directory.strip("/").split("/"):
            built = f"{built}/{part}"
            if built in self._known_dirs:
                continue
            try:
                self.conn.mkdir(built)
            except IOError:
                pass                      # already there, or reported by the upload
            self._known_dirs.add(built)

    # -- reading ------------------------------------------------------------- #

    def _size(self, remote):
        try:
            return self.conn.stat(remote).st_size
        except IOError:
            return None

    def index(self, on_progress=None):
        found = {}
        self._walk(self.root, "", found, on_progress)
        return found

    def _walk(self, directory, prefix, found, on_progress):
        try:
            entries = self.conn.listdir_attr(directory)
        except IOError:
            return
        for entry in entries:
            path = posixpath.join(directory, entry.filename)
            if stat_module.S_ISDIR(entry.st_mode):
                self._walk(path, f"{prefix}{entry.filename}/", found, on_progress)
            elif is_managed(entry.filename):
                found[prefix + entry.filename] = entry.st_size
        if on_progress:
            on_progress(len(found))

    # -- rearranging --------------------------------------------------------- #

    def move(self, from_relpath, to_relpath):
        source = posixpath.join(self.root, from_relpath)
        target = posixpath.join(self.root, to_relpath)
        self._makedirs(posixpath.dirname(target))
        self._delete_quietly(target)
        try:
            self.conn.rename(source, target)
        except (IOError, OSError) as error:
            raise BackendError(f"Could not rename {from_relpath}: {error}") from error

    def delete(self, relpath):
        self._delete_quietly(posixpath.join(self.root, relpath))

    def _delete_quietly(self, remote):
        try:
            self.conn.remove(remote)
        except IOError:
            pass

    def write_text(self, relpath, text):
        remote = posixpath.join(self.root, relpath)
        self._makedirs(posixpath.dirname(remote))
        partial = remote + ".part"
        try:
            with self.conn.open(partial, "w") as handle:
                handle.write(text)
            self._delete_quietly(remote)
            self.conn.rename(partial, remote)
        except (IOError, OSError) as error:
            self._delete_quietly(partial)
            raise BackendError(f"Could not write {relpath}: {error}") from error
        return remote

    def put_file(self, source, relpath):
        # The same upload as a ROM's: skipped at the same size, through a partial name.
        return self._put(source, posixpath.join(self.root, relpath))

    def close(self):
        try:
            self.conn.close()
        finally:
            self.client.close()
