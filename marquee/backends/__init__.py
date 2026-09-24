"""Destinations a romset can be written to.

Every backend takes (source, destination_folder) where the folder is relative to the
root the backend was built with, and is responsible for skipping files that are already
there at the same size. Adding a protocol means adding a module here and a line to
`for_destination`.
"""
import re
import socket

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


# What a transfer that was killed leaves behind: the partial copy of a ROM or a disk,
# or the temporary link a hardlink goes through. Nothing else ever writes these
# names, and nothing reads them -- they only take space, for ever.
LEFTOVER_SUFFIXES = tuple(suffix + extra for suffix in MANAGED_SUFFIXES
                          for extra in (".part", ".link"))


def is_leftover(name):
    return name.lower().endswith(LEFTOVER_SUFFIXES)


# Where EmulationStation and its scrapers keep media. An empty one is still where they
# will look, so it is never tidied away.
KEPT_FOLDERS = frozenset(("images", "videos", "manuals", "media", "downloaded_images",
                          "downloaded_videos"))


def empty_folders(folders, occupied):
    """The folders below the root with no file anywhere beneath them, deepest first.

    `folders` is every folder an index walked, `occupied` those that directly hold a
    file of any kind -- a scraped image or a note counts as much as a ROM.
    """
    full = set()
    for folder in occupied:
        while folder:
            if folder in full:
                break
            full.add(folder)
            folder = folder.rpartition("/")[0]
    return sorted((folder for folder in folders
                   if folder and folder not in full and removable(folder)),
                  key=lambda folder: (-folder.count("/"), folder))


def removable(folder):
    """Whether an empty folder may go: never the root, never a media folder."""
    return bool(folder) and folder.split("/", 1)[0].lower() not in KEPT_FOLDERS


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

        Partial copies an interrupted run left (see `is_leftover`) are not in it; a
        backend collects their relative paths in `self.leftovers` as it walks.
        """
        raise NotImplementedError

    # Filled in by index(): see above. `folders` is every folder walked below the
    # root and `occupied` the ones holding a file of any kind, which is what finds the
    # folders a removed game left behind (see `empty_folders`).
    leftovers = ()
    folders = ()
    occupied = ()

    def disk_space(self):
        """(bytes free for this user, disk size) behind the destination, or None
        when the protocol cannot say (FTP has no such question)."""
        return None

    def free_space(self):
        space = self.disk_space()
        return space[0] if space else None

    def remove_folder(self, relpath):
        """Remove one folder if, and only if, it is empty. Returns whether it went.

        Every protocol refuses to remove a folder with anything in it, so this can
        never take a file with it -- which is the whole of its safety.
        """
        return False

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


# Where each protocol listens when the URL names no port. SMB answers on 445 and, on
# older boxes, only on 139.
_PORTS = {"smb://": (445, 139), "ftp://": (21,), "ftps://": (21,), "sftp://": (22,),
          "ssh://": (22,)}
_HOST_PORT = re.compile(r"^([^/:]+)(?::(\d+))?(?:/|$)")


def address(destination):
    """(host, ports to try) of a remote destination, or None for a local folder."""
    if not is_remote(destination):
        return None
    scheme = next(prefix for prefix in SCHEMES if destination.startswith(prefix))
    parts = _split(destination)
    rest = parts[3] if parts else destination[len(scheme):]
    match = _HOST_PORT.match(rest)
    if not match:
        return None
    port = match.group(2)
    return match.group(1), (int(port),) if port else _PORTS[scheme]


def answers(destination, timeout=3):
    """Whether anything is listening where a remote library lives.

    A plain connection and nothing more: no login, no listing, nothing written to the
    job's log. It is what decides that a console switched back on is worth planning
    against, once a minute, while it is off.
    """
    found = address(destination)
    if not found:
        return False
    host, ports = found
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


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


def effective_hardlink(config):
    """Whether this configuration links rather than copies.

    Pinned when the setting says True or False. Automatic (None) is on exactly when a
    link from the source folder into the library works, and never for a library on a
    share, where there is nothing to link to.
    """
    if config.hardlink is not None:
        return bool(config.hardlink)
    if not config.copy_path or is_remote(config.copy_path):
        return False
    from .local import can_link
    return bool(can_link(config.rom_dir, config.copy_path))


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
