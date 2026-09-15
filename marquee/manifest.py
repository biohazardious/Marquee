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


def path_for(copy_path):
    return os.path.join(copy_path, NAME)


def candidates(copy_path):
    """Every filename a record could be under, newest spelling first."""
    return [os.path.join(copy_path, name) for name in (NAME,) + LEGACY_NAMES]


def read(copy_path):
    """The destination's own record, or None when there is none to read."""
    if not copy_path or backends.is_remote(copy_path):
        return None
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
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    return data


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


def write(copy_path, config, mame_version, summary, previous=None):
    """Record what this destination now holds. Never fatal: a read-only destination
    should not fail a sync that otherwise worked."""
    if not copy_path or backends.is_remote(copy_path):
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
        "mame_version": mame_version,
        "settings": {key: getattr(config, key) for key in SETTING_KEYS},
        "stats": {"machines": summary.machines, "files": summary.files,
                  "bytes": summary.destination_bytes},
        "history": history[-MAX_HISTORY:],
    }

    target = path_for(copy_path)
    try:
        os.makedirs(copy_path, exist_ok=True)
        atomic.write_json(target, document, indent=2)
    except (OSError, ValueError):
        # A read-only destination, or a path the filesystem will not accept, must not
        # fail a sync that otherwise worked.
        return None
    return target
