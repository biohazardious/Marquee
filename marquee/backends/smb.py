import io
import os
import posixpath
import re
import socket
from urllib.parse import unquote

from smb import smb_structs
from smb.base import NotConnectedError, SMBTimeout
from smb.SMBConnection import SMBConnection

from ..reporting import Reporter
from . import BackendError, CopyBackend, is_leftover, is_managed

# smb://[user[:password]@]host[:port]/share[/path...]
# The old pattern only accepted IPv4 literals and \w share paths, so hostnames,
# ports, passwords with punctuation and folders with spaces or dots all failed.
# The userinfo group is greedy so it ends at the last '@' before the path, which
# keeps an '@' inside the password out of the hostname.
CONN_STR_RE = re.compile(r'smb://(?:([^/]*)@)?([^/:]+)(?::(\d+))?/(.+)')


# A session that dropped or a reply that never came. pysmb raises the first two as
# plain Exceptions, so nothing that caught OperationFailure saw them: one stalled
# write -- a NAS disk spinning up -- ended a 300 GB sync.
_LOST = (NotConnectedError, SMBTimeout, OSError)
# Anything a call to the share can fail with.
_FAILED = (smb_structs.OperationFailure, smb_structs.ProtocolError) + _LOST


def _brief(error):
    """pysmb's OperationFailure prints every SMB packet it saw. The first line is
    the message; the rest is a hex dump nobody can act on."""
    text = str(error).strip()
    return text.splitlines()[0] if text else error.__class__.__name__


