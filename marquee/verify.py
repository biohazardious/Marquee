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


def zip_index(path):
    """{entry name, lowercased: crc as eight hex digits} from the central directory.

    Raises the zipfile error for a file that is not a readable archive; the caller
    decides what that means.
    """
    found = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            found[info.filename.lower()] = f"{info.CRC:08x}"
            # MAME writes flat zips, but a set rebuilt elsewhere may have foldered
            # them; the name is what identifies a ROM, not where it sits.
            found.setdefault(os.path.basename(info.filename).lower(), f"{info.CRC:08x}")
    return found


def check(path, expected):
    """(state, [what is wrong]) for one machine's zip.

    `expected` is [(rom name, crc, inherited from the parent)]. Extra entries are fine:
    a non-merged zip carries its BIOS and device ROMs too, and nothing here is
    interested in those.
    """
    if not expected:
        # Nothing to check it against. Saying "current" would be a claim; the caller
        # treats an unknown machine as untouched.
        return (CURRENT, [])
    if not os.path.isfile(path):
        return (ABSENT, [])
    try:
        found = zip_index(path)
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


def check_library(items, manifests, root, reporter=None, should_continue=None,
                  where=None):
    """Check every machine in `items` against the release, in place.

    Sets `state` and `state_detail` on each item and returns {state: count}. Stopping
    early is honest rather than fatal: what has been checked keeps its answer and the
    rest stay as they were.

    `where` names the file's current location for machines that are not at the path
    they belong at yet -- a library built to an older layout holds them one folder
    out, and looking only where they are *going* reports a file that is right there as
    absent.
    """
    reporter = reporter or Reporter()
    where = where or {}
    counts = {}
    total = len(items)
    for position, item in enumerate(items, 1):
        if should_continue and not should_continue():
            reporter.warn(f"Stopped after {position - 1:,} of {total:,}.")
            break
        here = where.get(item.name) or f"{item.folder}/{item.name}.zip"
        path = os.path.join(root, here.replace("/", os.sep))
        state, detail = check(path, manifests.get(item.name))
        item.state, item.state_detail = state, detail
        counts[state] = counts.get(state, 0) + 1
        if position % 200 == 0 or position == total:
            reporter.progress("verify", position, total,
                              f"{counts.get(STALE, 0):,} out of date")
    return counts


def summarise(counts):
    return ", ".join(f"{counts[state]:,} {state}" for state in STATES
                     if counts.get(state))
