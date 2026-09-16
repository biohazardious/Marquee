import os
import shutil
import time

from ..reporting import Reporter
from . import CopyBackend, is_managed

CHUNK_SIZE = 8 * 1024 * 1024
# Below this, shutil's own fast path is worth more than progress reporting.
CHUNKED_ABOVE = 64 * 1024 * 1024


class LocalCopy(CopyBackend):
    """Copies into a directory on this machine.

    When `hardlink` is on and the source sits on the same filesystem, the file is
    linked rather than copied. That matters more here than anywhere else: the
    categorised library is built out of the torrent folder, and linking means a
    340 GB library costs 340 GB in total rather than 680 GB, while the download
    client carries on seeding the very same bytes.
    """

    def __init__(self, copy_path, reporter=None, hardlink=False):
        if not copy_path.endswith(os.path.sep):
            copy_path += os.path.sep
        self.copy_path = copy_path
        self.reporter = reporter or Reporter()
        self.hardlink = hardlink
        self.linked = 0

    def copy(self, source, destination, on_bytes=None, replace=False):
        # Previously source/destination were stashed on self, which made the
        # instance single-use and _copy_directory dependent on hidden state.
        target = os.path.join(self.copy_path, destination)

        if os.path.isfile(source):
            self._copy_file(source, os.path.join(target, os.path.basename(source)),
                            on_bytes, replace)
        elif os.path.isdir(source):
            self._copy_directory(source, target, on_bytes, replace)
        else:
            self.reporter.warn(f"Source '{source}' does not exist, skipping")

    def _copy_file(self, src, dest, on_bytes=None, replace=False):
        source_size = os.path.getsize(src)

        if not replace and os.path.exists(dest) and source_size == os.path.getsize(dest):
            self.reporter.info(f"Skipping '{src}', already there at the same size")
            if on_bytes:
                on_bytes(source_size)
            return

        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # Replace, never overwrite in place. A destination file may be a hardlink --
        # to the source itself, or to a copy elsewhere -- and writing into it would
        # write through to every one of them. Unlinking first breaks that share.
        if os.path.lexists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass

        if self.hardlink and self._try_link(src, dest):
            self.linked += 1
            if on_bytes:
                on_bytes(source_size)
            self.reporter.info(f"Linked '{src}' to '{dest}'")
            return

        start_time = time.perf_counter()
        if on_bytes and source_size > CHUNKED_ABOVE:
            # A single CHD can be many gigabytes; without this a caller cannot show any
            # movement for minutes at a time.
            self._copy_in_chunks(src, dest, on_bytes)
        else:
            self._copy_whole(src, dest)
            if on_bytes:
                on_bytes(source_size)
        elapsed = time.perf_counter() - start_time

        # A small file can finish inside the timer's resolution.
        if elapsed > 0:
            transfer_speed = source_size / elapsed / (1024 * 1024)
            self.reporter.info(f"Copied '{src}' to '{dest}' ({transfer_speed:.2f} MB/s)")
        else:
            self.reporter.info(f"Copied '{src}' to '{dest}'")

    def _try_link(self, src, dest):
        """Hardlink when it is possible, and say nothing when it is not.

        It fails for ordinary reasons -- a different filesystem, exFAT or FAT32,
        an SMB mount, a read-only source -- and every one of them just means copying
        instead. Only the first is worth telling the user about.
        """
        try:
            if os.stat(src).st_dev != os.stat(os.path.dirname(dest)).st_dev:
                if self.linked == 0 and not getattr(self, "_said_cross_device", False):
                    self._said_cross_device = True
                    self.reporter.info(
                        "Source and destination are on different filesystems, so files "
                        "are copied rather than linked.")
                return False
            os.link(src, dest)
            return True
        except OSError:
            return False

    def _copy_whole(self, src, dest):
        """shutil.copy2 through a partial name, so a killed run cannot leave a
        truncated file under its final name for the console to load."""
        partial = dest + ".part"
        try:
            shutil.copy2(src, partial)
            os.replace(partial, dest)
        except BaseException:
            if os.path.exists(partial):
                try:
                    os.remove(partial)
                except OSError:
                    pass
            raise

    def _copy_in_chunks(self, src, dest, on_bytes):
        """Copy with progress, through a partial name so an interrupted run leaves a
        file the next pass replaces rather than one it would skip on matching size."""
        partial = dest + ".part"
        try:
            with open(src, "rb") as reader, open(partial, "wb") as writer:
                while True:
                    chunk = reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    writer.write(chunk)
                    on_bytes(len(chunk))
            shutil.copystat(src, partial)
            os.replace(partial, dest)
        except BaseException:
            if os.path.exists(partial):
                try:
                    os.remove(partial)
                except OSError:
                    pass
            raise

    def _copy_directory(self, source, destination, on_bytes=None, replace=False):
        for root, _dirs, files in os.walk(source):
            for file in files:
                source_path = os.path.join(root, file)
                dest_path = os.path.join(destination, os.path.relpath(source_path, source))
                self._copy_file(source_path, dest_path, on_bytes, replace)

    # -- sync ---------------------------------------------------------------- #

    def open_read(self, relpath):
        return open(os.path.join(self.copy_path, relpath.replace("/", os.sep)), "rb")

    def probe(self):
        root = self.copy_path.rstrip(os.sep) or os.sep
        if not os.path.isdir(root):
            raise FileNotFoundError(f"{root} is not a folder that exists yet")
        return sorted(name for name in os.listdir(root) if not name.startswith("."))

    def index(self, on_progress=None):
        root = self.copy_path.rstrip(os.sep)
        found = {}
        if not os.path.isdir(root):
            return found
        for current, _dirs, names in os.walk(root):
            relative = os.path.relpath(current, root)
            prefix = "" if relative == "." else relative.replace(os.sep, "/") + "/"
            for name in names:
                # A half-written chunked copy is not a real destination file, and only
                # ROMs and disks are ours to account for.
                if not is_managed(name):
                    continue
                try:
                    found[prefix + name] = os.stat(os.path.join(current, name)).st_size
                except OSError:
                    pass
            if on_progress:
                on_progress(len(found))
        return found

    def move(self, from_relpath, to_relpath):
        source = os.path.join(self.copy_path, from_relpath.replace("/", os.sep))
        target = os.path.join(self.copy_path, to_relpath.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(source, target)
        self._prune(os.path.dirname(source))

    def delete(self, relpath):
        target = os.path.join(self.copy_path, relpath.replace("/", os.sep))
        try:
            os.remove(target)
        except FileNotFoundError:
            return
        self._prune(os.path.dirname(target))

    def write_text(self, relpath, text):
        target = os.path.join(self.copy_path, relpath.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        partial = target + ".part"
        with open(partial, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(partial, target)
        return target

    def put_file(self, source, relpath):
        target = os.path.join(self.copy_path, relpath.replace("/", os.sep))
        try:
            if os.path.getsize(target) == os.path.getsize(source):
                return False
        except OSError:
            pass
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.lexists(target):
            try:
                os.remove(target)
            except OSError:
                pass
        if self.hardlink and self._try_link(source, target):
            return True
        self._copy_whole(source, target)
        return True

    def _prune(self, directory):
        """Drop directories a move or delete just emptied, up to the copy root."""
        root = os.path.abspath(self.copy_path.rstrip(os.sep))
        directory = os.path.abspath(directory)
        while directory.startswith(root) and directory != root:
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)