class _Counting:
    """A file object that reports what is read from it, so an upload that pysmb
    drives in one call can still show progress."""

    def __init__(self, handle, on_bytes):
        self.handle, self.on_bytes = handle, on_bytes

    def read(self, size=-1):
        data = self.handle.read(size)
        if data:
            self.on_bytes(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def rewind(self, position):
        """Back to `position` for a retry, taking back what was reported past it."""
        here = self.handle.tell()
        self.handle.seek(position)
        self.on_bytes(position - here)


class _Retrying:
    """The backend's connection, as SmbReadable sees it: every call goes through
    `RemoteCopy._call`, so a zip read survives a session that dropped."""

    def __init__(self, backend):
        self.backend = backend

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.backend._call(name, *args, **kwargs)


class SmbReadable:
    """A read-only, seekable view of one file on a share.

    pysmb has no file handle to speak of: it fetches a byte range per call. That is
    exactly what reading a zip's central directory needs -- the last few kilobytes,
    then a seek or two -- so each read() is one ranged fetch and nothing is
    downloaded that is not asked for. Enough of the file protocol for zipfile.

    The end of the file is fetched once, as one block, the first time anything in it
    is read. zipfile reads the end record, then the central directory, then checks
    the end record again -- three round trips, and a getAttributes before them, for
    every zip in a 10,000-game library. A size already known from the listing
    spares the getAttributes too.
    """

    # A zip's end record plus its central directory: 64 KB covers a game with about
    # a thousand ROMs in it. Anything further back is still a ranged fetch.
    TAIL = 64 * 1024

    def __init__(self, conn, share, path, size=None):
        self.conn, self.share, self.path = conn, share, path
        self.size = size
        if self.size is None:
            try:
                self.size = conn.getAttributes(share, path).file_size
            except (smb_structs.OperationFailure, smb_structs.ProtocolError) as error:
                raise FileNotFoundError(path) from error
            except _LOST as error:
                raise OSError(f"reading {path}: {_brief(error)}") from error
        self.position = 0
        self._tail = None

    def _from_tail(self, length):
        start = max(0, self.size - self.TAIL)
        if self.position < start or self.position + length > self.size:
            return None
        if self._tail is None:
            self._tail = self._fetch(start, self.size - start)
        offset = self.position - start
        return self._tail[offset:offset + length]

    def read(self, length=-1):
        if length is None or length < 0:
            length = self.size - self.position
        length = max(0, min(length, self.size - self.position))
        if not length:
            return b""
        data = self._from_tail(length)
        if data is None:
            data = self._fetch(self.position, length)
        self.position += len(data)
        return data

    def _fetch(self, offset, length):
        buffer = io.BytesIO()
        try:
            self.conn.retrieveFileFromOffset(self.share, self.path, buffer,
                                             offset=offset, max_length=length)
        except _FAILED as error:
            raise OSError(f"reading {self.path}: {_brief(error)}") from error
        return buffer.getvalue()

    def seek(self, offset, whence=os.SEEK_SET):
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self.position, os.SEEK_END: self.size}[whence]
        self.position = max(0, base + offset)
        return self.position

    def tell(self):
        return self.position

    def seekable(self):
        return True

    def readable(self):
        return True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _query_free_space(conn, share, path, timeout=30):
    """(free bytes for this user, total bytes) of the disk behind a share, or None.

    pysmb has no call for it. This is its own getSecurity() with the filesystem
    asked instead of the security descriptor: SMB2 QUERY_INFO, FileFsFullSize-
    Information ([MS-FSCC] 2.5.4). It leans on pysmb's internals, so any failure
    at all -- SMB1, an older or newer pysmb, a server that says no -- is simply
    "not known", which is what the page said before this existed.
    """
    import struct
    import time as time_module

    from smb.base import _PendingRequest
    from smb.smb2_constants import SMB2_INFO_FILESYSTEM, SMB2_OPLOCK_LEVEL_NONE
    from smb.smb2_structs import (SMB2CloseRequest, SMB2CreateRequest, SMB2Message,
                                  SMB2QueryInfoRequest, SMB2TreeConnectRequest)
    from smb.smb_constants import (FILE_OPEN, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE,
                                   FILE_SHARE_READ, FILE_SHARE_WRITE, SEC_IMPERSONATE,
                                   SYNCHRONIZE)

    FS_FULL_SIZE = 7
    if not conn.is_using_smb2 or not conn.has_authenticated:
        return None
    expiry = time_module.time() + timeout
    path = path.replace("/", "\\").strip("\\")
    result, failure = [], []

    def fail(reason):
        failure.append(reason)
        conn.is_busy = False

    def send_create(tid):
        message = SMB2Message(SMB2CreateRequest(
            path, file_attributes=0, access_mask=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_access=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            oplock=SMB2_OPLOCK_LEVEL_NONE, impersonation=SEC_IMPERSONATE,
            create_options=0, create_disp=FILE_OPEN))
        message.tid = tid
        conn._sendSMBMessage(message)
        conn.pending_requests[message.mid] = _PendingRequest(
            message.mid, expiry, created, fail, tid=tid)

    def created(reply, **kwargs):
        if reply.status != 0:
            fail("open")
            return
        fid = reply.payload.fid
        message = SMB2Message(SMB2QueryInfoRequest(
            fid, flags=0, additional_info=0, info_type=SMB2_INFO_FILESYSTEM,
            file_info_class=FS_FULL_SIZE, input_buf=b"", output_buf_len=64))
        message.tid = kwargs["tid"]
        conn._sendSMBMessage(message)
        conn.pending_requests[message.mid] = _PendingRequest(
            message.mid, expiry, queried, fail, tid=kwargs["tid"], fid=fid)

    def queried(reply, **kwargs):
        if reply.status == 0 and len(reply.payload.data) >= 32:
            total, caller, _actual, sectors, sector_bytes = struct.unpack(
                "<QQQII", reply.payload.data[:32])
            unit = sectors * sector_bytes
            result.append((caller * unit, total * unit))
        message = SMB2Message(SMB2CloseRequest(kwargs["fid"]))
        message.tid = kwargs["tid"]
        conn._sendSMBMessage(message)
        conn.pending_requests[message.mid] = _PendingRequest(
            message.mid, expiry, closed, fail)

    def closed(_reply, **_kwargs):
        conn.is_busy = False

    conn.is_busy = True
    try:
        if share in conn.connected_trees:
            send_create(conn.connected_trees[share])
        else:
            def connected(reply, **_kwargs):
                if reply.status != 0:
                    fail("tree")
                    return
                conn.connected_trees[share] = reply.tid
                send_create(reply.tid)
            message = SMB2Message(SMB2TreeConnectRequest(
                r"\\%s\%s" % (conn.remote_name.upper(), share)))
            conn._sendSMBMessage(message)
            conn.pending_requests[message.mid] = _PendingRequest(
                message.mid, expiry, connected, fail)
        while conn.is_busy:
            conn._pollForNetBIOSPacket(timeout)
    finally:
        conn.is_busy = False
    return result[0] if result and not failure else None


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
        username, _, password = userinfo.partition(':')
        # Percent-decoded, as the FTP and SFTP backends already do: otherwise a '/'
        # in a password cannot be written into the URL at all. unquote leaves a '%'
        # that is not followed by two hex digits alone.
        self.username, self.password = unquote(username), unquote(password)
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

    def close(self):
        # The base class's close() is a no-op, and every caller's backend.close()
        # used to be one too: the session lived until garbage collection.
        conn, self.conn = getattr(self, 'conn', None), None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def __del__(self):
        # __init__ can fail before self.conn exists.
        self.close()

    def _reconnect(self):
        self.close()
        self._open_connection()

    def _call(self, method, *args, **kwargs):
        """One call to the share, reconnecting once if the session had dropped.

        pysmb has no reconnect of its own; a session that timed out stayed dead and
        every later call failed. A second failure is the caller's to report.
        """
        try:
            if self.conn is None:
                raise NotConnectedError("not connected")
            return getattr(self.conn, method)(*args, **kwargs)
        except _LOST:
            self._reconnect()
            return getattr(self.conn, method)(*args, **kwargs)

    def _listing(self, remote_dir):
        """Cached {filename: size} for a remote directory.

        listPath() used to run once per file, which is one network round trip per
        ROM.
        """
        key = remote_dir or '/'
        if key not in self._listing_cache:
            try:
                entries = self._call("listPath", self.share_name, key)
            except smb_structs.OperationFailure:
                entries = []
            except _FAILED as error:
                raise BackendError(f"Could not list {key}: {_brief(error)}") from error
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
            # Counted as pysmb reads it, not when it is done: a 12 GB disk used to
            # sit at the same byte count for seven minutes while the network was
            # busy, and the page called it stalled.
            self._store(remote_file, _Counting(f, on_bytes) if on_bytes else f)
        listing[remote_filename] = local_size

    def _store(self, remote, handle):
        """Upload to a partial name, then rename over the target.

        A run killed part-way used to leave a truncated ROM under its final name on
        the console until the next run noticed the size. And the fifth positional
        argument that used to be passed here was `show_progress`, which on a current
        pysmb prints a tqdm bar to the web server's stderr for every file -- and on
        the pinned older one is a TypeError.
        """
        partial = remote + ".part"
        start = handle.tell()
        try:
            try:
                self.conn.storeFile(self.share_name, partial, handle, timeout=30)
            except _LOST:
                # Once more on a fresh session, from the start of the file: a
                # half-written .part is overwritten, and what was counted is taken
                # back so the progress bar does not run past the total.
                if hasattr(handle, "rewind"):
                    handle.rewind(start)
                else:
                    handle.seek(start)
                self._reconnect()
                self.conn.storeFile(self.share_name, partial, handle, timeout=30)
            self._delete_quietly(remote)
            self._call("rename", self.share_name, partial, remote)
        except _FAILED as error:
            self._delete_quietly(partial)
            raise BackendError(f"Upload of '{remote}' failed: {_brief(error)}") from error

    def _delete_quietly(self, remote):
        try:
            self._call("deleteFiles", self.share_name, remote)
        except _FAILED:
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
                self._call("createDirectory", self.share_name, basepath)
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

    def probe(self):
        try:
            entries = self._call("listPath", self.share_name,
                                 self.remote_path.strip("/") or "/")
        except _FAILED as error:
            raise BackendError(f"Connected to {self.server}, but {self.share_name}/"
                               f"{self.remote_path} could not be listed: "
                               f"{_brief(error)}") from error
        return sorted(entry.filename for entry in entries
                      if entry.filename not in (".", ".."))

    def index(self, on_progress=None):
        """Walk the share below remote_path. One listPath per directory, not per file."""
        found = {}
        self.leftovers = []
        self.folders, self.occupied = [], set()
        base = self.remote_path.strip("/")
        pending = [base]
        while pending:
            current = pending.pop()
            here = posixpath.relpath(current, base or ".") if current != base else ""
            if here:
                self.folders.append(here)
            try:
                entries = self._call("listPath", self.share_name, current or "/")
            except smb_structs.OperationFailure:
                # Unreadable is not empty: whatever is in there, it is not ours to
                # call nothing.
                if here:
                    self.occupied.add(here)
                continue
            except _FAILED as error:
                # A half-walked library reads as missing everything it did not reach:
                # all of it planned as new, the rest as orphans. Better to stop.
                raise BackendError(f"Lost the share while listing {current or '/'}: "
                                   f"{_brief(error)}") from error
            # Kept: the listing is what a later copy or check would ask for again.
            self._listing_cache[current or '/'] = {
                entry.filename: entry.file_size for entry in entries
                if not entry.isDirectory and entry.filename not in (".", "..")}
            for entry in entries:
                if entry.filename in (".", ".."):
                    continue
                child = posixpath.join(current, entry.filename) if current else entry.filename
                if entry.isDirectory:
                    pending.append(child)
                    continue
                if here:
                    self.occupied.add(here)
                if is_leftover(entry.filename):
                    self.leftovers.append(
                        posixpath.relpath(child, self.remote_path.strip("/") or "."))
                elif is_managed(entry.filename):
                    relative = posixpath.relpath(child, self.remote_path.strip("/") or ".")
                    found[relative] = entry.file_size
            if on_progress:
                on_progress(len(found))
        return found

    def open_read(self, relpath):
        remote = self._absolute(relpath)
        # The size, when a listing already said it: one round trip fewer a file.
        known = self._listing_cache.get(posixpath.dirname(remote) or '/', {})
        return SmbReadable(_Retrying(self), self.share_name, remote,
                           size=known.get(posixpath.basename(remote)))

    def _absolute(self, relpath):
        return posixpath.join(self.remote_path, relpath).strip("/")

    def move(self, from_relpath, to_relpath):
        target = self._absolute(to_relpath)
        self.create_remote_directory(posixpath.dirname(target))
        # SMB will not rename onto an existing file, and the alternative -- deleting
        # what is there first -- would destroy a file the plan wanted kept. Say so.
        if posixpath.basename(target) in self._listing(posixpath.dirname(target)):
            raise BackendError(f"{to_relpath} is already there; {from_relpath} was "
                               f"left where it is")
        try:
            self._call("rename", self.share_name, self._absolute(from_relpath), target)
        except _FAILED as error:
            raise BackendError(f"Could not rename {from_relpath}: "
                               f"{_brief(error)}") from error
        self._listing_cache.clear()

    def disk_space(self):
        try:
            return _query_free_space(self.conn, self.share_name,
                                     self.remote_path.strip("/"))
        except Exception:  # noqa: BLE001 - see _query_free_space
            return None

    def remove_folder(self, relpath):
        # The server refuses a folder with anything in it (STATUS_DIRECTORY_NOT_EMPTY).
        try:
            self._call("deleteDirectory", self.share_name, self._absolute(relpath))
        except _FAILED:
            return False
        self._listing_cache.clear()
        return True

    def delete(self, relpath):
        try:
            self._call("deleteFiles", self.share_name, self._absolute(relpath))
        except smb_structs.OperationFailure:
            pass
        except _FAILED as error:
            raise BackendError(f"Could not delete {relpath}: {_brief(error)}") from error
        self._listing_cache.clear()
