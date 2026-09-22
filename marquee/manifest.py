"""What the destination itself remembers.

A small file written into the destination root after every sync. It is what lets the
app open and say "this console holds MAME 0.282, 9,914 machines, synced on Tuesday"
instead of asking the user, and it carries the filters along with the set so they can be
read back on another machine or after a reinstall.

Deliberately small and readable: the file-level truth comes from indexing the
destination (see marquee.sync), so there is no inventory here to fall out of date.
"""
import json
import os
from datetime import datetime, timezone

from . import atomic, backends

NAME = ".marquee.json"
# What the file was called before the project was renamed. A library written by an
# older build still has to be recognised, or reopening it looks like a fresh install
# and the run re-copies everything it already holds.
LEGACY_NAMES = (".mameparser.json",)
FORMAT = 1
MAX_HISTORY = 20

SETTING_KEYS = ("allow_mature", "mature_rom_folder", "blacklist_genres",
                "blacklist_categories", "blacklist_roms")

# Records of libraries on a share, by destination, as last read or written through a
# connection that was open anyway -- indexing for a plan, or at the end of a transfer.
# The pages ask on every poll, and a connection to the console per poll is not on.
# Without this a library on SMB never had a record at all: the upgrade preview
# guessed its release from the settings, and a reinstall could not read its filters.
_REMOTE = {}


def path_for(copy_path):
    return os.path.join(copy_path, NAME)


def candidates(copy_path):
    """Every filename a record could be under, newest spelling first."""
    return [os.path.join(copy_path, name) for name in (NAME,) + LEGACY_NAMES]


def _valid(data):
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    return data


def read(copy_path):
    """The destination's own record, or None when there is none to read.

    For a library on a share, the one last seen through `load` or `write`: nothing
    here opens a connection.
    """
    if not copy_path:
        return None
    if backends.is_remote(copy_path):
        return _REMOTE.get(copy_path)
    data = None
    for candidate in candidates(copy_path):
        try:
            with open(candidate, encoding="utf-8") as handle:
                data = json.load(handle)
            break
        except (OSError, ValueError):
            # Missing, unreadable, or not the JSON we expect -- try the next name.
            continue
    if data is None:
        return None
    return _valid(data)


def load(copy_path, backend):
    """Read a shared library's record through a backend already connected to it.

    Never fatal: a record that cannot be read is a record that is not there, and a
    plan is not the place to fail over it.
    """
    if not copy_path or not backends.is_remote(copy_path):
        return read(copy_path)
    record = None
    for name in (NAME,) + LEGACY_NAMES:
        try:
            body = backend.read_file(name)
        except Exception:  # noqa: BLE001 - see above
            continue
        if body is None:
            continue
        try:
            record = _valid(json.loads(body))
        except ValueError:
            record = None
        break
    _REMOTE[copy_path] = record
    return record


def settings_from(record):
    """The filter settings a destination was built with, ready to apply to a Config."""
    if not record:
        return {}
    stored = record.get("settings") or {}
    return {key: stored[key] for key in SETTING_KEYS if key in stored}


def describe(record):
    """A one-line summary for the page, or None."""
    if not record:
        return None
    stats = record.get("stats") or {}
    return {
        "mame_version": record.get("mame_version"),
        "updated": record.get("updated"),
        "machines": stats.get("machines"),
        "files": stats.get("files"),
        "bytes": stats.get("bytes"),
        "runs": len(record.get("history") or []),
    }


def write(copy_path, config, mame_version, summary, previous=None, backend=None):
    """Record what this destination now holds. Never fatal: a read-only destination
    should not fail a sync that otherwise worked.

    A library on a share is written through `backend`, the connection the transfer
    used; without one there is nothing to write it with.
    """
    remote = backends.is_remote(copy_path)
    if not copy_path or (remote and backend is None):
        return None

    record = previous or read(copy_path) or {}
    history = list(record.get("history") or [])
    history.append({
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "from": record.get("mame_version"),
        "to": mame_version,
        "copied": summary.copied, "updated": summary.updated,
        "moved": summary.moved, "deleted": summary.deleted,
        "skipped": summary.skipped, "bytes": summary.copied_bytes,
        "cancelled": summary.cancelled,
    })

    document = {
        "format": FORMAT,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Only a run that finished moves the library to the new release. Written
        # regardless, an interrupted 0.289 -> 0.290 run recorded 0.290, and the next
        # upgrade was priced from there -- missing every machine not yet moved.
        "mame_version": (mame_version
                         if not (summary.cancelled or getattr(summary, "failed", 0))
                         or not record.get("mame_version")
                         else record.get("mame_version")),
        "settings": {key: getattr(config, key) for key in SETTING_KEYS},
        "stats": {"machines": summary.machines, "files": summary.files,
                  "bytes": summary.destination_bytes},
        "history": history[-MAX_HISTORY:],
    }

    if remote:
        try:
            backend.write_text(NAME, json.dumps(document, indent=2) + "\n")
        except Exception:  # noqa: BLE001 - never fatal, as above
            return None
        _REMOTE[copy_path] = document
        return NAME

    target = path_for(copy_path)
    try:
        os.makedirs(copy_path, exist_ok=True)
        atomic.write_json(target, document, indent=2)
    except (OSError, ValueError):
        # A read-only destination, or a path the filesystem will not accept, must not
        # fail a sync that otherwise worked.
        return None
    return target
