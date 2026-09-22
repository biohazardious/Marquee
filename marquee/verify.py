"""Is what is in the library actually the release we think it is?

A file with the right name is not the right file. MAME rebuilds ROM sets between
releases -- a bad dump replaced, a chip renamed, a mask ROM redumped -- and a library
that decides "we have it" from the filename alone carries those changes for ever. That
is the whole difference between a copy of a romset and a romset that is kept up to
date.

The check is exact and costs nothing but disk: a zip's central directory lists every
entry's name and CRC-32 without unpacking a byte. About 9 ms for a typical ROM zip,
so a 11,797-file library is checked in under two minutes.
"""
import hashlib
import os
import zipfile

from .reporting import Reporter

# Every ROM the release says it should hold is there, with the right CRC.
CURRENT = "current"
# A ROM is there under the right name with the wrong contents, or one this machine
# owns outright is missing. Either way it is not the release we are building.
STALE = "stale"
# Only inherited ROMs are missing. That is what a merged or split set looks like from
# here, and it is not evidence of the wrong version -- the parent's zip has them.
INCOMPLETE = "incomplete"
# The file is there and cannot be read as a zip.
DAMAGED = "damaged"
# Nothing at that path.
ABSENT = "absent"

# Ordered worst first, so a run can be summarised by what most needs doing.
STATES = (STALE, DAMAGED, INCOMPLETE, ABSENT, CURRENT)


def zip_index(source):
    """{entry name, lowercased: crc as eight hex digits} from the central directory.

    `source` is a path or an open, seekable file object -- a share on the console is
    read through the latter. Raises the zipfile error for a file that is not a
    readable archive; the caller decides what that means.
    """
    found = {}
    with zipfile.ZipFile(source) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            found[info.filename.lower()] = f"{info.CRC:08x}"
            # MAME writes flat zips, but a set rebuilt elsewhere may have foldered
            # them; the name is what identifies a ROM, not where it sits.
            found.setdefault(os.path.basename(info.filename).lower(), f"{info.CRC:08x}")
    return found


def check(path, expected, opener=None):
    """(state, [what is wrong]) for one machine's zip.

    `expected` is [(rom name, crc, inherited from the parent)]. Extra entries are fine:
    a non-merged zip carries its BIOS and device ROMs too, and nothing here is
    interested in those.

    `opener(path)` returns a seekable file object, or raises FileNotFoundError: that
    is how a library on a share is read. Without one, `path` is on this machine.
    """
    if not expected:
        # Nothing to check it against. Saying "current" would be a claim; the caller
        # treats an unknown machine as untouched.
        return (CURRENT, [])
    if opener is None:
        if not os.path.isfile(path):
            return (ABSENT, [])
        try:
            found = zip_index(path)
        except (zipfile.BadZipFile, OSError) as error:
            return (DAMAGED, [str(error)])
    else:
        try:
            handle = opener(path)
        except FileNotFoundError:
            return (ABSENT, [])
        try:
            with handle:
                found = zip_index(handle)
        except (zipfile.BadZipFile, OSError) as error:
            return (DAMAGED, [str(error)])

    wrong, missing, inherited = [], [], []
    for name, crc, merged in expected:
        here = found.get(name.lower())
        if here is None:
            (inherited if merged else missing).append(name)
            continue
        # A release that records no CRC for a ROM cannot be checked against it. Rare,
        # and not a reason to call a file wrong.
        if crc and here != crc:
            wrong.append(f"{name}: {here} not {crc}")

    if wrong or missing:
        detail = wrong + [f"{name}: missing" for name in missing]
        return (STALE, detail[:8])
    if inherited:
        return (INCOMPLETE, [f"{name}: missing" for name in inherited][:8])
    return (CURRENT, [])


CHD_MAGIC = b"MComprHD"
# Where a file is sampled for runs of zeros. A torrent client fetches the first and
# last pieces early -- that is what makes a video previewable -- so a 14 GB disk at
# 0.4% already carried its header, and it was copied to the library as fourteen
# gigabytes of zeros. Eight small reads spread over the body, plus the tail, find a
# piece that has not arrived. A zero run alone is not proof, though: genuine CHDs
# carry them too (2 of 305 on the library this was found on), so a sample is only
# ever held against something -- the finished download, or the torrent's own piece
# hashes.
SAMPLE_BYTES = 4096
SAMPLES = 8
TAIL_BYTES = 65536
COMPARE_BYTES = 65536


def zero_samples(handle, size):
    """Offsets of the samples that are nothing but zeros."""
    if size <= SAMPLE_BYTES * 4:
        return []
    positions = [size - min(TAIL_BYTES, size)]
    positions += [int(size * (index + 0.5) / SAMPLES) for index in range(SAMPLES)]
    found = []
    for position in positions:
        handle.seek(position)
        chunk = handle.read(min(SAMPLE_BYTES, size - position))
        if chunk and not chunk.strip(b"\x00"):
            found.append(position)
    return found


def body_has_data(handle, size):
    """False when a sample of the body, or the tail, is nothing but zeros."""
    return not zero_samples(handle, size)


