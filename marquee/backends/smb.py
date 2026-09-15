import os
import posixpath
import re
import socket

from smb import smb_structs
from smb.SMBConnection import SMBConnection

from ..reporting import Reporter
from . import BackendError, CopyBackend, is_managed

# smb://[user[:password]@]host[:port]/share[/path...]
# The old pattern only accepted IPv4 literals and \w share paths, so hostnames,
# ports, passwords with punctuation and folders with spaces or dots all failed.
# The userinfo group is greedy so it ends at the last '@' before the path, which
# keeps an '@' inside the password out of the hostname.
CONN_STR_RE = re.compile(r'smb://(?:([^/]*)@)?([^/:]+)(?::(\d+))?/(.+)')


class RemoteCopy(CopyBackend):
    def __init__(self, conn_str, reporter=None):
        self.reporter = reporter or Reporter()
        self.username = ''
        self.password = ''
        self.server = None
        self.port = None
        self.share_name = None
        self.remote_path = None
        self.conn = None
        self._listing_cache = {}
        self._known_dirs = set()
        self._parse_connection_string(conn_str)
        self._open_connection()

    def _parse_connection_string(self, conn_str):
        match = CONN_STR_RE.match(conn_str)
        if not match:
            raise ValueError('Invalid connection string')

        userinfo = match.group(1) or ''
        self.username, _, self.password = userinfo.partition(':')
        self.server = match.group(2)
        self.port = int(match.group(3)) if match.group(3) else None

        parts = [part for part in match.group(4).split('/') if part]
        self.share_name = parts[0]
        self.remote_path = '/'.join(parts[1:])

    def _open_connection(self):
        # pysmb defaults to NetBIOS on 139; modern Samba shares listen on 445.
        if self.port:
            attempts = [(self.port, self.port == 445)]
        else:
            attempts = [(445, True), (139, False)]

        errors = []
        for port, direct_tcp in attempts:
            conn = SMBConnection(self.username, self.password, 'client', self.server,
                                 use_ntlm_v2=True, is_direct_tcp=direct_tcp)
            try:
                if conn.connect(self.server, port):
                    self.conn = conn
                    return
                errors.append(f'port {port}: connection refused by server')
            except (socket.error, smb_structs.ProtocolError) as error:
                errors.append(f'port {port}: {error}')

        # Returning quietly here used to leave an unusable connection behind and
        # every later call failed with an unrelated error.
        raise ConnectionError(f"Failed to connect to {self.server}: {'; '.join(errors)}")

    def __del__(self):
        # __init__ can fail before self.conn exists.
        conn = getattr(self, 'conn', None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _listing(self, remote_dir):
        """Cached {filename: size} for a remote directory.

        listPath() used to run once per file, which is one network round trip per
        ROM.
        """
        key = remote_dir or '/'
        if key not in self._listing_cache:
            try:
                entries = self.conn.listPath(self.share_name, key)
            except smb_structs.OperationFailure:
                entries = []
            self._listing_cache[key] = {entry.filename: entry.file_size for entry in entries}
        return self._listing_cache[key]

    def _copy_file(self, local_file_path, remote_file, on_bytes=None, replace=False):
        remote_filename = posixpath.basename(remote_file)
        remote_dir = posixpath.dirname(remote_file)
        local_size = os.path.getsize(local_file_path)
        listing = self._listing(remote_dir)

        # The old code printed "skipping" as soon as the name matched, even when
        # the size differed and it went on to re-upload the file.
        if not replace and listing.get(remote_filename) == local_size:
            self.reporter.info(f"Skipping '{local_file_path}', already there at the same size")
            if on_bytes:
                on_bytes(local_size)
            return

        with open(local_file_path, 'rb') as f:
            self._store(remote_file, f)
        listing[remote_filename] = local_size
        if on_bytes:
            on_bytes(local_size)

    def _store(self, remote, handle):
        """Upload to a partial name, then rename over the target.

        A run killed part-way used to leave a truncated ROM under its final name on
        the console until the next run noticed the size. And the fifth positional
        argument that used to be passed here was `show_progress`, which on a current
        pysmb prints a tqdm bar to the web server's stderr for every file -- and on
        the pinned older one is a TypeError.
        """
        partial = remote + ".part"
        try:
            self.conn.storeFile(self.share_name, partial, handle, timeout=30)
            self._delete_quietly(remote)
            self.conn.rename(self.share_name, partial, remote)
        except (smb_structs.OperationFailure, smb_structs.ProtocolError,
                socket.error) as error:
            self._delete_quietly(partial)
            raise BackendError(f"Upload of '{remote}' failed: {error}") from error

    def _delete_quietly(self, remote):
        try:
            self.conn.deleteFiles(self.share_name, remote)
        except (smb_structs.OperationFailure, smb_structs.ProtocolError, socket.error):
            pass

    def write_text(self, relpath, text):
        import io
        remote = posixpath.join(self.remote_path, relpath) if self.remote_path else relpath
        self.create_remote_directory(posixpath.dirname(remote))
        body = text.encode("utf-8")
        self._store(remote, io.BytesIO(body))
        self._listing(posixpath.dirname(remote))[posixpath.basename(remote)] = len(body)
        return remote

    def put_file(self, source, relpath):
        remote = posixpath.join(self.remote_path, relpath) if self.remote_path else relpath
        size = os.path.getsize(source)
        if self._listing(posixpath.dirname(remote)).get(posixpath.basename(remote)) == size:
            return False
        self.create_remote_directory(posixpath.dirname(remote))
        with open(source, "rb") as handle:
            self._store(remote, handle)
        self._listing(posixpath.dirname(remote))[posixpath.basename(remote)] = size
        return True

    def _copy_folder(self, local_folder_path, remote_folder_path, on_bytes=None,
                     replace=False):
        self.create_remote_directory(remote_folder_path)
        # Upload all files in the local folder to the remote folder

        for file in sorted(os.listdir(local_folder_path)):
            local_path = os.path.join(local_folder_path, file)
            # os.path.join() would emit backslashes on Windows and break the SMB path.
            remote_path = posixpath.join(remote_folder_path, file)
            if os.path.isfile(local_path):
                self._copy_file(local_path, remote_path, on_bytes, replace)
            elif os.path.isdir(local_path):
                self._copy_folder(local_path, remote_path, on_bytes, replace)

    def create_remote_directory(self, remote_path):
        basepath = ''
        for subdir in remote_path.split('/'):
            if subdir == '':
                continue
            basepath = posixpath.join(basepath, subdir) if basepath else subdir
            if basepath in self._known_dirs:
                continue
            try:
                self.conn.createDirectory(self.share_name, basepath)
            except smb_structs.OperationFailure:
                # Almost always "already exists"; a real permission problem still
                # surfaces on the following listPath/storeFile.
                pass
            self._known_dirs.add(basepath)

    def copy(self, src, dest, on_bytes=None, replace=False):
        dest = posixpath.join(self.remote_path, dest.replace(os.sep, '/')).strip('/')
        if os.path.isfile(src):
            self.create_remote_directory(dest)
            self._copy_file(src, posixpath.join(dest, os.path.basename(src)), on_bytes,
                            replace)
        elif os.path.isdir(src):
            self._copy_folder(src, dest, on_bytes, replace)
        else:
            self.reporter.warn(f"Source '{src}' does not exist, skipping")

    # -- sync ---------------------------------------------------------------- #

    def index(self, on_progress=None):
        """Walk the share below remote_path. One listPath per directory, not per file."""
        found = {}
        pending = [self.remote_path.strip("/")]
        while pending:
            current = pending.pop()
            try:
                entries = self.conn.listPath(self.share_name, current or "/")
            except smb_structs.OperationFailure:
                continue
            for entry in entries:
                if entry.filename in (".", ".."):
                    continue
                child = posixpath.join(current, entry.filename) if current else entry.filename
                if entry.isDirectory:
                    pending.append(child)
                elif is_managed(entry.filename):
                    relative = posixpath.relpath(child, self.remote_path.strip("/") or ".")
                    found[relative] = entry.file_size
            if on_progress:
                on_progress(len(found))
        return found

    def _absolute(self, relpath):
        return posixpath.join(self.remote_path, relpath).strip("/")

    def move(self, from_relpath, to_relpath):
        target = self._absolute(to_relpath)
        self.create_remote_directory(posixpath.dirname(target))
        try:
            self.conn.rename(self.share_name, self._absolute(from_relpath), target)
        except (smb_structs.OperationFailure, smb_structs.ProtocolError,
                socket.error) as error:
            raise BackendError(f"Could not rename {from_relpath}: {error}") from error
        self._listing_cache.clear()

    def delete(self, relpath):
        try:
            self.conn.deleteFiles(self.share_name, self._absolute(relpath))
        except smb_structs.OperationFailure:
            pass
        self._listing_cache.clear()
