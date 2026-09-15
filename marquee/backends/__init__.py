"""Destinations a romset can be written to.

Every backend takes (source, destination_folder) where the folder is relative to the
root the backend was built with, and is responsible for skipping files that are already
there at the same size. Adding a protocol means adding a module here and a line to
`for_destination`.
"""
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
