"""Destinations a romset can be written to.

Every backend takes (source, destination_folder) where the folder is relative to the
root the backend was built with, and is responsible for skipping files that are already
there at the same size. Adding a protocol means adding a module here and a line to
`for_destination`.
"""
import re

from ..errors import ConfigError, MarqueeError


class BackendError(MarqueeError):
    """A destination operation failed: a rename refused, a share gone away.

    Every backend raises this rather than its library's own exception type, so the
    pipeline can warn and carry on from one place. It used to catch OSError only,
    which is what the local backend raises -- an SMB or FTP rename refused for a
    permission problem stopped the whole run before a file was copied.
    """


# Only these are ever treated as ours. A destination is somebody's console folder: it can
# hold a scraped gamelist.xml, box art, ES metadata and the odd dotfile, and none of that
# may be mistaken for a leftover ROM and deleted.
MANAGED_SUFFIXES = (".zip", ".chd")


def is_managed(name):
    return name.lower().endswith(MANAGED_SUFFIXES)


class CopyBackend:
    """The interface `pipeline.execute` relies on.

    Implemented by `local`, `smb`, `ftp` and `sftp`.
    """

    def copy(self, source, destination, on_bytes=None, replace=False):
        """Copy a file or directory to `destination`, relative to the backend's root.

        `on_bytes(count)` is called as data moves, so a caller can show progress through
        a single multi-gigabyte CHD rather than only between machines.

        A file already there at the same size is skipped -- unless `replace` says
        otherwise. The check inside the files (marquee.verify) can find a redump that
        weighs exactly what the dump it replaces weighed, and a size check alone would
        leave it in place forever while every page said it was out of date.
        """
        raise NotImplementedError

    def close(self):
        """Release whatever connection the backend holds. A local folder holds none."""

    # Whether files at the destination can be read back with random access. That is
    # what checking a zip's contents needs; plain FTP cannot do it.
    READS_BACK = True

    def open_read(self, relpath):
        """A seekable, read-only file object for one file at the destination, or
        FileNotFoundError. The library check reads every zip's central directory
        through this, which is a few kilobytes at the end of each file."""
        raise NotImplementedError

    def probe(self):
        """The names at the destination's top level: proof the place can be reached
        and read, without walking all of it. Used by the settings page's Test."""
        raise NotImplementedError

    def index(self, on_progress=None):
        """{relative path: size} of everything already at the destination.

        This is what turns a blind copy into a sync: without it a run cannot tell a file
        that moved from one that is missing, nor spot what an older romset left behind.
        """
        raise NotImplementedError

    def move(self, from_relpath, to_relpath):
        """Relocate a file already at the destination. Far cheaper than re-copying."""
        raise NotImplementedError

    def delete(self, relpath):
        """Remove a file from the destination."""
        raise NotImplementedError

    def write_text(self, relpath, text):
        """Write a small text file at an exact relative path.

        `copy` takes a folder and derives the filename from the source; a gamelist and
        its artwork need to land at a name we choose.
        """
        raise NotImplementedError

    def read_file(self, relpath):
        """The whole of a small file at the destination as bytes, or None when it is
        not there. For files we rewrite -- a gamelist EmulationStation has been adding
        favourites to -- so what is already in them can be kept."""
        try:
            with self.open_read(relpath) as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def put_file(self, source, relpath):
        """Copy one local file to an exact relative path.

        Returns True when something was transferred, False when it was already there
        at the same size -- artwork is written once and then skipped on every later
        run.
        """
        raise NotImplementedError


SCHEMES = ("smb://", "ftp://", "ftps://", "sftp://", "ssh://")


def is_remote(copy_path):
    """Whether the destination is reached over a network protocol rather than a path.

    One answer for every caller. Three of them used to test for "smb://" alone, so an
    ftp:// library had its manifest written into a local folder literally named
    "ftp:/user:password@host/..." -- credentials included -- relative to wherever the
    process happened to be started.
    """
    return bool(copy_path) and copy_path.startswith(SCHEMES)


# What a hidden password looks like on the page -- the same row of dots the download
# client's password comes back as.
MASK = "\u2022" * 8
# scheme://userinfo@ -- greedy up to the last '@' before the path, the way the SMB
# parser reads it, so an '@' inside the password stays with the password.
_USERINFO = re.compile(r"^([a-z]+://)([^/]*)@")


def _split(destination):
    """(scheme, user, password, rest) of a remote destination, or None."""
    match = _USERINFO.match(destination or "")
    if not match:
        return None
    user, _, password = match.group(2).partition(":")
    return match.group(1), user, password, destination[match.end():]


def redact(destination):
    """The destination with its password hidden, for anything a person or a log sees.

    A library on a share carries its credentials inside the URL, and that URL went
    out verbatim on every poll of /api/state and in every "could not reach" error.
    """
    parts = _split(destination) if is_remote(destination) else None
    if not parts or not parts[2]:
        return destination
    scheme, user, _password, rest = parts
    return f"{scheme}{user}:{MASK}@{rest}"


def unmask(given, saved):
    """Put the saved password back into a destination that came back masked.

    Only onto the same server and user: a masked URL pointing somewhere new would
    otherwise send the saved password to whoever answers there.
    """
    if not given or MASK not in given:
        return given
    new, old = _split(given), _split(saved or "")
    if new and old and new[2] == MASK and new[:2] == old[:2] \
            and new[3].split("/", 1)[0] == old[3].split("/", 1)[0]:
        scheme, user, _mask, rest = new
        return f"{scheme}{user}:{old[2]}@{rest}"
    raise ConfigError("The library's password is hidden on the page and cannot be "
                      "reused for a different server or user; type it in again.")


def for_destination(copy_path, reporter=None, hardlink=False):
    """Pick a backend from the destination string.

    `hardlink` only means anything locally: over a network protocol there is nothing
    to link to, and it is ignored rather than refused so one setting can serve every
    destination.
    """
    if not copy_path:
        raise ConfigError("No destination configured.")

    if copy_path.startswith("smb://"):
        from .smb import RemoteCopy
        return RemoteCopy(copy_path, reporter=reporter)

    if copy_path.startswith(("ftp://", "ftps://")):
        from .ftp import FtpCopy
        return FtpCopy(copy_path, reporter=reporter)

    if copy_path.startswith(("sftp://", "ssh://")):
        from .sftp import SftpCopy
        return SftpCopy(copy_path, reporter=reporter)

    from .local import LocalCopy
    return LocalCopy(copy_path, reporter=reporter, hardlink=hardlink)