def verify_pieces(handle, size, offset, piece_size, hashes):
    """Hash every whole torrent piece that lies inside this file against `hashes`.

    True when they all match, False on the first that does not, None when no whole
    piece lies inside (a file smaller than a piece, or straddling two). `offset` is
    where the file starts in the torrent's byte stream.
    """
    if not piece_size or not hashes:
        return None
    first = -(-offset // piece_size)
    last = (offset + size) // piece_size
    if last <= first:
        return None
    for index in range(first, last):
        if index >= len(hashes):
            return None
        handle.seek(index * piece_size - offset)
        data = handle.read(piece_size)
        if len(data) != piece_size or hashlib.sha1(data).hexdigest() != hashes[index]:
            return False
    return True


def _same_bytes(handle, source, positions, size):
    """Whether the library file and its finished source agree at these offsets."""
    with open(source, "rb") as other:
        for position in positions:
            length = min(COMPARE_BYTES, size - position)
            handle.seek(position)
            other.seek(position)
            if handle.read(length) != other.read(length):
                return False
    return True


def check_disk(path, opener=None, source=None, pieces=None):
    """(state, [what is wrong]) for one disk in the library.

    No release CRC to hold it against -- a CHD's checksum covers the decompressed
    image, which nothing here is going to read -- so the question is the narrower
    one: is this a whole CHD, or the right-sized run of zeros a transfer left when
    it copied a disk the download client had not finished?

    With `pieces` = (offset, piece size, hashes) from the torrent the disk came from,
    the answer is exact. Otherwise a zero-filled sample is held against `source`, the
    finished copy in the download folder: the same bytes there means the disk is
    genuinely like that. With neither, a zero run proves nothing and the disk is
    left alone -- a false "damaged" would have the next transfer replace a good file,
    and the check after that say it again.
    """
    try:
        handle = open(path, "rb") if opener is None else opener(path)
    except FileNotFoundError:
        return (ABSENT, [])
    except OSError as error:
        return (DAMAGED, [str(error)])
    try:
        with handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(0)
            if handle.read(len(CHD_MAGIC)) != CHD_MAGIC:
                return (DAMAGED, ["not a CHD"])
            if pieces:
                verdict = verify_pieces(handle, size, *pieces)
                if verdict is False:
                    return (DAMAGED, ["does not match the download's piece hashes"])
                if verdict is True:
                    return (CURRENT, [])
            zeros = zero_samples(handle, size)
            if not zeros:
                return (CURRENT, [])
            if source and os.path.isfile(source) and os.path.getsize(source) == size:
                if not _same_bytes(handle, source, zeros, size):
                    return (DAMAGED, ["zero-filled where the finished download is not: "
                                      "copied before it had finished downloading"])
    except OSError as error:
        return (DAMAGED, [str(error)])
    return (CURRENT, [])


def check_library(items, manifests, root, reporter=None, should_continue=None,
                  where=None, opener=None, moved=None, sources=None, pieces=None):
    """Check every machine in `items` against the release, in place.

    Sets `state` and `state_detail` on each item and returns {state: count}. Stopping
    early is honest rather than fatal: what has been checked keeps its answer and the
    rest stay as they were.

    `where` names the file's current location for machines that are not at the path
    they belong at yet -- a library built to an older layout holds them one folder
    out, and looking only where they are *going* reports a file that is right there as
    absent. `moved` does the same for disks, by library-relative path. With an
    `opener` the files are read through it by their library-relative path, and `root`
    is not used: that is a library on a share.

    Disks are looked at too, for the one fault a transfer can leave in them: a file
    copied before the download had finished. `sources` maps a disk's library path to
    its finished copy in the download folder and `pieces` to (offset, piece size,
    hashes) in the torrent it came from; see `check_disk` for what each proves. A
    damaged disk makes the machine `damaged` and is named in `damaged_disks`, which
    is what makes the next transfer replace it.
    """
    reporter = reporter or Reporter()
    where = where or {}
    moved = moved or {}
    sources = sources or {}
    pieces = pieces or {}
    counts = {}
    total = len(items)
    for position, item in enumerate(items, 1):
        if should_continue and not should_continue():
            reporter.warn(f"Stopped after {position - 1:,} of {total:,}.")
            break
        here = where.get(item.name) or f"{item.folder}/{item.name}.zip"
        path = here if opener else os.path.join(root, here.replace("/", os.sep))
        state, detail = check(path, manifests.get(item.name), opener)
        item.rom_state = state
        item.damaged_disks = []
        disks = list(item.wanted_paths())[1:] if hasattr(item, "wanted_paths") else []
        for relpath in disks:
            current = moved.get(relpath, relpath)
            disk_path = current if opener else os.path.join(root, current.replace("/", os.sep))
            disk_state, disk_detail = check_disk(disk_path, opener,
                                                 source=sources.get(relpath),
                                                 pieces=pieces.get(relpath))
            if disk_state == DAMAGED:
                item.damaged_disks.append(relpath)
                name = os.path.basename(relpath)
                detail = list(detail) + [f"{name}: {why}" for why in disk_detail]
                state = DAMAGED
        item.state, item.state_detail = state, detail
        counts[state] = counts.get(state, 0) + 1
        if position % 200 == 0 or position == total:
            reporter.progress("verify", position, total,
                              f"{counts.get(STALE, 0):,} out of date")
    return counts


def summarise(counts):
    return ", ".join(f"{counts[state]:,} {state}" for state in STATES
                     if counts.get(state))
